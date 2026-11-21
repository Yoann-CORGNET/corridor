"""FastAPI application exposing the engine on ``/ws?callsign=...``.

The callsign says who connects; the scenario gives its role (``Scenario.role_of``). An unknown
callsign is closed with code ``4003``. Each client gets the snapshot and the events of its role
(``corridor.adapters.ws.audience``), and may only send commands as itself: no authentication,
but the filter would be decorative if any client could act under another callsign.

Route export over HTTP, for one mission (``mission_id``):

GET  /api/export/inav       INAV mission (MW XML) of the mission's current plan
GET  /api/export/ardupilot  ArduPilot mission (QGC WPL 110)
GET  /api/export/briefing   the pilot's briefing, plain text
POST /api/export/atak       send the route to the pilot's ATAK through OpenTAKServer

Every export takes ``altitude_m`` (above launch) and refuses a ``preview`` plan: it relies
on cuts nobody confirmed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import Field, ValidationError

from corridor.adapters.ws.audience import Audience, Viewer
from corridor.adapters.ws.hub import Hub, Outbox
from corridor.adapters.ws.schemas import Ack, ClientMessage, Error, MissionView, Snapshot
from corridor.domain.geo import Grid, Position
from corridor.domain.model import Model
from corridor.domain.rf.coverage import (
    ContourLevel,
    compute_js_heatmap_geojson,
    compute_polar_coverage,
    polar_coverage_to_geojson,
    probe_point_rf,
)
from corridor.domain.rf.dem import DemProvider, HillFeature, RasterDem, SyntheticDem
from corridor.domain.scenario import Scenario
from corridor.domain.zones import Zone, constraints_by_mission, nominal_zones
from corridor.engine.clock import SimClock
from corridor.engine.engine import CommandRejected, Engine
from corridor.engine.replan import Replanner
from corridor.export.atak import TakTarget, deliver
from corridor.export.firmware import ardupilot_waypoints, inav_mission
from corridor.export.route import DEFAULT_ALTITUDE_M, FlightRoute, briefing, flight_route

ReplannerFactory = Callable[[Scenario, dict[str, Grid]], Replanner]
"""Takes the planning grid of each mission, by mission id."""

TICK_S = 0.5
UNKNOWN_CALLSIGN = 4003
log = logging.getLogger(__name__)

Altitude = Annotated[float, Query(gt=0, le=1000, description="Metres above launch.")]
MissionId = Annotated[str, Query(description="Mission whose current plan is exported.")]
FileFormat = Literal["inav", "ardupilot", "briefing"]
EXPORT_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "Unknown mission."},
    409: {"description": "No plan yet, or the plan relies on unconfirmed cuts."},
}
ATAK_ERRORS: dict[int | str, dict[str, Any]] = {
    **EXPORT_ERRORS,
    502: {"description": "OpenTAKServer unreachable."},
    503: {"description": "No OpenTAKServer configured (--tak-host)."},
}


class AtakSent(Model):
    route_uid: str
    pilot_callsign: str
    chat_sent: bool = Field(description="False when the pilot's uid was not seen in time.")
    revision: int


def export_route(engine: Engine, mission_id: str, altitude_m: float) -> FlightRoute:
    runtime = engine.missions.get(mission_id)
    if runtime is None:
        raise HTTPException(404, f"unknown mission {mission_id!r}")
    plan = runtime.plan
    if plan is None:
        raise HTTPException(409, "no plan yet")
    if plan.status == "preview":
        raise HTTPException(409, "the plan relies on unconfirmed cuts")
    return flight_route(
        engine.scenario,
        runtime.mission,
        plan.optimised_route,
        altitude_m,
        cuts=list(engine.state.cuts.values()),
        status=plan.status,
        revision=plan.revision,
        battery_used_pct=plan.metrics.optimised_battery_used_pct,
    )


def export_file(route: FlightRoute, fmt: FileFormat) -> Response:
    stem = route.mission_id
    if fmt == "inav":
        content, name, media = inav_mission(route), f"{stem}.mission", "application/xml"
    elif fmt == "ardupilot":
        content, name, media = ardupilot_waypoints(route), f"{stem}.waypoints", "text/plain"
    else:
        content, name, media = "\n".join(briefing(route)) + "\n", f"{stem}.txt", "text/plain"
    headers = {"Content-Disposition": f'attachment; filename="{name}"'}
    return Response(content, media_type=media, headers=headers)


async def _tick_forever(engine: Engine) -> None:
    while True:
        try:
            engine.tick()
        except Exception:
            log.exception("engine tick failed")
        await asyncio.sleep(TICK_S)


def snapshot(engine: Engine, audience: Audience, viewer: Viewer, nominal: list[Zone]) -> Snapshot:
    scope = audience.scope(viewer)
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


def _find_dem_path(name: str) -> Path | None:
    for candidate in [
        Path("data") / name,
        Path("backend/data") / name,
        Path(__file__).resolve().parents[4] / "data" / name,
    ]:
        if candidate.exists():
            return candidate
    return None


def _select_dem(scenario: Scenario) -> DemProvider:
    launch_p = scenario.missions[0].launch
    ny = _find_dem_path("copernicus_newyork.tif")
    if 40.5 <= launch_p.lat <= 41.0 and ny:
        return RasterDem(ny)
    annecy = _find_dem_path("copernicus_annecy.tif")
    if 45.5 <= launch_p.lat <= 46.5 and annecy:
        return RasterDem(annecy)
    hk = _find_dem_path("copernicus_hongkong.tif")
    if 22.0 <= launch_p.lat <= 22.5 and hk:
        return RasterDem(hk)
    munich = _find_dem_path("copernicus_munich.tif")
    if munich:
        return RasterDem(munich)
    return SyntheticDem(
        base_elevation_m=520.0,
        hills=[
            HillFeature(
                center=Position(lat=48.160, lon=11.550),
                radius_m=1200.0,
                height_m=95.0,
            ),
            HillFeature(
                center=Position(lat=48.130, lon=11.580),
                radius_m=1000.0,
                height_m=80.0,
            ),
        ],
    )


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

    default_band = scenario.missions[0].band
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

    return features


def create_app(
    scenario: Scenario,
    clock_factor: float,
    cell_m: float,
    replanner: ReplannerFactory | None = None,
    tak: TakTarget | None = None,
) -> FastAPI:
    """Without ``replanner``, nothing is planned. The first plan is requested at startup.

    Without ``tak``, the ATAK export answers 503.
    """
    constraints = constraints_by_mission(scenario, cell_m)
    nominal = nominal_zones(scenario)
    grids = {mission: c.grid for mission, c in constraints.items()}
    planner = replanner(scenario, grids) if replanner else None
    engine = Engine(scenario, constraints, SimClock(clock_factor), planner)
    audience = Audience(scenario, constraints)
    hub = Hub(engine.clock.now, audience.sees)
    engine.subscribe(hub.publish)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        engine.request_plans()
        ticker = asyncio.create_task(_tick_forever(engine))
        yield
        ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker
        if planner:
            planner.close()

    app = FastAPI(title="Corridor", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/rf/coverage")
    async def get_rf_coverage(drone_h_agl_m: float = 20.0) -> dict[str, Any]:
        """Return terrain-aware RF coverage GeoJSON contours for scenario threats."""
        dem = _select_dem(scenario)
        features = _build_threat_coverage_features(dem, scenario, drone_h_agl_m)
        return {"type": "FeatureCollection", "features": features}

    @app.get("/api/rf/heatmap")
    async def get_rf_heatmap(
        mission_id: str | None = None,
        drone_h_agl_m: float = 20.0,
        step_m: float = 300.0,
        cells: bool = False,
    ) -> dict[str, Any]:
        """Return continuous J/S heatmap grid for GPU-accelerated heatmap rendering."""
        dem = _select_dem(scenario)
        m = scenario.mission(mission_id) if mission_id else scenario.missions[0]
        if m is None:
            m = scenario.missions[0]
        grid = grids.get(m.id) or next(iter(grids.values()))
        jammers: list[tuple[str, Position, float]] = [
            (j.id, j.pos, j.eirp_w) for j in scenario.friendly_jammers
        ]
        for t in scenario.enemy_threats:
            if t.type == "jammer":
                jammers.append((t.id, t.pos, t.eirp_w))

        # Calculate bounding box with 5km margin around scenario grid
        frame = grid.frame
        sw = frame.to_position(-5000.0, -5000.0)
        ne = frame.to_position(grid.cols * grid.cell_m + 5000.0, grid.rows * grid.cell_m + 5000.0)
        bounds = (sw.lat, sw.lon, ne.lat, ne.lon)

        return compute_js_heatmap_geojson(
            dem=dem,
            pilot_pos=m.launch,
            pilot_eirp_w=m.link_eirp_w,
            jammers=jammers,
            band_or_freq_hz=m.band,
            bounds=bounds,
            step_m=max(step_m, 250.0),
            drone_h_agl_m=drone_h_agl_m,
            cells=cells,
        )

    @app.get("/api/rf/probe")
    async def probe_rf(
        lat: float,
        lon: float,
        mission_id: str | None = None,
        drone_h_agl_m: float = 20.0,
    ) -> dict[str, Any]:
        """Probe real-time RF link budget & terrain shielding at a specific coordinate."""
        dem = _select_dem(scenario)
        m = scenario.mission(mission_id) if mission_id else scenario.missions[0]
        if m is None:
            m = scenario.missions[0]
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
        outbox = hub.connect(viewer, snapshot(engine, audience, viewer, nominal))
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

    @app.get("/api/export/{fmt}", responses=EXPORT_ERRORS)
    def get_export(
        fmt: FileFormat, mission_id: MissionId, altitude_m: Altitude = DEFAULT_ALTITUDE_M
    ) -> Response:
        return export_file(export_route(engine, mission_id, altitude_m), fmt)

    @app.post("/api/export/atak", responses=ATAK_ERRORS)
    async def post_atak(
        mission_id: MissionId, altitude_m: Altitude = DEFAULT_ALTITUDE_M
    ) -> AtakSent:
        if tak is None:
            raise HTTPException(503, "no OpenTAKServer: start corridor-server with --tak-host")
        route = export_route(engine, mission_id, altitude_m)
        try:
            sent = await deliver(tak, route)
        except OSError as exc:
            raise HTTPException(502, f"OpenTAKServer {tak.host}:{tak.port}: {exc}") from exc
        engine.notify(
            route.pilot_callsign,
            f"Route rev {route.revision} sent to your ATAK: take-off not before "
            f"{route.label(route.takeoff_s, up=True)}.",
        )
        assert route.revision is not None
        return AtakSent(
            route_uid=sent.route_uid,
            pilot_callsign=sent.pilot_callsign,
            chat_sent=sent.chat_sent,
            revision=route.revision,
        )

    return app
