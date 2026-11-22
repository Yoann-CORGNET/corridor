"""Engine -> ATAK: routes, zones, drones and notifications as CoT through OpenTAKServer.

``TakBridge`` subscribes to the engine and sends from its own task over one ``TakClient``
connection (plain TCP, OTS port 8088), so ATAK phones on the same OTS see:

- the planned route of each mission: a line coloured by plan status, its reasons, take-off and
  arrival times and flight levels in the remarks (ATAK draws the line on the ground; heights
  are in the remarks);
- the zones of each mission: polygons (friendly red, green while cut, enemy purple, radar
  orange, zone to avoid yellow); a zone that goes away is deleted;
- each drone in flight: a friendly UAV marker, at most once per ``DRONE_EVERY_S``;
- each notification: a GeoChat message, direct to the recipient when an ATAK device with that
  callsign has been seen on the server, else to All Chat Rooms prefixed with the callsign (the
  command's own notifications stay in the web front end).

Every ``REFRESH_S`` the bridge sends the whole picture again, so a phone that joins late gets
it (OTS does not replay). If OTS is down or drops the connection, the bridge retries every
``RETRY_S``; what happened meanwhile, notifications included, is not replayed. The server runs
without it unless ``corridor-server --tak-host`` is given. One way only: nothing ATAK sends
becomes a command yet.

Everything goes to every ATAK client of the server: the audience rules of the web front end
(each pilot sees the zones near his route) do not apply here yet.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from corridor.domain.events import (
    CutCancelled,
    CutConfirmed,
    CutScheduled,
    DroneState,
    Event,
    JammerRestarted,
    MissionCreated,
    Notification,
    PlanUpdated,
    RouteReason,
    ThreatAdded,
    ThreatRemoved,
)
from corridor.domain.zones import Zone
from corridor.engine.engine import Engine
from corridor.tak import cot
from corridor.tak.client import Identity, TakClient

log = logging.getLogger(__name__)

REFRESH_S = 15.0
RETRY_S = 5.0
DRONE_EVERY_S = 1.0
DRONE_STALE_S = 30.0
UID = "corridor-server"
CALLSIGN = "CORRIDOR"

ROUTE_COLOR = {
    "approved": cot.argb(255, 64, 192, 87),
    "preview": cot.argb(255, 240, 140, 0),
    "baseline": cot.argb(255, 34, 184, 207),
    "fallback": cot.argb(255, 250, 82, 82),
    "straight": cot.argb(255, 250, 82, 82),
}
ZONE_COLOR: dict[str, tuple[int, int]] = {
    "friendly_jammer": (cot.argb(255, 224, 49, 49), cot.argb(60, 224, 49, 49)),
    "friendly_cut": (cot.argb(255, 47, 158, 68), cot.argb(40, 47, 158, 68)),
    "enemy_jammer": (cot.argb(255, 156, 54, 181), cot.argb(60, 156, 54, 181)),
    "radar": (cot.argb(255, 253, 126, 20), cot.argb(50, 253, 126, 20)),
    "avoid": (cot.argb(255, 250, 176, 5), cot.argb(40, 250, 176, 5)),
}
KIND_NAME: dict[str, str] = {
    "friendly_jammer": "jammer",
    "enemy_jammer": "enemy jammer",
    "radar": "radar",
    "avoid": "zone to avoid",
}


@dataclass
class TakBridge:
    engine: Engine
    host: str
    port: int = 8088
    _queue: asyncio.Queue[Event | None] = field(default_factory=asyncio.Queue, init=False)
    _connected: bool = field(default=False, init=False)
    _uids: dict[str, str] = field(default_factory=dict, init=False)
    """Callsign -> uid of the ATAK devices seen on the server, for direct chats."""
    _zones_sent: set[str] = field(default_factory=set, init=False)
    _drone_sent: dict[str, float] = field(default_factory=dict, init=False)
    _messages: itertools.count[int] = field(default_factory=itertools.count, init=False)

    def on_event(self, event: Event) -> None:
        """Engine subscriber: never blocks, only queues what is worth sending."""
        if not self._connected:
            return
        match event:
            case DroneState():
                last = self._drone_sent.get(event.mission, 0.0)
                if time.monotonic() - last < DRONE_EVERY_S:
                    return
                self._drone_sent[event.mission] = time.monotonic()
            case (
                PlanUpdated()
                | Notification()
                | CutScheduled()
                | CutConfirmed()
                | CutCancelled()
                | JammerRestarted()
                | ThreatAdded()
                | ThreatRemoved()
                | MissionCreated()
            ):
                pass
            case _:
                return
        self._queue.put_nowait(event)

    async def run(self) -> None:
        """Keep a connection to OTS and send, until cancelled."""
        while True:
            client = TakClient(self.host, self.port, self._identity())
            reader: asyncio.Task[None] | None = None
            try:
                await client.connect()
                log.info("TAK: connected to %s:%s", self.host, self.port)
                reader = asyncio.create_task(self._read(client))
                self._connected = True
                await self._send_all(client)
                await self._pump(client, reader)
            except (OSError, ConnectionError) as exc:
                log.warning("TAK: %s; retrying in %.0f s", exc, RETRY_S)
            finally:
                self._connected = False
                self._drain()
                if reader is not None:
                    reader.cancel()
                    with contextlib.suppress(asyncio.CancelledError, OSError, ConnectionError):
                        await reader
                with contextlib.suppress(OSError, ConnectionError):
                    await client.close()
            await asyncio.sleep(RETRY_S)

    async def _pump(self, client: TakClient, reader: asyncio.Task[None]) -> None:
        loop = asyncio.get_running_loop()
        refresh_at = loop.time() + REFRESH_S
        while True:
            if reader.done():
                raise ConnectionError("OTS closed the connection")
            try:
                event = await asyncio.wait_for(
                    self._queue.get(), max(0.0, refresh_at - loop.time())
                )
            except TimeoutError:
                await self._send_all(client)
                refresh_at = loop.time() + REFRESH_S
                continue
            if event is not None:
                await self._send_event(client, event)

    async def _read(self, client: TakClient) -> None:
        """Learn the uid of each ATAK callsign from what OTS routes to us."""
        async for ev in client.events():
            detail = ev.find("detail")
            contact = detail.find("contact") if detail is not None else None
            uid, callsign = ev.get("uid"), contact.get("callsign") if contact is not None else None
            if uid and callsign and (ev.get("type") or "").startswith("a-"):
                self._uids[callsign] = uid

    def _drain(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()

    def _identity(self) -> Identity:
        """Corridor shows on the ATAK map at the first launch point, else the first friendly
        jammer: any point in the area would do."""
        scenario = self.engine.scenario
        places = [m.launch for m in scenario.missions] + [j.pos for j in scenario.friendly_jammers]
        pos = places[0] if places else None
        return Identity(UID, CALLSIGN, pos.lat if pos else 0.0, pos.lon if pos else 0.0)

    async def _send_event(self, client: TakClient, event: Event) -> None:
        match event:
            case PlanUpdated():
                await client.send(self.route(event))
            case DroneState():
                await client.send(self.drone(event))
            case Notification():
                message = self.chat(event)
                if message is not None:
                    await client.send(message)
            case _:
                await self._send_zones(client)

    async def _send_all(self, client: TakClient) -> None:
        await self._send_zones(client)
        for runtime in self.engine.missions.values():
            if runtime.plan is not None:
                await client.send(self.route(runtime.plan))

    async def _send_zones(self, client: TakClient) -> None:
        now = time.time()
        shapes: dict[str, bytes] = {}
        for mission_id, runtime in self.engine.missions.items():
            for zone in runtime.constraints.zones:
                uid = f"corridor.zone.{mission_id}.{zone.threat}"
                shapes[uid] = self.zone(uid, mission_id, zone, now)
        for message in shapes.values():
            await client.send(message)
        for uid in self._zones_sent - shapes.keys():
            await client.send(cot.delete(uid, now))
        self._zones_sent = set(shapes)

    # --- messages ------------------------------------------------------------------------

    def route(self, plan: PlanUpdated) -> bytes:
        label = self.engine.scenario.clock_label
        route = plan.optimised_route
        takeoff = next((p.t for p in route if p.action != "ground"), route[0].t)
        levels = sorted({p.alt_m for p in route if p.alt_m})
        lines = [f"{plan.status}, rev {plan.revision}"]
        lines.append(f"Take-off {label(takeoff)}, on target {label(route[-1].t)}")
        if levels:
            lines.append("Levels " + ", ".join(f"{a:.0f} m" for a in levels) + " above ground")
        lines += [reason_text(r, label) for r in plan.reasons]
        return cot.shape(
            f"corridor.route.{plan.mission}",
            f"{plan.mission} route ({plan.status})",
            [(p.lat, p.lon) for p in route],
            time.time(),
            closed=False,
            stroke=ROUTE_COLOR.get(plan.status, ROUTE_COLOR["baseline"]),
            stroke_weight=4.0,
            remarks="\n".join(lines),
        )

    def zone(self, uid: str, mission_id: str, zone: Zone, now: float) -> bytes:
        cut = zone.kind == "friendly_jammer" and self._cut_now(zone.threat)
        stroke, fill = ZONE_COLOR["friendly_cut" if cut else zone.kind]
        name = f"{zone.threat} ({KIND_NAME[zone.kind]}{', cut' if cut else ''})"
        remarks = f"Zone of {zone.threat} for mission {mission_id}"
        if zone.forbidden == "outside":
            remarks += ": forbidden outside this ring"
        return cot.shape(
            uid,
            name,
            [(p.lat, p.lon) for p in zone.ring],
            now,
            closed=True,
            stroke=stroke,
            fill=fill,
            stroke_weight=2.0,
            remarks=remarks,
        )

    def drone(self, state: DroneState) -> bytes:
        runtime = self.engine.missions.get(state.mission)
        name = runtime.mission.drone_id if runtime else state.mission
        remarks = (
            f"Mission {state.mission}, {state.action}, battery used {state.battery_used_pct:.0f}%"
            + (f", {state.delay_s:.0f} s late" if state.delay_s else "")
        )
        return cot.marker(
            f"corridor.drone.{state.mission}",
            name,
            "a-f-A-M-F-Q",
            state.position.lat,
            state.position.lon,
            time.time(),
            remarks=remarks,
            stale_s=DRONE_STALE_S,
        )

    def chat(self, note: Notification) -> bytes | None:
        """Direct to the recipient's ATAK device if seen, else All Chat Rooms; None for the
        command, which reads its notifications in the web front end."""
        if note.to == self.engine.scenario.command_callsign:
            return None
        uid = self._uids.get(note.to)
        message_id = f"corridor-{next(self._messages)}"
        if uid is not None:
            return cot.chat(
                UID, CALLSIGN, note.text, time.time(), message_id, to_uid=uid, to_callsign=note.to
            )
        return cot.chat(UID, CALLSIGN, f"[{note.to}] {note.text}", time.time(), message_id)

    def _cut_now(self, jammer: str) -> bool:
        now = self.engine.clock.now()
        return any(
            c.jammer == jammer and c.status == "confirmed" and c.start_s <= now < c.end_s
            for c in self.engine.state.cuts.values()
        )


def reason_text(r: RouteReason, clock: Callable[[float], str]) -> str:
    """One line on why the route goes where it goes (as in the web front end)."""
    name = (
        "the area out of the pilot's reach"
        if r.kind == "link"
        else f"{KIND_NAME[r.kind]} {r.threat}"
    )
    when = (
        f", {clock(r.enter_s)} to {clock(r.exit_s)}"
        if r.enter_s is not None and r.exit_s is not None
        else ""
    )
    match r.effect:
        case "avoided":
            return f"Around {name}"
        case "climbed_over":
            return f"Over {name} (flies above its zone)"
        case "crossed_in_cut":
            return f"Through {name} during its cut{when}"
        case _:
            if r.kind == "radar":
                return f"Through {name}: seen{when}"
            if r.kind == "avoid":
                return f"Through {name}{when}"
            return f"Through {name} while it is on{when}: control link at risk"
