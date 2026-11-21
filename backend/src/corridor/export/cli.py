"""``corridor-export``: plan a scenario offline and export the route.

corridor-export --cut J3:900:1380                   write every file to export/
corridor-export --cut J3:900:1380 --send 10.0.0.5   also send the route to the pilot's ATAK
corridor-export --revoke 10.0.0.5                   remove the route from the pilot's ATAK
corridor-export --mission M2                        another mission than the first one

Cuts given with ``--cut`` count as confirmed. The running server exports its own current
plan over HTTP instead (``/api/export/...``). ``--out`` must stay inside the working
directory, and each file inside ``--out``: nothing is written anywhere else.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from corridor.domain.planner.plan import plan_mission
from corridor.domain.scenario import load_scenario
from corridor.domain.state import SCENARIO_ACTOR, Cut
from corridor.domain.zones import mission_constraints
from corridor.export.atak import TakTarget, deliver, revoke, route_cot
from corridor.export.firmware import ardupilot_waypoints, inav_mission
from corridor.export.route import DEFAULT_ALTITUDE_M, FlightRoute, briefing, flight_route


def _cut(text: str) -> tuple[str, float, float]:
    try:
        jammer, start, end = text.split(":")
        return jammer, float(start), float(end)
    except ValueError:
        raise argparse.ArgumentTypeError("expected JAMMER:START_S:END_S") from None


def _target(text: str, callsign: str) -> TakTarget:
    host, _, port = text.partition(":")
    return TakTarget(host, int(port) if port else 8088, callsign)


def contained(path: Path, root: Path) -> Path:
    """``path`` resolved against ``root``; ValueError if it leaves ``root``."""
    root = root.resolve()
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"{path} is outside {root}")
    return resolved


def write_files(route: FlightRoute, out: Path, base: Path | None = None) -> list[Path]:
    """Write every export of ``route`` into ``out``, which must stay inside ``base`` (default:
    the working directory)."""
    folder = contained(out, base or Path.cwd())
    folder.mkdir(parents=True, exist_ok=True)
    stem = route.mission_id
    files = {
        f"{stem}.mission": inav_mission(route),
        f"{stem}.waypoints": ardupilot_waypoints(route),
        f"{stem}-atak-route.xml": route_cot(route, time.time()).decode(),
        f"{stem}-briefing.txt": "\n".join(briefing(route)) + "\n",
    }
    written = []
    for name, content in files.items():
        path = contained(Path(name), folder)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(prog="corridor-export")
    parser.add_argument("--scenario", type=Path, default=Path("scenarios/demo.json"))
    parser.add_argument("--mission", help="mission id (default: the first one)")
    parser.add_argument("--cell-m", type=float, default=100.0)
    parser.add_argument("--cut", type=_cut, action="append", default=[], metavar="J:START:END")
    parser.add_argument("--altitude", type=float, default=DEFAULT_ALTITUDE_M, help="m above launch")
    parser.add_argument("--out", type=Path, default=Path("export"))
    parser.add_argument("--send", metavar="HOST[:PORT]", help="OpenTAKServer CoT TCP")
    parser.add_argument("--revoke", metavar="HOST[:PORT]", help="delete the route on ATAK")
    parser.add_argument("--callsign", default="CORRIDOR", help="our callsign on TAK")
    parser.add_argument("--pilot-uid", help="skip waiting for the pilot's position report")
    args = parser.parse_args()

    scenario = load_scenario(args.scenario)
    mission = scenario.mission(args.mission) if args.mission else scenario.missions[0]
    if mission is None:
        raise SystemExit(f"unknown mission {args.mission!r}")
    cuts = [
        Cut(
            id=f"cut-{jammer}-{i}",
            jammer=jammer,
            start_s=start,
            end_s=end,
            status="confirmed",
            requested_by=SCENARIO_ACTOR,
        )
        for i, (jammer, start, end) in enumerate(args.cut, start=1)
    ]
    grid = mission_constraints(scenario, mission, args.cell_m).grid
    plan = plan_mission(scenario, mission, grid, cuts)
    if plan is None:
        raise SystemExit("no route: even the baseline is blocked")
    route = flight_route(
        scenario,
        mission,
        plan.optimised_route,
        args.altitude,
        cuts=cuts,
        status=plan.status,
        battery_used_pct=plan.metrics.optimised_battery_used_pct,
    )
    print("\n".join(briefing(route)))

    if args.revoke:
        asyncio.run(revoke(_target(args.revoke, args.callsign), route))
        print(f"route deleted on {route.pilot_callsign}'s ATAK")
        return
    try:
        written = write_files(route, args.out)
    except ValueError as exc:
        raise SystemExit(f"not written: {exc}") from None
    for path in written:
        print(f"wrote {path}")
    if args.send:
        target = _target(args.send, args.callsign)
        sent = asyncio.run(deliver(target, route, pilot_uid=args.pilot_uid))
        chat = "with briefing chat" if sent.chat_sent else "no chat: pilot uid not seen"
        print(f"route {sent.route_uid} sent to {sent.pilot_callsign} ({chat})")
