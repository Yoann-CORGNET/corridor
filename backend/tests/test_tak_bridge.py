"""Engine -> ATAK: the CoT the bridge sends, and the bridge against a fake OTS."""

import asyncio
import contextlib
import time
from xml.etree.ElementTree import Element, fromstring

import pytest

from corridor.adapters.tak import bridge as tak_bridge
from corridor.adapters.tak.bridge import ROUTE_COLOR, TakBridge
from corridor.domain.commands import ConfirmCut, ScheduleCut
from corridor.domain.events import (
    Notification,
    PlanMetrics,
    PlanUpdated,
    RoutePoint,
    RouteReason,
)
from corridor.domain.scenario import Scenario
from corridor.engine.clock import SimClock
from corridor.engine.engine import Engine
from corridor.tak import client as tak_client
from corridor.tak import cot

PILOT, EW, COMMAND = "PILOT-1", "EW-J3", "CMD"


def engine(scenario: Scenario) -> Engine:
    return Engine(scenario, 100, SimClock(1, wall=lambda: 1000.0))


def plan(scenario: Scenario) -> PlanUpdated:
    m = scenario.missions[0]
    route = [
        RoutePoint(lat=m.launch.lat, lon=m.launch.lon, t=900, action="fly", alt_m=30.0),
        RoutePoint(lat=m.target.lat, lon=m.target.lon, t=1200, action="fly", alt_m=60.0),
    ]
    metrics = PlanMetrics(
        baseline_battery_used_pct=50, optimised_battery_used_pct=40, saved_pct=20, saved_s=60
    )
    reason = RouteReason(
        threat="J3", kind="friendly_jammer", effect="crossed_in_cut", enter_s=950, exit_s=1100
    )
    return PlanUpdated(
        mission=m.id,
        revision=3,
        status="approved",
        metrics=metrics,
        baseline_route=route,
        optimised_route=route,
        reasons=[reason],
    )


def remarks(ev: Element) -> str:
    return ev.findtext("detail/remarks") or ""


def test_the_route_is_a_line_with_its_reasons_in_the_remarks(scenario: Scenario) -> None:
    ev = fromstring(TakBridge(engine(scenario), "x").route(plan(scenario)))
    assert ev.get("type") == "u-d-f" and ev.get("uid") == "corridor.route.M1"
    assert len(ev.findall("detail/link")) == 2
    assert ev.find("detail/strokeColor").get("value") == str(ROUTE_COLOR["approved"])  # type: ignore[union-attr]
    text = remarks(ev)
    assert "Take-off 14:15:00, on target 14:20:00" in text
    assert "Levels 30 m, 60 m above ground" in text
    assert "Through jammer J3 during its cut, 14:15:50 to 14:18:20" in text


def test_a_friendly_zone_turns_green_while_cut(scenario: Scenario) -> None:
    wall = [0.0]
    e = Engine(scenario, 100, SimClock(1, wall=lambda: wall[0]))
    b = TakBridge(e, "x")
    zone = next(z for z in e.missions["M1"].constraints.zones if z.threat == "J3")
    on = fromstring(b.zone("u", "M1", zone, time.time()))
    assert on.find("detail/contact").get("callsign") == "J3 (jammer)"  # type: ignore[union-attr]
    cut = e.submit(ScheduleCut(actor=PILOT, jammer="J3", start_s=1100, end_s=1500))[0]
    e.submit(ConfirmCut(actor=COMMAND, cut=cut.cut.id))  # type: ignore[union-attr]
    e.clock.set(paused=False)
    wall[0] = 1200.0  # simulated 1200 s: inside the cut
    green = fromstring(b.zone("u", "M1", zone, time.time()))
    assert green.find("detail/contact").get("callsign") == "J3 (jammer, cut)"  # type: ignore[union-attr]
    assert len(green.findall("detail/link")) == len(zone.ring) + 1  # closed


def test_chat_goes_direct_when_the_callsign_is_on_tak(scenario: Scenario) -> None:
    b = TakBridge(engine(scenario), "x")
    assert b.chat(Notification(to=COMMAND, text="for the web")) is None
    to_all = fromstring(b.chat(Notification(to=PILOT, text="New route")) or b"")
    assert to_all.get("type") == "b-t-f"
    assert to_all.find("detail/__chat").get("chatroom") == cot.ALL_CHAT  # type: ignore[union-attr]
    assert remarks(to_all) == "[PILOT-1] New route"
    b._uids[PILOT] = "ANDROID-42"
    direct = fromstring(b.chat(Notification(to=PILOT, text="New route")) or b"")
    assert direct.find("detail/__chat").get("id") == "ANDROID-42"  # type: ignore[union-attr]
    assert direct.find("detail/marti/dest").get("callsign") == PILOT  # type: ignore[union-attr]
    assert remarks(direct) == "New route"


def test_bridge_against_a_fake_ots(scenario: Scenario, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tak_client, "CONNECT_SETTLE_S", 0.0)
    monkeypatch.setattr(tak_bridge, "REFRESH_S", 0.3)
    received: list[Element] = []

    async def run() -> None:
        connected = asyncio.Event()

        async def ots(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            # An ATAK device with the operator's callsign is on the server.
            writer.write(cot.self_sa("ANDROID-7", EW, 48.1, 11.5, time.time()))
            await writer.drain()
            connected.set()
            framer = cot.Framer()
            while data := await reader.read(65536):
                received.extend(framer.feed(data))
            writer.close()

        server = await asyncio.start_server(ots, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        e = engine(scenario)
        b = TakBridge(e, "127.0.0.1", port)
        e.subscribe(b.on_event)
        task = asyncio.create_task(b.run())
        await asyncio.wait_for(connected.wait(), 5)
        await asyncio.sleep(0.2)  # first full picture sent, the operator's SA read
        e.submit(ScheduleCut(actor=PILOT, jammer="J3", start_s=1100, end_s=1500))
        b.on_event(plan(scenario))
        b.on_event(Notification(to=EW, text="Cut J3 now"))
        await asyncio.sleep(0.6)  # and one refresh
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        server.close()
        await asyncio.wait_for(server.wait_closed(), 5)

    asyncio.run(run())
    types = [ev.get("type") for ev in received]
    assert types[0] == "a-f-G-U-C"  # Corridor announces itself first
    uids = {ev.get("uid") for ev in received}
    assert {"corridor.zone.M1.J3", "corridor.route.M1"} <= uids
    chats = [ev for ev in received if ev.get("type") == "b-t-f"]
    to_operator = [c for c in chats if c.find("detail/marti/dest") is not None]
    assert len(to_operator) == 1
    assert to_operator[0].find("detail/__chat").get("id") == "ANDROID-7"  # type: ignore[union-attr]
    assert remarks(to_operator[0]) == "Cut J3 now"
    # The full picture again on refresh: the zone was sent at least twice.
    assert sum(1 for ev in received if ev.get("uid") == "corridor.zone.M1.J3") >= 2


def test_a_zone_that_goes_away_is_deleted(scenario: Scenario) -> None:
    sent: list[bytes] = []

    class Recorder:
        async def send(self, data: bytes) -> None:
            sent.append(data)

    e = engine(scenario)
    b = TakBridge(e, "x")
    asyncio.run(b._send_zones(Recorder()))  # type: ignore[arg-type]
    assert "corridor.zone.M1.J3" in b._zones_sent
    e.missions["M1"].constraints = e.missions["M1"].constraints.model_copy(
        update={"zones": [z for z in e.missions["M1"].constraints.zones if z.threat != "J3"]}
    )
    sent.clear()
    asyncio.run(b._send_zones(Recorder()))  # type: ignore[arg-type]
    deletes = [fromstring(d) for d in sent if b"t-x-d-d" in d]
    assert [d.find("detail/link").get("uid") for d in deletes] == ["corridor.zone.M1.J3"]  # type: ignore[union-attr]
