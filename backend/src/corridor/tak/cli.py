"""``corridor-tak``: talk to OpenTAKServer over CoT TCP streaming.

corridor-tak listen              print every CoT routed to us
corridor-tak listen --capture d  also save each event as raw XML in d/
corridor-tak demo                publish a test jam zone and drone corridor
corridor-tak smoke               demo + listener on two connections, exit 0 if routed
corridor-tak route-smoke         route addressed to one of two listeners, exit 0 if only it gets it
"""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from pathlib import Path
from xml.etree.ElementTree import Element

from corridor.domain.events import RoutePoint
from corridor.domain.scenario import load_scenario
from corridor.export.atak import route_cot, route_uid
from corridor.export.route import flight_route
from corridor.tak.client import ANNOUNCE_EVERY_S, Identity, TakClient
from corridor.tak.cot import argb, encode, marker, shape, summary

DEFAULT_LAT = 48.1374
DEFAULT_LON = 11.5755
JAM_RED = argb(255, 220, 30, 30)
JAM_FILL = argb(70, 220, 30, 30)
CORRIDOR_BLUE = argb(255, 30, 110, 230)


def demo_events(lat: float, lon: float, t: float, suffix: str = "") -> list[bytes]:
    """A jam zone square (~1.1 km) and a drone corridor that crosses it.

    ``suffix`` is appended to every uid so a test run only matches its own events.
    """
    d = 0.005
    jam = [(lat - d, lon - d), (lat - d, lon + d), (lat + d, lon + d), (lat + d, lon - d)]
    corridor = [(lat - 4 * d, lon - 4 * d), (lat, lon), (lat + 4 * d, lon + 2 * d)]
    return [
        marker(
            f"corridor-demo-ew-unit{suffix}",
            "EW-1",
            "a-f-G-U-C",
            lat - 2 * d,
            lon + 3 * d,
            t,
            remarks="Friendly EW unit (demo)",
        ),
        shape(
            f"corridor-demo-jam-1{suffix}",
            "JAM 2.4 GHz 14:00-16:00",
            jam,
            t,
            closed=True,
            stroke=JAM_RED,
            fill=JAM_FILL,
            remarks="Declared jam zone (demo)",
        ),
        shape(
            f"corridor-demo-corridor-1{suffix}",
            "FPV corridor A",
            corridor,
            t,
            closed=False,
            stroke=CORRIDOR_BLUE,
            stroke_weight=5.0,
            remarks="Booked drone corridor (demo)",
        ),
    ]


def _identity(args: argparse.Namespace, default_callsign: str) -> Identity:
    callsign = args.callsign or default_callsign
    return Identity(f"corridor-{callsign}-{uuid.uuid4().hex[:8]}", callsign, args.lat, args.lon)


async def listen(args: argparse.Namespace) -> None:
    client = TakClient(args.host, args.port, _identity(args, "CORRIDOR-RX"))
    await client.connect()
    capture: Path | None = args.capture
    if capture:
        capture.mkdir(parents=True, exist_ok=True)
    print(f"listening on {args.host}:{args.port} as {client.me.callsign}")
    try:
        async for ev in client.events():
            print(summary(ev), flush=True)
            if capture:
                name = f"{time.strftime('%H%M%S')}_{ev.get('type')}_{ev.get('uid')}.xml"
                (capture / name.replace("/", "_")).write_bytes(encode(ev))
    finally:
        await client.close()


async def demo(args: argparse.Namespace) -> None:
    client = TakClient(args.host, args.port, _identity(args, "CORRIDOR"))
    await client.connect()
    print(f"publishing demo shapes around {args.lat},{args.lon} (Ctrl-C to stop)")
    try:
        while True:
            for cot in demo_events(args.lat, args.lon, time.time()):
                await client.send(cot)
            await asyncio.sleep(ANNOUNCE_EVERY_S)
    finally:
        await client.close()


async def smoke(args: argparse.Namespace) -> int:
    # Per-run nonce in callsigns and uids: OTS keeps per-callsign queues and replays stored
    # CoT on connect, so fixed names could make a run pass on a previous run's messages.
    nonce = uuid.uuid4().hex[:6]
    rx = TakClient(args.host, args.port, _identity(args, f"SMOKE-RX-{nonce}"))
    tx = TakClient(args.host, args.port, _identity(args, f"SMOKE-TX-{nonce}"))
    await rx.connect()
    await asyncio.sleep(2)  # let OTS register the receiver before anything is sent
    await tx.connect()
    suffix = f"-{nonce}"
    events = demo_events(args.lat, args.lon, time.time(), suffix)
    expected = {
        f"corridor-demo-ew-unit{suffix}",
        f"corridor-demo-jam-1{suffix}",
        f"corridor-demo-corridor-1{suffix}",
    }
    for cot in events:
        await tx.send(cot)

    seen: set[str] = set()

    async def collect() -> None:
        ev: Element
        async for ev in rx.events():
            print("rx:", summary(ev))
            if ev.get("uid") in expected:
                seen.add(ev.get("uid", ""))
                if seen == expected:
                    return

    try:
        await asyncio.wait_for(collect(), timeout=args.timeout)
    except TimeoutError:
        pass
    finally:
        await tx.close()
        await rx.close()
    missing = expected - seen
    if missing:
        print(f"FAIL: not routed through OTS: {sorted(missing)}")
        return 1
    print("OK: demo events routed through OTS")
    return 0


async def route_smoke(args: argparse.Namespace) -> int:
    """Pivot test 1: a route carrying ``<marti><dest>`` reaches that callsign only."""
    nonce = uuid.uuid4().hex[:6]
    pilot = TakClient(args.host, args.port, _identity(args, f"SMOKE-PILOT-{nonce}"))
    other = TakClient(args.host, args.port, _identity(args, f"SMOKE-OTHER-{nonce}"))
    tx = TakClient(args.host, args.port, _identity(args, f"SMOKE-TX-{nonce}"))
    for client in (pilot, other):
        await client.connect()
    await asyncio.sleep(2)  # let OTS register both receivers
    await tx.connect()

    scenario = load_scenario(args.scenario)
    mission = scenario.missions[0].model_copy(
        update={"id": f"SMOKE-{nonce}", "pilot_callsign": pilot.me.callsign}
    )
    ends = (mission.launch, mission.target)
    points = [
        RoutePoint(lat=p.lat, lon=p.lon, t=60.0 * i, action="fly") for i, p in enumerate(ends)
    ]
    route = flight_route(scenario, mission, points)
    uid = route_uid(route)
    await tx.send(route_cot(route, time.time()))

    async def wait_for(client: TakClient) -> bool:
        async for ev in client.events():
            if ev.get("uid") == uid:
                return True
        return False

    async def got(client: TakClient) -> bool:
        try:
            return await asyncio.wait_for(wait_for(client), args.timeout)
        except TimeoutError:
            return False

    try:
        to_pilot, to_other = await asyncio.gather(got(pilot), got(other))
    finally:
        for client in (tx, pilot, other):
            await client.close()
    print(f"addressed recipient got it: {to_pilot}; other client got it: {to_other}")
    if to_pilot and not to_other:
        print("OK: OTS delivers the route to the addressed callsign only")
        return 0
    print("FAIL: " + ("not delivered" if not to_pilot else "also delivered to another client"))
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="corridor-tak")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--callsign")
    parser.add_argument("--lat", type=float, default=DEFAULT_LAT)
    parser.add_argument("--lon", type=float, default=DEFAULT_LON)
    sub = parser.add_subparsers(dest="command", required=True)
    p_listen = sub.add_parser("listen")
    p_listen.add_argument("--capture", type=Path)
    sub.add_parser("demo")
    p_smoke = sub.add_parser("smoke")
    p_smoke.add_argument("--timeout", type=float, default=10.0)
    p_route = sub.add_parser("route-smoke")
    p_route.add_argument("--timeout", type=float, default=10.0)
    p_route.add_argument("--scenario", type=Path, default=Path("scenarios/demo.json"))
    args = parser.parse_args()

    try:
        if args.command == "listen":
            asyncio.run(listen(args))
        elif args.command == "demo":
            asyncio.run(demo(args))
        elif args.command == "route-smoke":
            raise SystemExit(asyncio.run(route_smoke(args)))
        else:
            raise SystemExit(asyncio.run(smoke(args)))
    except KeyboardInterrupt:
        pass
