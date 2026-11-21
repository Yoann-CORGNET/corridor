"""``corridor-server``: run the engine and its WebSocket endpoint."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from corridor.adapters.ws.app import create_app
from corridor.domain.scenario import load_scenario
from corridor.engine.replan import ProcessReplanner
from corridor.export.atak import TakTarget


def main() -> None:
    parser = argparse.ArgumentParser(prog="corridor-server")
    parser.add_argument("--scenario", type=Path, default=Path("scenarios/demo.json"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--clock-factor", type=float, default=20.0, help="simulated seconds per real second"
    )
    parser.add_argument("--cell-m", type=float, default=100.0, help="planning grid cell size")
    parser.add_argument("--tak-host", help="OpenTAKServer, to send approved routes to ATAK")
    parser.add_argument("--tak-port", type=int, default=8088, help="CoT TCP port")
    parser.add_argument("--tak-callsign", default="CORRIDOR", help="our callsign on TAK")
    args = parser.parse_args()
    scenario = load_scenario(args.scenario)
    tak = TakTarget(args.tak_host, args.tak_port, args.tak_callsign) if args.tak_host else None
    app = create_app(scenario, args.clock_factor, args.cell_m, ProcessReplanner, tak)
    uvicorn.run(app, host=args.host, port=args.port)
