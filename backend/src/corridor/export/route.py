"""Flight route: a plan's route as waypoints with altitude and time constraints.

Every exporter (ATAK, INAV, ArduPilot) reads this, never the planner output directly.

The planner works in 2D, on one altitude layer: every waypoint gets the export altitude,
in metres above the launch point. ``Waypoint.alt_m`` exists per waypoint so a 3D planner
can fill it later without touching the exporters.

Times are simulated seconds, like the rest of the domain. ``FlightRoute.label`` turns them
into wall-clock text. A "not before" time (take-off, leaving a hold) is rounded up, so
following it to the second never means leaving early.

A route that crosses a friendly jammer zone during a cut is also unsafe if it leaves late:
``takeoff_latest_s`` is the last take-off that keeps every crossing inside its cut, with the
planner's margins (checked with the flight safety rule).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from corridor.domain.events import PlanStatus, RoutePoint
from corridor.domain.geo import LocalFrame, Position
from corridor.domain.safety import Flight, schedule_violation
from corridor.domain.scenario import Mission, Scenario
from corridor.domain.state import Cut
from corridor.domain.zones import mission_shapes

DEFAULT_ALTITUDE_M = 100.0
LATEST_STEP_S = 5.0
LATEST_HORIZON_S = 900.0
"""Beyond this much slack, the take-off is treated as having no latest time."""
MERGE_M = 1.0
"""Waypoints closer than this to the previous one are dropped (launch on a cell centre)."""

WaypointKind = Literal["waypoint", "hold", "target"]


@dataclass(frozen=True)
class Waypoint:
    number: int
    """1-based, in flight order. The start point is not a waypoint."""
    lat: float
    lon: float
    alt_m: float
    """Above the launch point."""
    arrive_s: float
    depart_s: float
    """Equals ``arrive_s`` except for a hold, which must not be left before it."""
    kind: WaypointKind

    @property
    def hold_s(self) -> float:
        return self.depart_s - self.arrive_s


@dataclass(frozen=True)
class FlightRoute:
    mission_id: str
    drone_id: str
    pilot_callsign: str
    start_time: datetime
    """Wall-clock time of simulated t = 0 (timezone-aware)."""
    launch: Position
    """Home point for the flight controller."""
    start: Position
    """Where the route begins: the launch point, or the drone's position for a fallback."""
    takeoff_s: float
    """Not before. For an airborne route, the time the route starts from ``start``."""
    takeoff_latest_s: float | None
    """Not after; None when a late take-off breaks no cut window (or no cut was given)."""
    airborne: bool
    """The drone is already flying (fallback route): no take-off item."""
    altitude_m: float
    cruise_speed_mps: float
    waypoints: tuple[Waypoint, ...]
    status: PlanStatus | None = None
    revision: int | None = None
    battery_used_pct: float | None = None

    @property
    def arrival_s(self) -> float:
        return self.waypoints[-1].arrive_s

    def wall(self, t: float, *, up: bool = False) -> datetime:
        """Wall-clock time of ``t``, to the second. ``up``: round up (a "not before")."""
        return self.start_time + timedelta(seconds=math.ceil(t) if up else round(t))

    def label(self, t: float, *, up: bool = False) -> str:
        """``HH:MM:SS`` in the scenario's timezone, with ``Z`` when that is UTC."""
        wall = self.wall(t, up=up)
        return wall.strftime("%H:%M:%S") + ("Z" if wall.utcoffset() == timedelta(0) else "")

    @property
    def title(self) -> str:
        return f"{self.mission_id} {self.drone_id}"


def latest_takeoff_s(
    scenario: Scenario,
    mission: Mission,
    route: Sequence[RoutePoint],
    takeoff_s: float,
    cuts: Sequence[Cut],
) -> float | None:
    """Last take-off keeping the route inside its confirmed cuts, to the second, or None."""
    if not any(c.status == "confirmed" for c in cuts):
        return None
    frame, discs = LocalFrame(mission.launch), mission_shapes(scenario, mission)
    points = list(route)
    ok = 0.0
    delay = LATEST_STEP_S
    while delay <= LATEST_HORIZON_S:
        flight = Flight(route=points, revision=0, delay_s=delay)
        if schedule_violation(flight, takeoff_s + delay, frame, discs, cuts):
            return math.floor(takeoff_s + ok)
        ok, delay = delay, delay + LATEST_STEP_S
    return None


def flight_route(
    scenario: Scenario,
    mission: Mission,
    route: Sequence[RoutePoint],
    altitude_m: float = DEFAULT_ALTITUDE_M,
    *,
    cuts: Sequence[Cut] = (),
    status: PlanStatus | None = None,
    revision: int | None = None,
    battery_used_pct: float | None = None,
) -> FlightRoute:
    """Waypoints of ``route``: ground wait dropped, each hover pair merged into a hold.

    ``cuts``: the cuts the route was planned on, to compute the latest take-off.

    The planner writes a hover as two points at the same place: the arrival (``hover``) and
    the departure (``fly``). The last point is the target.
    """
    if altitude_m <= 0:
        raise ValueError("altitude_m must be positive")
    points = list(route)
    first = next((i for i, p in enumerate(points) if p.action != "ground"), None)
    if first is None or first == len(points) - 1:
        raise ValueError("a route needs a take-off point and at least one more point")
    start = points[first]
    waypoints = _waypoints(LocalFrame(mission.launch), points, first, altitude_m)
    return FlightRoute(
        mission_id=mission.id,
        drone_id=mission.drone_id,
        pilot_callsign=mission.pilot_callsign,
        start_time=scenario.start_time,
        launch=mission.launch,
        start=Position(lat=start.lat, lon=start.lon),
        takeoff_s=start.t,
        takeoff_latest_s=latest_takeoff_s(scenario, mission, points, start.t, cuts),
        airborne=status == "fallback",
        altitude_m=altitude_m,
        cruise_speed_mps=mission.cruise_speed_mps,
        waypoints=tuple(waypoints),
        status=status,
        revision=revision,
        battery_used_pct=battery_used_pct,
    )


def _waypoints(
    frame: LocalFrame, points: list[RoutePoint], first: int, altitude_m: float
) -> list[Waypoint]:
    """Waypoints after the take-off point ``points[first]``, each hover pair merged."""
    waypoints: list[Waypoint] = []
    start = points[first]
    previous = frame.to_xy(Position(lat=start.lat, lon=start.lon))
    i = first + 1
    while i < len(points):
        here = points[i]
        xy = frame.to_xy(Position(lat=here.lat, lon=here.lon))
        hover_end = i + 1 < len(points) and here.action == "hover"
        depart = points[i + 1].t if hover_end else here.t
        i += 2 if hover_end else 1
        kind = _kind(last=i >= len(points), holds=depart > here.t)
        if kind == "waypoint" and math.dist(previous, xy) < MERGE_M:
            continue
        waypoints.append(
            Waypoint(
                number=len(waypoints) + 1,
                lat=here.lat,
                lon=here.lon,
                alt_m=altitude_m,
                arrive_s=here.t,
                depart_s=depart,
                kind=kind,
            )
        )
        previous = xy
    return waypoints


def _kind(*, last: bool, holds: bool) -> WaypointKind:
    if last:
        return "target"
    if holds:
        return "hold"
    return "waypoint"


def briefing(route: FlightRoute) -> list[str]:
    """What the pilot needs, one line each: no zone, no jammer (need to know)."""
    head = route.title
    if route.revision is not None:
        head += f" rev {route.revision}"
    if route.status:
        head += f" ({route.status})"
    start = f"{route.start.lat:.5f},{route.start.lon:.5f}"
    if route.airborne:
        lines = [head, f"New route from {start} at {route.label(route.takeoff_s)}"]
    else:
        lines = [head, f"T/O {takeoff_window(route)} from {start}"]
    lines.append(f"Alt {route.altitude_m:.0f} m above launch, {route.cruise_speed_mps:.0f} m/s")
    for wp in route.waypoints:
        if wp.kind == "hold":
            lines.append(
                f"WP{wp.number} {route.label(wp.arrive_s)} HOLD, "
                f"leave not before {route.label(wp.depart_s, up=True)}"
            )
        elif wp.kind == "target":
            lines.append(f"TGT {route.label(wp.arrive_s)}")
        else:
            lines.append(f"WP{wp.number} {route.label(wp.arrive_s)}")
    if route.battery_used_pct is not None:
        lines.append(f"Battery {route.battery_used_pct:.0f}% for this leg")
    if route.takeoff_latest_s is not None or any(w.kind == "hold" for w in route.waypoints):
        lines.append("Timed on friendly jamming: never early, never past the latest time")
    return lines


def takeoff_window(route: FlightRoute) -> str:
    """``not before HH:MM:SS`` or ``HH:MM:SS-HH:MM:SS``."""
    first = route.label(route.takeoff_s, up=True)
    if route.takeoff_latest_s is None:
        return f"not before {first}"
    return f"{first}-{route.label(route.takeoff_latest_s)}"
