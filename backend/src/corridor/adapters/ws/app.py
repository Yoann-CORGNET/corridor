"""FastAPI application exposing the engine on ``/ws?callsign=...``.

The callsign says who connects; the scenario gives its role (``Scenario.role_of``). An unknown
callsign is closed with code ``4003``. Each client gets the snapshot and the events of its role
(``corridor.adapters.ws.audience``), and may only send commands as itself: no authentication,
but the filter would be decorative if any client could act under another callsign.

A ``mission.created`` event is followed by a new snapshot for every client that sees it: the
mission may bring jammers, threats and cuts into view that the client never received. A
``mission.updated`` one is followed by a new snapshot for every client: the mission may also
leave a view (an operator whose jammer it no longer crosses).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import math
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import ValidationError

from corridor.adapters.ws.audience import Audience, Viewer
from corridor.adapters.ws.hub import Hub, Outbox
from corridor.adapters.ws.outlines import level_outlines
from corridor.adapters.ws.schemas import Ack, ClientMessage, Error, MissionView, Snapshot
from corridor.domain.events import (
    Event,
    MissionCreated,
    MissionUpdated,
    RelaySwitched,
    ThreatAdded,
    ThreatRemoved,
)
from corridor.domain.geo import LocalFrame, Position
from corridor.domain.heatmap import heatmap_png as mission_heatmap_png
from corridor.domain.heatmap import mission_heatmap
from corridor.domain.rf.coverage import (
    ContourLevel,
    compute_js_heatmap,
    compute_polar_coverage,
    heatmap_png,
    polar_coverage_to_geojson,
    probe_point_rf,
)
from corridor.domain.rf.dem import DemProvider
from corridor.domain.scenario import EnemyRadar, Scenario
from corridor.domain.terrain import dem_path, ground_model
from corridor.domain.zones import nominal_zones
from corridor.engine.clock import SimClock
from corridor.engine.engine import CommandRejected, Engine, MissionRuntime
from corridor.engine.replan import Replanner

ReplannerFactory = Callable[[Scenario], Replanner]

TICK_S = 0.5
UNKNOWN_CALLSIGN = 4003
log = logging.getLogger(__name__)


async def _tick_forever(engine: Engine) -> None:
    while True:
        try:
            engine.tick()
        except Exception:
            log.exception("engine tick failed")
        await asyncio.sleep(TICK_S)


def snapshot(engine: Engine, audience: Audience, viewer: Viewer) -> Snapshot:
    """Nominal zones come from the current threats: the command adds and removes them."""
    scope = audience.scope(viewer)
    nominal = nominal_zones(engine.scenario)
    return Snapshot(
        viewer=viewer,
        scenario=audience.scenario_for(viewer),
        nominal_zones=audience.nominal_zones_for(viewer, nominal),
        missions=[
            MissionView(
                constraints=runtime.constraints,
                status=engine.state.missions[mission_id],
                plan=runtime.plan,
                drone=runtime.drone,
                request=runtime.request,
            )
            for mission_id, runtime in engine.missions.items()
            if mission_id in scope.missions
        ],
        clock=engine.clock.state(),
        cuts=[c for c in engine.state.cuts.values() if c.jammer in scope.jammers],
    )


def handle_message(raw: str, viewer: Viewer, engine: Engine, hub: Hub, outbox: Outbox) -> None:
    try:
        message = ClientMessage.model_validate_json(raw)
    except ValidationError as exc:
        hub.reply(outbox, Error(req_id=None, reason=str(exc)))
        return
    if message.command.actor != viewer.callsign:
        reason = f"connected as {viewer.callsign}, cannot act as {message.command.actor}"
        hub.reply(outbox, Error(req_id=message.req_id, reason=reason))
        return
    try:
        engine.submit(message.command)
    except CommandRejected as exc:
        hub.reply(outbox, Error(req_id=message.req_id, reason=str(exc)))
        return
    hub.reply(outbox, Ack(req_id=message.req_id))


async def _pump(websocket: WebSocket, outbox: Outbox) -> None:
    while True:
        await websocket.send_text(await outbox.get())


def _select_dem(scenario: Scenario) -> DemProvider:
    """The scenario's elevation file, or flat ground (``corridor.domain.terrain``)."""
    return ground_model(scenario.terrain)


def _build_threat_coverage_features(
    dem: DemProvider,
    scenario: Scenario,
    drone_h_agl_m: float,
) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    friendly_contours = [
        ContourLevel(-70.0, "Friendly Denied (-70 dBm)", "#e03131", 0.32, "#c92a2a", 2),
        ContourLevel(-85.0, "Friendly Contention (-85 dBm)", "#f08c00", 0.20, "#e8590c", 2),
        ContourLevel(-100.0, "Friendly Fringe (-100 dBm)", "#fab005", 0.10, "#f59f00", 1),
    ]
    enemy_contours = [
        ContourLevel(-70.0, "Enemy Threat (-70 dBm)", "#9c36b5", 0.32, "#862e9c", 2),
        ContourLevel(-85.0, "Enemy Contention (-85 dBm)", "#ae3ec9", 0.20, "#9c36b5", 2),
        ContourLevel(-100.0, "Enemy Fringe (-100 dBm)", "#cc5de8", 0.10, "#ae3ec9", 1),
    ]

    default_band = scenario.pilots[0].drone.band
    for jammer in scenario.friendly_jammers:
        cov = compute_polar_coverage(
            dem=dem,
            tx_pos=jammer.pos,
            tx_eirp_w=jammer.eirp_w,
            band_or_freq_hz=default_band,
            rx_h_agl_m=drone_h_agl_m,
            max_range_m=5000.0,
            azimuth_step_deg=10.0,
        )
        gj = polar_coverage_to_geojson(cov, contours=friendly_contours)
        for f in gj["features"]:
            props = dict(f.get("properties", {}))
            props["threat_id"] = jammer.id
            props["threat_kind"] = "friendly_jammer"
            f["properties"] = props
            features.append(f)

    for threat in scenario.enemy_threats:
        if threat.type == "jammer":
            cov = compute_polar_coverage(
                dem=dem,
                tx_pos=threat.pos,
                tx_eirp_w=threat.eirp_w,
                band_or_freq_hz=default_band,
                rx_h_agl_m=drone_h_agl_m,
                max_range_m=5000.0,
                azimuth_step_deg=10.0,
            )
            gj = polar_coverage_to_geojson(cov, contours=enemy_contours)
            for f in gj["features"]:
                props = dict(f.get("properties", {}))
                props["threat_id"] = threat.id
                props["threat_kind"] = "enemy_jammer"
                f["properties"] = props
                features.append(f)
        elif isinstance(threat, EnemyRadar):
            features.append(_radar_coverage_feature(threat))

    return features


def _radar_coverage_feature(radar: EnemyRadar) -> dict[str, Any]:
    """The radar's disc, its range widened by its position error, as the heatmap and the
    planner see it. Masking it by the terrain comes with the planner (open decision)."""
    reach_m = radar.radius_m + radar.position_error_m
    frame = LocalFrame(radar.pos)
    ring = [
        frame.to_position(reach_m * math.cos(a), reach_m * math.sin(a))
        for a in (2 * math.pi * k / 64 for k in range(64))
    ]
    coords = [[p.lon, p.lat] for p in [*ring, ring[0]]]
    return {
        "type": "Feature",
        "properties": {
            "name": "Radar coverage",
            "fill": "#fd7e14",
            "fill_opacity": 0.2,
            "stroke": "#e8590c",
            "stroke_width": 2,
            "threat_id": radar.id,
            "threat_kind": "radar",
        },
        "geometry": {"type": "Polygon", "coordinates": [coords]},
    }


def create_app(
    scenario: Scenario,
    clock_factor: float,
    cell_m: float,
    replanner: ReplannerFactory | None = None,
) -> FastAPI:
    """Without ``replanner``, nothing is planned. The first plan is requested at startup."""
    planner = replanner(scenario) if replanner else None
    engine = Engine(scenario, cell_m, SimClock(clock_factor), planner)
    audience = Audience(engine)
    hub = Hub(engine.clock.now, audience.sees)

    def on_event(event: Event) -> None:
        hub.publish(event)

        def snapshot_for(viewer: Viewer) -> Snapshot:
            return snapshot(engine, audience, viewer)

        if isinstance(event, MissionCreated | ThreatAdded | ThreatRemoved | RelaySwitched):
            hub.resync(snapshot_for, lambda viewer: audience.sees(viewer, event))
        elif isinstance(event, MissionUpdated):
            hub.resync(snapshot_for, lambda _: True)

    engine.subscribe(on_event)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        engine.request_plans()
        ticker = asyncio.create_task(_tick_forever(engine))
        try:
            yield
        finally:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker
            if planner:
                planner.close()

    app = FastAPI(title="Corridor", lifespan=lifespan)

    def runtime_for(mission_id: str | None) -> MissionRuntime:
        """The mission asked for, else the first one; 404 when there is none yet."""
        runtime = engine.missions.get(mission_id or "") or next(
            iter(engine.missions.values()), None
        )
        if runtime is None:
            raise HTTPException(status_code=404, detail="no mission yet")
        return runtime

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/zones3d", responses={404: {"description": "Unknown mission."}})
    def get_zones3d(mission_id: str) -> dict[str, Any]:
        """3D only: the cells each jammer and radar forbids at each flight level for the
        mission, as the planner sees them, as GeoJSON outlines; empty in 2D."""
        runtime = engine.missions.get(mission_id)
        if runtime is None:
            raise HTTPException(404, "unknown mission")
        return level_outlines(engine.scenario, runtime.mission, runtime.constraints.grid)

    @app.get("/api/buildings")
    def get_buildings() -> Response:
        """Building footprints of the scenario with ``height_m``, for the map (GeoJSON); an
        empty collection without buildings."""
        terrain = engine.scenario.terrain
        if terrain.buildings is None:
            return JSONResponse({"type": "FeatureCollection", "features": []})
        return FileResponse(dem_path(terrain.buildings), media_type="application/geo+json")

    @app.get("/api/rf/coverage")
    async def get_rf_coverage(drone_h_agl_m: float = 20.0) -> dict[str, Any]:
        """Return terrain-aware RF coverage GeoJSON contours for scenario threats."""
        dem = _select_dem(engine.scenario)
        features = _build_threat_coverage_features(dem, engine.scenario, drone_h_agl_m)
        return {"type": "FeatureCollection", "features": features}

    heatmap_cache: dict[str, dict[str, Any]] = {}

    @app.get("/api/rf/heatmap")
    async def get_rf_heatmap(
        mission_id: str | None = None,
        drone_h_agl_m: float = 20.0,
        step_m: float = 50.0,
    ) -> dict[str, Any]:
        """J/S heatmap as a PNG data URL with the corners it drapes on (MapLibre image source).

        Computed once per configuration: the jammers, the pilot link, the terrain and the box.
        """
        current = engine.scenario
        runtime = runtime_for(mission_id)
        m, grid = runtime.mission, runtime.constraints.grid
        jammers: list[tuple[str, Position, float]] = [
            (j.id, j.pos, j.eirp_w) for j in current.friendly_jammers
        ]
        for t in current.enemy_threats:
            if t.type == "jammer":
                jammers.append((t.id, t.pos, t.eirp_w))

        # Bounding box with 5 km margin around the mission grid
        frame = grid.frame
        sw = frame.to_position(-5000.0, -5000.0)
        ne = frame.to_position(grid.cols * grid.cell_m + 5000.0, grid.rows * grid.cell_m + 5000.0)
        bounds = (sw.lat, sw.lon, ne.lat, ne.lon)
        step = max(step_m, 50.0)

        key = repr(
            (current.terrain, jammers, m.launch, m.link_eirp_w, m.band, bounds, step, drone_h_agl_m)
        )
        cached = heatmap_cache.get(key)
        if cached is not None:
            return cached

        def render() -> dict[str, Any]:
            heatmap = compute_js_heatmap(
                dem=_select_dem(current),
                pilot_pos=m.launch,
                pilot_eirp_w=m.link_eirp_w,
                jammers=jammers,
                band_or_freq_hz=m.band,
                bounds=bounds,
                step_m=step,
                drone_h_agl_m=drone_h_agl_m,
            )
            png = base64.b64encode(heatmap_png(heatmap)).decode("ascii")
            w, s, e, n = heatmap.west, heatmap.south, heatmap.east, heatmap.north
            return {
                "image": f"data:image/png;base64,{png}",
                "coordinates": [[w, n], [e, n], [e, s], [w, s]],
                "step_m": step,
            }

        result = await asyncio.to_thread(render)
        if len(heatmap_cache) >= 16:
            heatmap_cache.clear()
        heatmap_cache[key] = result
        return result

    mission_heatmap_cache: dict[str, dict[str, Any]] = {}

    @app.get(
        "/api/heatmap",
        responses={404: {"description": "No mission."}, 422: {"description": "Unknown level."}},
    )
    async def get_mission_heatmap(
        mission_id: str | None = None, level: int = 0, off: str = ""
    ) -> dict[str, Any]:
        """J/S of the mission at one flight level (index, lowest first; 0 in 2D), on its
        planning grid, as a PNG data URL with the corners it drapes on. The denied cells are
        the ones the planner forbids (``corridor.domain.heatmap``).

        ``off``: comma-separated friendly jammers whose cut is running at the instant shown.
        """
        current = engine.scenario
        runtime = runtime_for(mission_id)
        m, grid = runtime.mission, runtime.constraints.grid
        levels = len(current.terrain.levels_m) if current.terrain.mode == "3d" else 1
        if not 0 <= level < levels:
            raise HTTPException(422, f"level must be in [0, {levels})")
        jammers_off = frozenset(j for j in off.split(",") if j)
        key = repr(
            (
                current.terrain,
                current.friendly_jammers,
                current.enemy_threats,
                current.relays,
                current.js_threshold_db,
                m,
                grid,
                sorted(jammers_off),
                level,
            )
        )
        cached = mission_heatmap_cache.get(key)
        if cached is not None:
            return cached

        def render() -> dict[str, Any]:
            heatmap = mission_heatmap(current, m, grid, jammers_off)
            png = mission_heatmap_png(heatmap, level, current.js_threshold_db)
            sw = grid.origin
            ne = grid.to_position(grid.cols * grid.cell_m, grid.rows * grid.cell_m)
            w, s, e, n = sw.lon, sw.lat, ne.lon, ne.lat
            return {
                "image": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
                "coordinates": [[w, n], [e, n], [e, s], [w, s]],
            }

        result = await asyncio.to_thread(render)
        if len(mission_heatmap_cache) >= 64:
            mission_heatmap_cache.clear()
        mission_heatmap_cache[key] = result
        return result

    @app.get("/api/rf/probe")
    async def probe_rf(
        lat: float,
        lon: float,
        mission_id: str | None = None,
        drone_h_agl_m: float = 20.0,
    ) -> dict[str, Any]:
        """Probe real-time RF link budget & terrain shielding at a specific coordinate."""
        dem = _select_dem(engine.scenario)
        m = runtime_for(mission_id).mission
        jammers: list[tuple[str, Position, float]] = [
            (j.id, j.pos, j.eirp_w) for j in scenario.friendly_jammers
        ]
        for t in scenario.enemy_threats:
            if t.type == "jammer":
                jammers.append((t.id, t.pos, t.eirp_w))

        return probe_point_rf(
            dem=dem,
            target_pos=Position(lat=lat, lon=lon),
            pilot_pos=m.launch,
            pilot_eirp_w=m.link_eirp_w,
            jammers=jammers,
            band_or_freq_hz=m.band,
            drone_h_agl_m=drone_h_agl_m,
        )

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket, callsign: str = "") -> None:
        await websocket.accept()
        viewer = audience.viewer(callsign)
        if viewer is None:
            await websocket.close(code=UNKNOWN_CALLSIGN, reason=f"unknown callsign {callsign!r}")
            return
        outbox = hub.connect(viewer, snapshot(engine, audience, viewer))
        sender = asyncio.create_task(_pump(websocket, outbox))
        try:
            while True:
                handle_message(await websocket.receive_text(), viewer, engine, hub, outbox)
        except WebSocketDisconnect:
            pass
        finally:
            hub.disconnect(outbox)
            sender.cancel()
            # The pump may already have died sending to a closed socket: nothing to report.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender

    return app
