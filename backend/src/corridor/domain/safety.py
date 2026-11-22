"""The simulated flight and the safety rule checked while it flies.

A flight follows a timed route ``delay_s`` behind its schedule (late launch, or held by
``inject.delay``). While held, the drone stays where it is; held time in the air counts on the
battery.

Safety rules, on the rest of the route shifted by the delay:

- ``schedule_violation``: it may only be in a friendly jammer zone inside a confirmed cut, with
  the planner's margins (broken when the drone is late or a cut is cancelled);
- ``route_threat``: it may not enter an enemy jammer or radar zone, nor in 3D a cell out of the
  pilot's reach (broken when the command adds a threat while the drone flies).

When either no longer holds, the engine asks for a fallback route from the drone position,
using no cut.

In 3D (``LevelCells``) the zones are the planner's cells at the drone's flight level; during a
climb or a descent, both levels count.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt

from corridor.domain.events import RouteAction, RoutePoint
from corridor.domain.geo import Grid, LocalFrame, Position
from corridor.domain.planner.plan import ENTRY_MARGIN_S, EXIT_MARGIN_S
from corridor.domain.state import Cut, merge_cuts
from corridor.domain.zones import ThreatShape

CHECK_STEP_S = 2.0
TOLERANCE_S = 2.0
"""Slack on the margins, so sampling differences with the planner never flag an on-time drone."""


@dataclass
class Flight:
    route: list[RoutePoint]
    revision: int
    delay_s: float = 0.0
    air_before_s: float = 0.0
    """Airborne time on previous routes."""
    fallback_requested: bool = False
    held_route_t: float | None = None
    hold_start_s: float = 0.0
    hold_until_s: float = 0.0
    hold_in_air: bool = False
    past_holds_air_s: float = 0.0
    through_jammers: frozenset[str] = frozenset()
    """Friendly jammers the pilot chose to fly straight through while on (``on_timeout``
    cross after a refusal): the safety rule leaves them alone."""
    escape_until_s: float = 0.0
    """Route time before which the safety rules look away: the straight leg out of a zone the
    drone was caught in (``MissionPlan.escape_until_s``)."""

    def route_time(self, now: float) -> float:
        """Where on its schedule the drone is at sim time ``now``."""
        if self.held_route_t is not None and now < self.hold_until_s:
            return self.held_route_t
        return now - self.delay_s

    def hold(self, now: float, delay_s: float) -> None:
        """Keep the drone where it is for ``delay_s`` more seconds."""
        current = self.route_time(now)
        self.past_holds_air_s += self._current_hold_air(now)
        self.hold_in_air = position_at(self.route, current)[1] != "ground"
        self.held_route_t = current
        self.hold_start_s = now
        self.hold_until_s = max(self.hold_until_s, now) + delay_s
        self.delay_s += delay_s

    def airborne_s(self, now: float) -> float:
        route_air = airborne_s(self.route, self.route_time(now))
        return self.air_before_s + self.past_holds_air_s + self._current_hold_air(now) + route_air

    def arrived(self, now: float) -> bool:
        return self.route_time(now) >= self.route[-1].t

    def _current_hold_air(self, now: float) -> float:
        if self.held_route_t is None or not self.hold_in_air:
            return 0.0
        return max(0.0, min(now, self.hold_until_s) - self.hold_start_s)


def position_at(route: Sequence[RoutePoint], t: float) -> tuple[Position, RouteAction]:
    """Position on a timed route; the action of a point holds until the next one."""
    first, last = route[0], route[-1]
    if t <= first.t:
        return Position(lat=first.lat, lon=first.lon), first.action
    for a, b in itertools.pairwise(route):
        if a.t <= t <= b.t:
            if a.action != "fly" or b.t == a.t:
                return Position(lat=a.lat, lon=a.lon), a.action
            f = (t - a.t) / (b.t - a.t)
            position = Position(lat=a.lat + f * (b.lat - a.lat), lon=a.lon + f * (b.lon - a.lon))
            return position, "fly"
    return Position(lat=last.lat, lon=last.lon), last.action


def airborne_s(route: Sequence[RoutePoint], t: float) -> float:
    """Time in the air (fly and hover) from the start of the route to ``t``."""
    return sum(
        min(t, b.t) - a.t for a, b in itertools.pairwise(route) if a.action != "ground" and t > a.t
    )


def takeoff_s(route: Sequence[RoutePoint]) -> float:
    return next((p.t for p in route if p.action != "ground"), route[-1].t)


@dataclass(frozen=True)
class LevelCells:
    """3D: the cells each friendly jammer forbids at each flight level, on the mission grid,
    and those forbidden whatever the cuts, by threat (``corridor.domain.planner.layers``)."""

    grid: Grid
    altitudes: tuple[float, ...]
    by_jammer: dict[str, npt.NDArray[np.bool_]]
    """``[level, row, col]``."""
    blocked: dict[str, npt.NDArray[np.bool_]] = field(default_factory=dict)
    """Enemy jammers, radars and ``LINK`` (out of the pilot's reach), ``[level, row, col]``."""

    def jammers(self, position: Position, levels: set[int]) -> list[str]:
        return _inside(self.grid, self.by_jammer, position, levels)

    def threats(self, position: Position, levels: set[int]) -> list[str]:
        return _inside(self.grid, self.blocked, position, levels)

    def levels_at(self, route: Sequence[RoutePoint], t: float) -> set[int]:
        """Levels of the route points around ``t``: one in level flight, two in a climb."""
        around = next(((a, b) for a, b in itertools.pairwise(route) if a.t <= t <= b.t), None)
        points = around or (route[-1],)
        return {self.altitudes.index(p.alt_m) for p in points if p.alt_m in self.altitudes}

    def level_at(self, route: Sequence[RoutePoint], t: float) -> int:
        """Level of the last route point at or before ``t`` (the lowest if none)."""
        before = [p for p in route if p.t <= t and p.alt_m in self.altitudes]
        return self.altitudes.index(before[-1].alt_m) if before else 0


def schedule_violation(
    flight: Flight,
    now: float,
    frame: LocalFrame,
    zones: Sequence[ThreatShape],
    cuts: Sequence[Cut],
    cells: LevelCells | None = None,
) -> str | None:
    """Friendly jammer whose zone the rest of the route would enter outside a confirmed cut.

    ``frame`` is the mission frame (centred on the launch point) the zones are expressed in;
    with ``cells`` (3D), the zones are those cells instead.
    """
    windows = [
        (c.jammer, c.start_s + ENTRY_MARGIN_S - TOLERANCE_S, c.end_s - EXIT_MARGIN_S + TOLERANCE_S)
        for c in merge_cuts(c for c in cuts if c.status == "confirmed")
    ]
    friendly = [
        z for z in zones if z.kind == "friendly_jammer" and z.threat not in flight.through_jammers
    ]
    t, end = max(flight.route_time(now), flight.escape_until_s), flight.route[-1].t
    while t <= end:
        position, action = position_at(flight.route, t)
        if action != "ground":
            actual = t + flight.delay_s
            if cells is not None:
                inside = cells.jammers(position, cells.levels_at(flight.route, t))
            else:
                x, y = frame.to_xy(position)
                inside = [z.threat for z in friendly if z.contains(x, y)]
            for jammer in inside:
                if jammer in flight.through_jammers:
                    continue  # flown through on purpose (plan B straight anyway)
                if not any(j == jammer and a <= actual <= b for j, a, b in windows):
                    return jammer
        t += CHECK_STEP_S
    return None


def route_threat(
    flight: Flight,
    now: float,
    frame: LocalFrame,
    zones: Sequence[ThreatShape],
    cells: LevelCells | None = None,
) -> str | None:
    """Enemy jammer or radar whose zone the rest of the route would enter; in 3D (``cells``)
    also ``LINK`` where the pilot's signal is too weak at the drone's level."""
    enemies = [z for z in zones if z.kind in ("enemy_jammer", "radar")]
    t, end = max(flight.route_time(now), flight.escape_until_s), flight.route[-1].t
    while t <= end:
        position, action = position_at(flight.route, t)
        if action != "ground":
            if cells is not None:
                inside = cells.threats(position, cells.levels_at(flight.route, t))
            else:
                x, y = frame.to_xy(position)
                inside = [z.threat for z in enemies if z.contains(x, y)]
            if inside:
                return inside[0]
        t += CHECK_STEP_S
    return None


def _inside(
    grid: Grid, by_threat: dict[str, npt.NDArray[np.bool_]], position: Position, levels: set[int]
) -> list[str]:
    cell = grid.cell_of(position)
    if cell is None or not levels:
        return []
    col, row = cell
    return [t for t, cells in by_threat.items() if any(cells[lv, row, col] for lv in levels)]


def cut_transitions(cuts: Sequence[Cut], after: float, until: float) -> list[tuple[Cut, str]]:
    """Confirmed cuts that start or end in (after, until], in time order. Cuts of a jammer
    that touch count as one: the jammer is not restarted between them."""
    found: list[tuple[float, Cut, str]] = []
    for cut in merge_cuts(c for c in cuts if c.status == "confirmed"):
        if after < cut.start_s <= until:
            found.append((cut.start_s, cut, "start"))
        if after < cut.end_s <= until:
            found.append((cut.end_s, cut, "end"))
    return [(cut, kind) for _, cut, kind in sorted(found, key=lambda f: f[0])]
