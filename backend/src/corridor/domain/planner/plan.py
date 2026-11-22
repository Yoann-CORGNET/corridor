"""Planning entry points: from a mission, its scenario and the cuts to timed routes and their
comparison. Each mission is planned on its own, on its own grid. Every route returned is
smoothed (``corridor.domain.planner.smooth``) on the masks it was searched on."""

from __future__ import annotations

import bisect
import itertools
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import distance_transform_edt

from corridor.domain.events import (
    ChoiceKind,
    PlanMetrics,
    PlanStatus,
    ProposedCut,
    RouteAction,
    RouteChoice,
    RoutePoint,
)
from corridor.domain.geo import Cell, Grid, LocalFrame, Position
from corridor.domain.planner.astar import Step, earliest_path
from corridor.domain.planner.layers import Cells, Layers, forbidden_cells, mission_layers
from corridor.domain.planner.masks import TimedMasks, shape_mask
from corridor.domain.planner.smooth import smooth_route
from corridor.domain.scenario import Mission, Scenario
from corridor.domain.state import Cut, merge_cuts
from corridor.domain.zones import ThreatShape, forbidden_shapes, mission_shapes

ENTRY_MARGIN_S = 30.0
"""Enter a friendly zone no sooner than this after its cut starts."""
EXIT_MARGIN_S = 60.0
"""Leave a friendly zone at least this long before its jammer restarts."""
MIN_SAVING_S = 1.0
"""A route using cuts must save more flight time than this to be preferred."""
COARSE_TAKEOFF_STEP_S = 30.0
"""Takeoff times are swept at this step, then refined at 5 s around the best one."""
MIN_OPTION_SAVING_S = 30.0
"""A route needing cuts must save at least this much flight time against the route before it
(the safe one, or ``one_cut`` for ``fastest``) to be worth asking the command. Below, it is
still shown, so the pilot sees why it is not offered."""
OPTION_ACTOR = "planner"
"""``requested_by`` of the hypothetical cuts the choices are computed with."""


@dataclass(frozen=True)
class MissionPlan:
    """Content of ``plan.updated``, without the revision the engine adds."""

    status: PlanStatus
    metrics: PlanMetrics
    baseline_route: list[RoutePoint]
    optimised_route: list[RoutePoint]
    choices: list[RouteChoice] = field(default_factory=list)
    escape_until_s: float | None = None
    """Fallback only: end of the straight leg out of a zone the drone was caught in. The
    safety rules ignore the route before it: the drone is getting out."""


def plan_mission(
    scenario: Scenario,
    mission: Mission,
    grid: Grid,
    cuts: Sequence[Cut],
    earliest_departure_s: float = 0.0,
) -> MissionPlan | None:
    """Baseline, best route on confirmed cuts, best route on every cut; pick and compare.

    - ``preview``: scheduled cuts give a better route than confirmed ones. Not flyable until
      the command confirms.
    - ``approved``: confirmed cuts give a better route than the baseline.
    - ``baseline``: no cut helps; the optimised route is the baseline.

    Routes leave no sooner than the mission departure and ``earliest_departure_s`` (a replan
    during the run cannot take off in the past).

    ``choices`` are compared with the flyable route (``approved`` or ``baseline``), the safe one.
    Without any route avoiding every jammer (the pilot stands inside a friendly jammer's zone),
    see ``_plan_through_cuts``. None when no route exists at all.
    """
    if earliest_departure_s > mission.departure_s:
        mission = mission.model_copy(update={"departure_s": earliest_departure_s})
    baseline = plan_route(scenario, mission, grid)
    if baseline is None:
        return _plan_through_cuts(scenario, mission, grid, cuts)
    status: PlanStatus = "baseline"
    optimised = baseline
    confirmed = [c for c in cuts if c.status == "confirmed"]
    if confirmed:
        approved = plan_route(scenario, mission, grid, confirmed)
        if approved is not None and _saves(approved, optimised):
            status, optimised = "approved", approved
    flyable = optimised
    if len(confirmed) < len(cuts):
        preview = plan_route(scenario, mission, grid, cuts)
        if preview is not None and _saves(preview, optimised):
            status, optimised = "preview", preview
    return MissionPlan(
        status=status,
        metrics=route_metrics(mission, baseline, optimised),
        baseline_route=baseline,
        optimised_route=optimised,
        choices=route_choices(scenario, mission, grid, cuts, flyable),
    )


def _plan_through_cuts(
    scenario: Scenario, mission: Mission, grid: Grid, cuts: Sequence[Cut]
) -> MissionPlan | None:
    """Plan of a mission that cannot fly without a cut, as when its pilot starts inside a
    friendly jammer's zone: no detour, only routes cutting jammers.

    ``approved`` once confirmed cuts give a route; until then ``preview`` (not flyable), on the
    scheduled cuts if they give one, else on the first route choice. The route is its own
    baseline: there is nothing to compare it with. None when no route exists, even with cuts.
    """
    confirmed = [c for c in cuts if c.status == "confirmed"]
    choices = route_choices(scenario, mission, grid, cuts, None)
    status: PlanStatus = "preview"
    route = plan_route(scenario, mission, grid, confirmed) if confirmed else None
    if route is not None:
        status = "approved"
    elif cuts:
        route = plan_route(scenario, mission, grid, cuts)
    if route is None and choices:
        route = choices[0].route
    if route is None:
        return None
    return MissionPlan(
        status=status,
        metrics=route_metrics(mission, route, route),
        baseline_route=route,
        optimised_route=route,
        choices=choices,
    )


def route_choices(
    scenario: Scenario,
    mission: Mission,
    grid: Grid,
    cuts: Sequence[Cut],
    safe: list[RoutePoint] | None,
) -> list[RouteChoice]:
    """Routes the pilot may pick: ``fastest``, ``one_cut``, ``safe``, in that order.

    ``safe`` is the flyable route as is. ``one_cut`` is the best of the routes cutting a single
    friendly jammer, ``fastest`` the route cutting every jammer it needs (``_with_cuts``).
    Jammers with a cut not over by departure are left alone: they are already being dealt
    with.

    ``one_cut`` is shown as soon as it is faster than ``safe`` (``MIN_SAVING_S``), and is
    ``worth_asking`` only if it saves ``MIN_OPTION_SAVING_S``: the pilot always sees the direct
    route and how little it would gain. ``fastest`` is shown only when it cuts more than one
    jammer and is worth asking against ``one_cut`` (or against ``safe`` without ``one_cut``):
    a second direct route saving a few seconds more would only clutter the list.
    """

    def choice(
        kind: ChoiceKind, cuts: list[ProposedCut], route: list[RoutePoint], worth: bool
    ) -> RouteChoice:
        radars = _radars_crossed(scenario, mission, route)
        return _choice(mission, kind, cuts, route, safe, radars, worth)

    # No safe route: the pilot cannot fly without a cut, so every direct route is worth asking.
    choices = [] if safe is None else [choice("safe", [], safe, True)]
    start, goal = grid.cell_of(mission.launch), grid.cell_of(mission.target)
    if start is None or goal is None:
        return choices
    # A jammer whose cut still awaits the command's answer is left alone: one request at a
    # time. A confirmed cut does not block: a route may ask to extend it.
    busy = {c.jammer for c in cuts if c.status != "confirmed" and c.end_s > mission.departure_s}
    free = [
        z.threat
        for z in mission_shapes(scenario, mission)
        if z.kind == "friendly_jammer" and z.threat not in busy
    ]
    confirmed = [c for c in cuts if c.status == "confirmed"]
    singles = [
        found
        for found in (_with_cuts(scenario, mission, grid, confirmed, [j]) for j in free)
        if found is not None
    ]
    reference = safe
    if singles:
        one_cut = min(singles, key=lambda f: flight_s(f[0]))
        gain = math.inf if safe is None else flight_s(safe) - flight_s(one_cut[0])
        if gain > MIN_SAVING_S:
            worth = gain >= MIN_OPTION_SAVING_S
            choices.insert(0, choice("one_cut", one_cut[1], one_cut[0], worth))
            reference = one_cut[0]
    if len(free) > 1:
        fastest = _with_cuts(scenario, mission, grid, confirmed, free)
        if (
            fastest is not None
            and len(fastest[1]) > 1
            and (
                reference is None
                or flight_s(reference) - flight_s(fastest[0]) >= MIN_OPTION_SAVING_S
            )
        ):
            choices.insert(0, choice("fastest", fastest[1], fastest[0], True))
    return choices


def _with_cuts(
    scenario: Scenario,
    mission: Mission,
    grid: Grid,
    confirmed: Sequence[Cut],
    jammers: Sequence[str],
) -> tuple[list[RoutePoint], list[ProposedCut]] | None:
    """Fastest route if ``jammers`` could be cut, and the cut windows it needs.

    The windows come from the route the drone would fly if those jammers were off for the
    whole flight, taking off at departure: for each zone it enters, from the first to the
    last moment it is in a cell the zone touches, widened by the entry and exit margins. A
    jammer whose zone the route does not enter needs no cut. The route is then searched again
    with those windows only, so the result is exactly what scheduling the cuts gives. A window
    may start before departure: a drone taking off inside the zone needs the jammer off from
    its take-off, margin included. ``confirmed`` cuts hold in both
    searches. None when no route fits the battery or no jammer needs cutting.
    """
    start, goal = grid.cell_of(mission.launch), grid.cell_of(mission.target)
    if start is None or goal is None:
        return None
    departure = mission.departure_s
    end = departure + mission.battery_max_s
    held = _usable_cuts(confirmed)
    off = [_hypothetical(j, departure, end) for j in jammers]
    layers = mission_layers(scenario, mission, grid, [*held, *off], departure, end)
    path = _path(grid, mission, layers, start, goal, departure, end)
    if path is None:
        return None
    steps, takeoff = path
    cells = _jammer_cells(scenario, mission, grid, jammers)
    needed: list[Cut] = []
    for jammer in jammers:
        jammed = cells.get(jammer)
        if jammed is None:
            continue
        presence = _presence(steps, jammed)
        if presence is None:
            continue
        first = presence[0]
        if jammed[0, start[1], start[0]]:  # taking off inside the zone: off from take-off
            first = min(first, takeoff)
        need = _hypothetical(
            jammer,
            max(0.0, math.floor(first - ENTRY_MARGIN_S)),
            math.ceil(presence[1] + EXIT_MARGIN_S),
        )
        # Only what the confirmed cuts of this jammer do not cover yet is asked for.
        needed += _minus(need, [c for c in confirmed if c.jammer == jammer])
    if not needed:
        return None  # the confirmed cuts already cover it: nothing to ask
    horizon = max([departure, *(w.end_s for w in needed)]) + mission.battery_max_s
    usable = _usable_cuts([*confirmed, *needed])
    layers = mission_layers(scenario, mission, grid, usable, departure, horizon)
    route = _search(grid, mission, layers, start, goal, departure, horizon)
    if route is None or flight_s(route) > mission.battery_max_s:
        return None
    # Smoothed on the masks of the windows, like any planned route: it stays inside them.
    route = smooth_route(
        route, grid, layers.timed, mission.cruise_speed_mps, altitudes=layers.altitudes
    )
    proposed = [ProposedCut(jammer=w.jammer, start_s=w.start_s, end_s=w.end_s) for w in needed]
    return route, proposed


def _jammer_cells(
    scenario: Scenario, mission: Mission, grid: Grid, jammers: Sequence[str]
) -> dict[str, Cells]:
    """Cells each friendly jammer of ``jammers`` forbids, ``[level, row, col]``, as the planner
    masks them: its 2D zone (one level) or its 3D zone through the terrain."""
    if scenario.terrain.mode == "2d":
        frame = LocalFrame(mission.launch)
        return {
            z.threat: shape_mask(grid, z, frame)[np.newaxis]
            for z in forbidden_shapes(scenario, mission)
            if z.kind == "friendly_jammer" and z.threat in jammers
        }
    friendly, _ = forbidden_cells(scenario, mission, grid)
    return {j: c for j, c in friendly.items() if j in jammers}


def _choice(
    mission: Mission,
    kind: ChoiceKind,
    cuts: list[ProposedCut],
    route: list[RoutePoint],
    safe: list[RoutePoint] | None,
    radars: list[str],
    worth: bool,
) -> RouteChoice:
    route_s = flight_s(route)
    safe_s = route_s if safe is None else flight_s(safe)  # nothing to save against
    return RouteChoice(
        kind=kind,
        cuts=cuts,
        route=route,
        flight_s=route_s,
        battery_used_pct=100 * route_s / mission.battery_max_s,
        saved_s=safe_s - route_s,
        saved_pct=100 * (safe_s - route_s) / safe_s,
        radars=radars,
        worth_asking=worth,
    )


RADAR_CHECK_M = 50.0
"""Step along a route when looking for the radar zones it crosses."""


def _radars_crossed(scenario: Scenario, mission: Mission, route: list[RoutePoint]) -> list[str]:
    """Radars whose exact zone ``route`` enters, in the order it meets them."""
    radars = [z for z in mission_shapes(scenario, mission) if z.kind == "radar"]
    if not radars:
        return []
    frame = LocalFrame(mission.launch)
    xy = [frame.to_xy(Position(lat=p.lat, lon=p.lon)) for p in route]
    crossed: list[str] = []
    for a, b in itertools.pairwise(xy):
        steps = max(1, math.ceil(math.dist(a, b) / RADAR_CHECK_M))
        for i in range(steps + 1):
            x = a[0] + (b[0] - a[0]) * i / steps
            y = a[1] + (b[1] - a[1]) * i / steps
            for radar in radars:
                if radar.threat not in crossed and radar.contains(x, y):
                    crossed.append(radar.threat)
    return crossed


def plan_route(
    scenario: Scenario, mission: Mission, grid: Grid, cuts: Iterable[Cut] = ()
) -> list[RoutePoint] | None:
    """Route with the least flight time found, friendly jammers off during ``cuts`` only.

    No cut gives the baseline. Each cut window first shrinks by the safety margins, so the
    route is safe by construction. Waiting on the ground before takeoff is free, hovering is
    not, so takeoff is a variable: the search runs for takeoff times swept every
    ``COARSE_TAKEOFF_STEP_S``, then every 5 s around the best, and keeps the least flight
    time, then the earliest arrival. A better takeoff narrower than the coarse step can be
    missed. The best grid route is then smoothed. None when no route fits the battery.
    """
    start, goal = grid.cell_of(mission.launch), grid.cell_of(mission.target)
    if start is None or goal is None:
        return None
    windows = _usable_cuts(cuts)
    zones = forbidden_shapes(scenario, mission)
    latest = max([mission.departure_s, *(w.end_s for w in windows)])
    layers = mission_layers(
        scenario, mission, grid, windows, mission.departure_s, latest + mission.battery_max_s
    )

    def search(takeoffs: Iterable[float], best: list[RoutePoint] | None) -> list[RoutePoint] | None:
        for takeoff in takeoffs:
            limit_s = flight_s(best) if best else mission.battery_max_s
            route = _search(grid, mission, layers, start, goal, takeoff, takeoff + limit_s)
            if route is None or flight_s(route) > mission.battery_max_s:
                continue
            if best is None or (flight_s(route), route[-1].t) < (flight_s(best), best[-1].t):
                best = route
        return best

    def smoothed(route: list[RoutePoint] | None) -> list[RoutePoint] | None:
        if route is None:
            return None
        return smooth_route(
            route, grid, layers.timed, mission.cruise_speed_mps, altitudes=layers.altitudes
        )

    best = search([mission.departure_s], None)
    if not windows:
        return smoothed(best)
    # A route that uses a window is in its zone after the window opens, so taking off more
    # than ``flight_s(best)`` before the first window cannot beat ``best``. A route that uses
    # no window does no better from a later takeoff. Exact, and it skips hopeless searches
    # when the cut is far ahead.
    earliest_useful = min(w.start_s for w in windows) - (flight_s(best) if best else math.inf)
    first = max(mission.departure_s, earliest_useful)
    latest_takeoff = _latest_useful_takeoff(mission, zones, windows, grid.cell_m)
    best = search(_steps(first, latest_takeoff, COARSE_TAKEOFF_STEP_S), best)
    if best is None:
        return None
    around = _takeoff(best)
    fine = _steps(around - COARSE_TAKEOFF_STEP_S, around + COARSE_TAKEOFF_STEP_S, 5.0)
    return smoothed(search((t for t in fine if t >= mission.departure_s), best))


def plan_fallback(
    scenario: Scenario,
    mission: Mission,
    grid: Grid,
    position: Position,
    now_s: float,
    level: int = 0,
) -> MissionPlan | None:
    """Safe route for a drone already in the air at ``position``: no cut, take off now.

    The pilot stays at the launch point, so the zones are those of the mission; only the
    route starts elsewhere, at flight ``level`` in 3D. Used when the drone can no longer keep
    its cut windows, or when a threat added in flight lies on its route. A drone already inside
    a forbidden cell (the threat appeared over it) first flies straight to the nearest free
    cell at its level.
    """
    start, goal = grid.cell_of(position), grid.cell_of(mission.target)
    if start is None or goal is None:
        return None
    layers = mission_layers(scenario, mission, grid, [], now_s, now_s + mission.battery_max_s)
    level = min(level, len(layers.timed) - 1)
    deadline = now_s + mission.battery_max_s
    # A drone caught in a zone (a threat added over it) first leaves by the nearest free cell.
    exit_cell = escape_cell(layers.timed[level], start, now_s)
    if exit_cell is None:
        return None
    alt = None if layers.altitudes is None else layers.altitudes[level]
    prefix: list[RoutePoint] = []
    depart, depart_s = position, now_s
    if exit_cell != start:
        prefix = [RoutePoint(lat=position.lat, lon=position.lon, t=now_s, action="fly", alt_m=alt)]
        depart = grid.to_position(*_centre(grid, exit_cell))
        leg_m = math.dist(grid.to_xy(position), _centre(grid, exit_cell))
        depart_s = now_s + leg_m / mission.cruise_speed_mps
    airborne = mission.model_copy(update={"launch": depart, "departure_s": depart_s})
    route = _search(grid, airborne, layers, exit_cell, goal, depart_s, deadline, level)
    if route is None:
        return None
    route = prefix + smooth_route(
        route,
        grid,
        layers.timed,
        mission.cruise_speed_mps,
        delay_takeoff=False,
        altitudes=layers.altitudes,
    )
    return MissionPlan(
        status="fallback",
        metrics=route_metrics(mission, route, route),
        baseline_route=route,
        optimised_route=route,
        escape_until_s=depart_s if prefix else None,
    )


def escape_cell(timed: TimedMasks, start: Cell, now_s: float) -> Cell | None:
    """``start`` if free at ``now_s``, else the nearest cell free then; None if none is."""
    k = max(0, bisect.bisect_right(timed.edges, now_s) - 1)
    mask = timed.masks[min(k, len(timed.masks) - 1)]
    col, row = start
    if not mask[row, col]:
        return start
    if mask.all():
        return None
    _, (rows, cols) = distance_transform_edt(mask, return_indices=True)
    return int(cols[row, col]), int(rows[row, col])


def flight_s(route: list[RoutePoint]) -> float:
    """Time in the air: from takeoff to the target, hover included, ground wait excluded."""
    return route[-1].t - _takeoff(route)


def _takeoff(route: list[RoutePoint]) -> float:
    return next(p.t for p in route if p.action != "ground")


def _usable_cuts(cuts: Iterable[Cut]) -> list[Cut]:
    """The usable part of each cut, cuts of a jammer that touch merged first (one margin each
    side of the merged cut, none in between)."""
    return [w for w in (_usable(c) for c in merge_cuts(cuts)) if w is not None]


def _minus(need: Cut, covered: Sequence[Cut]) -> list[Cut]:
    """Parts of ``need`` that no cut of ``covered`` (same jammer) overlaps, in time order."""
    pieces = [need]
    for cut in sorted(covered, key=lambda c: c.start_s):
        rest: list[Cut] = []
        for piece in pieces:
            if cut.end_s <= piece.start_s or piece.end_s <= cut.start_s:
                rest.append(piece)
                continue
            if piece.start_s < cut.start_s:
                rest.append(piece.model_copy(update={"end_s": cut.start_s}))
            if cut.end_s < piece.end_s:
                rest.append(piece.model_copy(update={"start_s": cut.end_s}))
        pieces = rest
    return pieces


def _usable(cut: Cut) -> Cut | None:
    """The part of ``cut`` the drone may spend in the zone, or None if nothing is left."""
    start_s, end_s = cut.start_s + ENTRY_MARGIN_S, cut.end_s - EXIT_MARGIN_S
    if end_s <= start_s:
        return None
    return cut.model_copy(update={"start_s": start_s, "end_s": end_s})


def _latest_useful_takeoff(
    mission: Mission, zones: list[ThreatShape], windows: list[Cut], cell_m: float
) -> float:
    """Past this, even a straight flight reaches every cut zone after its window closed.

    For a directional jammer the zone lies within its main disc, so the distance to that disc
    is a lower bound: the sweep may go a little too far, never stop too early.
    """
    latest = mission.departure_s
    half_diagonal = cell_m * math.sqrt(2) / 2
    for window in windows:
        for zone in zones:
            if zone.kind != "friendly_jammer" or zone.threat != window.jammer:
                continue
            d = zone.disc
            to_centre = math.hypot(d.x, d.y)  # the mission frame is centred on the launch
            if d.forbidden == "inside":
                to_edge = to_centre - d.radius_m - half_diagonal
            else:
                to_edge = d.radius_m - half_diagonal - to_centre
            latest = max(latest, window.end_s - max(to_edge, 0.0) / mission.cruise_speed_mps)
    return latest


def _steps(first: float, last: float, step: float) -> list[float]:
    """``first``, ``first + step``, ... up to ``last`` included."""
    count = max(math.floor((last - first) / step), 0)
    return [first + i * step for i in range(count + 1)] + ([last] if last > first else [])


def _search(
    grid: Grid,
    mission: Mission,
    layers: Layers,
    start: Cell,
    goal: Cell,
    takeoff: float,
    deadline_s: float,
    start_level: int = 0,
) -> list[RoutePoint] | None:
    """Route taking off at ``takeoff``; None if it cannot reach the goal cell by
    ``deadline_s``."""
    path = _path(grid, mission, layers, start, goal, takeoff, deadline_s, start_level)
    return None if path is None else _route(grid, mission, *path, layers.altitudes)


def _path(
    grid: Grid,
    mission: Mission,
    layers: Layers,
    start: Cell,
    goal: Cell,
    takeoff: float,
    deadline_s: float,
    start_level: int = 0,
) -> tuple[list[Step], float] | None:
    """Cell steps and actual takeoff time of ``_search``, before they become a route."""
    speed = mission.cruise_speed_mps
    first_leg_s = math.dist(grid.to_xy(mission.launch), _centre(grid, start)) / speed
    steps = earliest_path(
        layers.timed,
        start,
        goal,
        takeoff + first_leg_s,
        grid.cell_m / speed,
        deadline_s,
        layers.climb_s,
        start_level,
    )
    if steps is None:
        return None
    first = steps[0]
    if first.depart_s > first.arrive_s and start_level == 0:
        # Hovering over the launch cell: better spent on the ground.
        takeoff += first.depart_s - first.arrive_s
        steps = [Step(first.cell, first.depart_s, first.depart_s, first.level), *steps[1:]]
    return steps, takeoff


def _hypothetical(jammer: str, start_s: float, end_s: float) -> Cut:
    return Cut(
        id=f"option-{jammer}",
        jammer=jammer,
        start_s=start_s,
        end_s=end_s,
        status="scheduled",
        requested_by=OPTION_ACTOR,
    )


def _presence(steps: list[Step], cells: Cells) -> tuple[float, float] | None:
    """First and last instant the drone is in, or moving from or to, a cell of ``cells``
    (``[level, row, col]``) at its level."""

    def forbidden(step: Step) -> bool:
        return bool(cells[min(step.level, cells.shape[0] - 1), step.cell[1], step.cell[0]])

    times = [t for s in steps if forbidden(s) for t in (s.arrive_s, s.depart_s)]
    for a, b in itertools.pairwise(steps):
        if forbidden(a) or forbidden(b):
            times += [a.depart_s, b.arrive_s]
    return (min(times), max(times)) if times else None


def _route(
    grid: Grid,
    mission: Mission,
    steps: list[Step],
    takeoff: float,
    altitudes: tuple[float, ...] | None,
) -> list[RoutePoint]:
    """Ground wait, launch, the turning, hover, climb and descent points of ``steps``,
    target. In 3D each point carries the height of its level (0 on the ground)."""
    launch = grid.to_xy(mission.launch)
    start_level = steps[0].level
    points: list[tuple[tuple[float, float], float, RouteAction, int | None]] = []
    if takeoff > mission.departure_s:
        points.append((launch, mission.departure_s, "ground", None))
    points.append((launch, takeoff, "fly", start_level))
    for i, step in enumerate(steps):
        hovers = step.depart_s > step.arrive_s
        ends = i in (0, len(steps) - 1)
        climbs = not ends and (step.level != steps[i - 1].level or step.level != steps[i + 1].level)
        turns = not ends and _turns(steps[i - 1].cell, step.cell, steps[i + 1].cell)
        if not (hovers or ends or climbs or turns):
            continue
        centre = _centre(grid, step.cell)
        if hovers:
            points.append((centre, step.arrive_s, "hover", step.level))
            points.append((centre, step.depart_s, "fly", step.level))
        else:
            points.append((centre, step.arrive_s, "fly", step.level))
    target = grid.to_xy(mission.target)
    last = steps[-1]
    leg_s = math.dist(_centre(grid, last.cell), target) / mission.cruise_speed_mps
    points.append((target, last.arrive_s + leg_s, "fly", last.level))

    route: list[RoutePoint] = []
    previous: tuple[tuple[float, float], float] | None = None
    for xy, t, action, level in points:
        position = grid.to_position(*xy)
        alt = None if altitudes is None else 0.0 if level is None else altitudes[level]
        point = RoutePoint(lat=position.lat, lon=position.lon, t=t, action=action, alt_m=alt)
        if previous == (xy, t):
            route[-1] = point  # launch or target exactly on a cell centre
        else:
            route.append(point)
        previous = (xy, t)
    return route


def _saves(candidate: list[RoutePoint], reference: list[RoutePoint]) -> bool:
    return flight_s(candidate) < flight_s(reference) - MIN_SAVING_S


def route_metrics(
    mission: Mission, baseline: list[RoutePoint], optimised: list[RoutePoint]
) -> PlanMetrics:
    baseline_s, optimised_s = flight_s(baseline), flight_s(optimised)
    return PlanMetrics(
        baseline_battery_used_pct=100 * baseline_s / mission.battery_max_s,
        optimised_battery_used_pct=100 * optimised_s / mission.battery_max_s,
        saved_pct=100 * (baseline_s - optimised_s) / baseline_s if baseline_s > 0 else 0.0,
        saved_s=baseline_s - optimised_s,
    )


def _turns(before: Cell, here: Cell, after: Cell) -> bool:
    return (here[0] - before[0], here[1] - before[1]) != (after[0] - here[0], after[1] - here[1])


def _centre(grid: Grid, cell: Cell) -> tuple[float, float]:
    return (cell[0] + 0.5) * grid.cell_m, (cell[1] + 0.5) * grid.cell_m
