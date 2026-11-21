# Corridor

4D drone route planning synchronised with the friendly EW schedule (idea #1 in
`../edth_ideas.xlsx`, reworked). The planner computes the route, in space and time, that reaches
the target while avoiding radars and jamming zones and keeping the radio link with the pilot.
When a friendly jammer has a scheduled cut, the planner recomputes a more direct route that
crosses the zone during the cut, and notifies the pilot and the jammer operator. Goal: save the
battery a detour would cost.

Full description of the feature (users, behaviour, safety rules, demo story, open questions):
[`docs/feature.md`](docs/feature.md).

No ATAK plugin: the TAK SDK is not available, so Corridor runs as an external service that
speaks CoT (Cursor on Target) to an OpenTAKServer (OTS). ATAK clients connected to the same
OTS see zones, routes and the drone as regular shapes, and what ATAK sends (shapes, chat,
positions) comes back to Corridor.

## Architecture

```
web front end ◄──WebSocket /ws──► backend (Python, one process: FastAPI + uvicorn)
(planning screen)                   ├─ engine: world state, simulation clock, safety rules
                                    ├─ planner: pure computation, runs off the event loop (to write)
                                    ├─ WebSocket adapter: commands in, events out
                                    └─ TAK adapter (later) ◄──CoT, TCP 8088──► OpenTAKServer ◄──► ATAK (Android)
```

- The engine is the single owner of state: scenario, cuts, mission status, simulation time.
  Adapters only send it commands and receive its events. The web front end and ATAK are two
  views of it.
- Planner: pure function (no state, no clock), so it can run in a process pool. J/S jamming
  model and planner design in [docs/architecture.md](docs/architecture.md).
- TAK adapter: will publish zones, routes, drone and notifications as CoT, and turn ATAK chat
  (`ACK`) into commands. The CoT client (`corridor.tak`) works standalone but is not wired to the
  engine yet.
- OTS is only the relay to ATAK. The web front end never talks to OTS directly.

Core architecture, module layout and WebSocket protocol: [docs/architecture.md](docs/architecture.md).

Status:
- Backend core: scenario loading and checks, J/S zones, planning grid, cut scheduling /
  confirmation / cancellation, mission launch, accelerated clock, WebSocket server, its JSON
  Schema, tests.
- Planner (masks, SIPP, takeoff sweep, statuses and metrics) running in a worker process and
  emitting `plan.updated`.
- Flight: simulated drone (`drone.state`), launch only on a flyable plan, safety rule (late
  drone or cancelled cut -> fallback route from the drone position), timed jammer restarts
  (`jammer.restarted`), `inject.delay` demo twist.
- TAK side: OTS stack, CoT client and demo shapes (sections below). Not wired to the engine yet,
  except the route export.
- Route export: the current plan as an ATAK route sent to the pilot only, an INAV mission and an
  ArduPilot mission (section below).
- Front end: first draft in `frontend/` (map, cuts, notifications, timeline, live drone).

## Front end

Planning screen used for the demo; ATAK stays the field view. Lives in `frontend/` (first draft:
what works and how to run it in [`frontend/README.md`](frontend/README.md)).

| Layer | Choice | Why |
|---|---|---|
| UI | React + Vite + TypeScript | Known by the team, typed contract with the backend |
| Components | Mantine | Panels, buttons, notifications, dark theme without CSS work |
| Map | MapLibre GL JS, used directly (no wrapper library) | GeoJSON layers updated in place, smooth animation, offline tiles |
| Basemap | Online tiles + an offline PMTiles extract of the demo area | The map keeps working without venue Wi-Fi |
| Timeline | vis-timeline | Draggable cut windows and a moving time cursor out of the box |
| Client state | Zustand | One small store, fed by the WebSocket |

Layout:

- Map, about 70% of the screen: jamming zones (red = active, green = cut, purple = enemy),
  radars (orange), baseline route (grey, dashed), planned route, animated drone, ATAK users'
  positions.
- Right panel: mission card (distance, time, battery vs detour), route options, approve button,
  notification feed (pilot and EW operator).
- Bottom: timeline, one row per jammer with its cut windows, plus one row for the drone showing
  when it crosses each zone. Scrubbing the timeline updates the map.

Rules:

- All map code sits in one framework-free module, `frontend/src/map/mapView.ts` (`setZones`,
  `setRoutes`, `setDrone`, `setPhones`, ...). React components only create the map and call it.
- The front end holds no business state: commands go to the backend and state comes back over
  the same WebSocket (protocol in [docs/architecture.md](docs/architecture.md)). On (re)connect the backend sends the full state, so a page reload or a
  dropped connection loses nothing.
- No internet needed at runtime: fonts, scripts and basemap are bundled.
- Message types are generated from `schema/ws.schema.json` (see "Contract" in
  [docs/architecture.md](docs/architecture.md)), never written by hand.
- Demo build: `npm run build`, then the backend serves `frontend/dist/` as static files. One process,
  one port, no Node on the demo laptop. The static mount is not in `adapters/ws/app.py` yet.
- Demo controls: scenario picker, start / pause / speed, reset, a keyboard shortcut per demo
  step.

Repository layout:

```
backend/    Python project (uv): engine, WebSocket server, TAK client, tests, scenarios
schema/     WebSocket protocol JSON Schema, generated by the backend, consumed by the front end
infra/ots/  OpenTAKServer stack (Docker compose)
docs/       architecture.md (core, protocol, planner design), feature.md (functional spec)
resources/  research notes, standalone planner prototype, old state of the art (reference only)
frontend/   front end (React + Vite), first draft
```

## Corridor server

Python commands run from `backend/`.

```bash
cd backend
uv sync
uv run corridor-server            # ws://127.0.0.1:8000/ws?callsign=..., scenarios/demo.json, clock x20, paused
uv run corridor-server --host 0.0.0.0 --port 8000 --clock-factor 30 --cell-m 200 \
  --scenario scenarios/other.json
uv run corridor-schema > ../schema/ws.schema.json   # after changing a message model
```

- A client connects with its callsign (`/ws?callsign=CMD`, `PILOT-1`, `EW-J3`...). It gets a
  `snapshot` and the events its role may see (command: everything; pilot: its mission; jammer
  operator: its jammers and the missions they concern). A reconnect gets a fresh snapshot.
- The simulation clock starts paused at t = 0; the front starts it with a `clock.set` command.
- Demo: with the demo powers, J3's zone takes about 300 s to cross. A cut shorter than about
  7 min (crossing + 30 s + 60 s of margins) cannot give the straight route.
- Late-drone twist: launch on an approved plan, start the clock, then send `inject.delay`
  (front: "Hold drone 120 s") before the drone reaches J3: it switches to a fallback route.
- State lives in memory: restart the server to reset the scenario.
- `--host 0.0.0.0` exposes the server on the LAN (another laptop, a phone browser).
- A test fails when `schema/ws.schema.json` is out of date: regenerate it with `corridor-schema`.
- `--tak-host <ip>` (and `--tak-port`, default 8088): OpenTAKServer the route export sends to.

## Route export

One flight route (waypoints with altitude and time constraints, `corridor.export.route`), several
destinations. Only a flyable plan is exported: a `preview` plan relies on cuts nobody confirmed.

| Destination | What | Time constraints |
|---|---|---|
| ATAK | native route (`b-m-r`), addressed to the pilot's callsign only, plus the briefing as a GeoChat DM | in the waypoint names: `T/O 14:12:35Z-14:14:00Z`, `WP3 100m HOLD til 14:20:10Z`, `TGT 100m 14:21:37Z` |
| INAV | MW XML `.mission` (INAV Configurator, mwp, ezgui) | none in the firmware: take off on time; a hold is a fixed `POSHOLD_TIME` |
| ArduPilot | QGC WPL 110 `.waypoints` (Mission Planner, QGroundControl) | `NAV_DELAY` until a UTC time: take-off and every hold departure are "not before" times |

- Need to know: the pilot gets the route and its times, never a zone or a jammer.
- Take-off window: when the route crosses a friendly jammer during a cut, a late take-off is as
  unsafe as an early one. The latest take-off comes from the flight safety rule (cut margins kept).
- Altitude: the planner is 2D. Every waypoint gets the export altitude (default 100 m above
  launch). `Waypoint.alt_m` is per waypoint for a later 3D planner.
- Both firmwares navigate on GNSS: no use if friendly jamming covers GNSS bands.
- ArduPilot's `NAV_DELAY` needs GPS time and is reported to overrun by up to about a minute: test
  on the target vehicle.

From the running server (front: "Export route" card, for the selected mission; Vite proxies
`/api`). `mission_id` is required:

```bash
curl -OJ "http://127.0.0.1:8000/api/export/inav?mission_id=M1&altitude_m=80"   # M1.mission
curl -OJ "http://127.0.0.1:8000/api/export/ardupilot?mission_id=M1"             # M1.waypoints
curl "http://127.0.0.1:8000/api/export/briefing?mission_id=M1"
curl -X POST "http://127.0.0.1:8000/api/export/atak?mission_id=M1"               # needs --tak-host
```

Offline, without the server (cuts given with `--cut` count as confirmed), files in `export/`:

```bash
uv run corridor-export --cut J3:900:1380
uv run corridor-export --cut J3:900:1380 --altitude 80 --send 192.168.1.20   # also to ATAK
uv run corridor-export --revoke 192.168.1.20                                  # delete it on ATAK
uv run corridor-export --mission M2 --cut J4:900:1380                         # another mission
```

Before the demo, on the real OTS and phone:

1. `uv run corridor-tak route-smoke`: a route addressed to one of two clients must reach only it
   (OTS `<marti><dest>` routing, read in the OTS source, never run on 1.8.3).
2. Draw a route in ATAK, capture it (`corridor-tak listen --capture captures/`) and align
   `corridor.export.atak.route_cot` on it, as for shapes.
3. Open `M1.mission` in INAV Configurator (Mission Control tab, Load mission file) to show it.

## Requirements

- Docker with the compose plugin, daemon running (`sudo systemctl enable --now docker`, user in
  the `docker` group)
- uv (installs Python 3.12 if missing)
- Node.js LTS (front end development only)

## Start OTS

```bash
cd infra/ots
cp ots_config.env.example ots_config.env
docker compose up -d
docker compose ps        # ots must become "healthy" (about 1 minute on first start)
docker compose logs -f ots
```

`infra/ots/compose.yml` is a trimmed copy of `brian7704/OpenTAKServer-Docker@30aa20b`. The header
of the file lists what was removed and why (no web UI, no video for now).

Images are pinned by version tag and digest: OTS 1.8.3 (2026-11-11). Do not use upstream
`:master`: it is a stale 1.5.14 build from 2025-10 that loses RabbitMQ bindings at random.
To upgrade: change the version tag in `compose.yml`, `docker compose pull`, run the smoke test
(several times, including after `docker compose up -d --force-recreate`), then re-pin with:

```bash
for img in $(docker compose config --images); do
  docker image inspect --format '{{index .RepoDigests 0}}' "$img"
done
```

First start takes 1 to 2 minutes (CA creation, database migrations). The one-shot `ots-init`
service gives the OTS user write access to the data volume, then exits.

The OTS API is published on `127.0.0.1:8081` only: `curl http://127.0.0.1:8081/api/health`.

Reset everything (deletes the OTS database and CA): `docker compose down -v`.

## TAK CLI (`corridor-tak`)

```bash
cd backend
uv sync
uv run corridor-tak smoke     # two connections through OTS, exit 0 if demo shapes are routed
uv run corridor-tak demo      # publish a test jam zone + corridor, refreshed every 30 s
uv run corridor-tak listen    # print every CoT routed to us
uv run corridor-tak listen --capture captures/   # also save raw XML
uv run corridor-tak route-smoke   # route addressed to one of two clients: exit 0 if only it gets it
```

Options: `--host` (default 127.0.0.1), `--port` (8088), `--lat` / `--lon` (demo position).

## Connect ATAK

1. Phone and laptop on the same network (Wi-Fi LAN, or the same Tailscale tailnet).
2. If a firewall runs on the laptop, open TCP 8088.
3. ATAK: Settings > Network Preferences > TAK Servers > Add. Address: laptop IP, port 8088,
   protocol TCP, no SSL.
4. From `backend/`, run `uv run corridor-tak demo --lat <lat> --lon <lon>` near where the map is centered: a red
   jam zone, a blue corridor crossing it and an EW-1 marker appear.

## Shape format: check against a real capture

`backend/src/corridor/tak/cot.py` builds shapes in the layout ATAK uses for drawn shapes (`u-d-f`, one
`<link point>` per vertex, signed ARGB colours). It was written from known ATAK output, not from
a schema. Before building on it:

1. From `backend/`: `uv run corridor-tak listen --capture captures/`
2. In ATAK, draw a polygon and a line and give them a name.
3. Compare `captures/*u-d-*.xml` with what `shape()` produces and align.

## OTS notes (checked in the running OTS 1.8.3 code)

- A TCP client is registered, and only then receives other clients' CoT, after it sends an
  event carrying `<contact>`. `TakClient` sends a self SA on connect and every 30 s.
- Race in OTS: if that first SA arrives before the handler's RabbitMQ channel is open, OTS
  records the client but never binds its queues. The client can send but receives nothing,
  and nothing is logged. `TakClient` waits 1 s (`CONNECT_SETTLE_S`) before its first send
  (smoke test: 3/3 failures without the delay, 6/6 passes with it). ATAK can hit the same
  race: if it shows its own position but none of Corridor's shapes, disconnect and reconnect
  the server in ATAK.
- OTS does not echo a client's own CoT back to it.
- The OTS CA is created on first start; default admin account: `administrator` / `password`.
- Every CoT is also published to the RabbitMQ `firehose` exchange: a later option for
  consuming everything without a TCP connection.

## Development

```bash
cd backend
uv run pytest
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy
```
