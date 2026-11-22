"""The engine: single owner of world state, applies commands and publishes events.

Everything runs on the asyncio event loop thread and ``submit`` never awaits, so no two
commands interleave and the state needs no lock. Subscribers are called synchronously and
must not block (the WebSocket hub only enqueues).

Several missions share the jammers and the cuts. Each mission has its own runtime: plan,
flight, drone, revision. A mission only reacts to the friendly jammers that concern it
(``MissionConstraints.jammers``). Missions come from the scenario file or from a pilot's
``mission.request``; the engine keeps ``scenario.missions`` up to date with both.

Threats: the command adds and removes enemy threats and zones to avoid (``threat.add``,
``threat.remove``); the engine keeps ``scenario.enemy_threats`` up to date. Each change
recomputes the zones and grid of every mission, and every mission still ``planned`` that the
threat concerns gets a new plan. A drone in the air whose route now enters the new zone falls
back to a safe route (safety rule); otherwise it keeps its route. Its pilot is told.

Scripted timeline: ``scenario.events`` fire once each, at the first tick past their ``at_s``,
as the commands they mirror (``jammer.restart`` by the jammer's operator, ``threat.add`` and
``threat.remove`` by the command, ``inject.delay`` by the scenario). A rejected one (no drone
in flight to hold, jammer not cut) is skipped and the command is told.

Plans: while a mission is still ``planned``, every change to the cuts of a jammer that
concerns it requests a new plan with a new revision of that mission. The plan comes back later,
on the event loop thread; a plan whose revision is not the latest requested is dropped.

Flight: ``tick`` runs about twice per wall-clock second. It reports confirmed cuts starting
and ending (``jammer.restarted``), replans a plan whose take-off time has passed before launch,
moves the simulated drones (``drone.state``) and applies the safety rule of
``corridor.domain.safety`` to each: when a drone can no longer keep its cut windows, it asks
for a fallback route from the drone position, using no cut, and switches the flight to it.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import assert_never

from corridor.domain.commands import (
    AddThreat,
    CancelCut,
    ChooseRoute,
    Command,
    ConfirmCut,
    DecideRoute,
    InjectDelay,
    LaunchMission,
    RemoveThreat,
    RequestMission,
    RestartJammer,
    ScheduleCut,
    SetClock,
    UpdateMission,
)
from corridor.domain.events import (
    ClockChanged,
    CutCancelled,
    CutConfirmed,
    CutScheduled,
    DroneState,
    Event,
    JammerRestarted,
    MissionCreated,
    MissionStatusChanged,
    MissionUpdated,
    Notification,
    NotificationAction,
    PlanUpdated,
    RouteRequest,
    RouteRequested,
    ThreatAdded,
    ThreatRemoved,
)
from corridor.domain.geo import LocalFrame
from corridor.domain.planner.layers import LINK, cells_by_threat, link_cells
from corridor.domain.planner.plan import MissionPlan, route_metrics
from corridor.domain.reasons import choice_reasons, route_reasons
from corridor.domain.safety import (
    Flight,
    LevelCells,
    cut_transitions,
    position_at,
    route_threat,
    schedule_violation,
    takeoff_s,
)
from corridor.domain.scenario import (
    THREAT_PREFIX,
    AvoidZone,
    EnemyJammer,
    EnemyRadar,
    EnemyThreat,
    Mission,
    Scenario,
    ScriptedEvent,
    ScriptedHold,
    ScriptedRemoval,
    ScriptedRestart,
    ScriptedThreat,
)
from corridor.domain.state import SCENARIO_ACTOR, Cut, WorldState, merge_cuts
from corridor.domain.terrain import profile, with_altitude
from corridor.domain.zones import (
    MissionConstraints,
    ThreatShape,
    mission_constraints,
    mission_shapes,
)
from corridor.engine.clock import SimClock
from corridor.engine.replan import Replanner

Subscriber = Callable[[Event], None]

PLAN_LEAD_WALL_S = 3.0
"""Wall-clock seconds a plan may take. A replan's earliest takeoff is that far ahead in
simulated time, per plan queued before it, so the route does not start in the past by the
time it arrives."""


FLYABLE = ("approved", "baseline")
"""Plan statuses a drone may launch on. ``preview`` waits for the command's confirmation."""

DECISION_LEAD_S = 120.0
"""The command answers a route request at the latest this long before the take-off or the
first cut it needs, whichever comes first, so the jammer crews have time to act."""
TIMEOUT_ACTOR = "timeout"
KIND_LABEL = {
    "fastest": "direct route (all cuts)",
    "one_cut": "direct route (1 cut)",
    "safe": "detour",
}
"""Route choices in the words of the front end: the detour goes around every jammer, a
direct route goes through jammers during their cuts."""
LAUNCH_ACTOR = "launch"


class CommandRejected(Exception):
    """The command breaks a domain rule. The message is shown to the sender."""


SAFE_ROUTE = "Computing a safe route."


@dataclass
class MissionRuntime:
    """What the engine keeps for one mission, next to its status in ``WorldState``."""

    mission: Mission
    constraints: MissionConstraints
    frame: LocalFrame
    shapes: list[ThreatShape]
    """Zones of the concerned threats, in ``frame``, for the safety rule."""
    cells: LevelCells | None = None
    """3D: the friendly zones per level for the safety rule, instead of ``shapes``."""
    plan: PlanUpdated | None = None
    flight: Flight | None = None
    drone: DroneState | None = None
    revision: int = 0
    awaiting: int | None = None
    """Revision of the mission plan on its way, None when none is."""
    request: RouteRequest | None = None
    """The route the pilot picked, and the command's answer if it needs cuts."""

    def concerns(self, jammer: str) -> bool:
        return jammer in self.constraints.jammers


class Engine:
    def __init__(
        self,
        scenario: Scenario,
        cell_m: float,
        clock: SimClock,
        replanner: Replanner | None = None,
    ) -> None:
        """``cell_m``: cell size of the mission planning grids. Without ``replanner``, nothing
        is planned."""
        self.scenario = scenario
        self.clock = clock
        self.state = WorldState.from_scenario(scenario)
        self.missions: dict[str, MissionRuntime] = {}
        self._cell_m = cell_m
        for mission in scenario.missions:
            self.missions[mission.id] = self._runtime_for(mission)
        self._replanner = replanner
        self._subscribers: list[Subscriber] = []
        self._cut_ids = itertools.count(1)
        self._mission_ids = itertools.count(1)
        self._last_tick_s = clock.now()

    def _runtime_for(self, mission: Mission, scenario: Scenario | None = None) -> MissionRuntime:
        """Zones of ``mission`` in ``scenario`` (default: the current one).

        Raises ValueError on a degenerate zone (jammer on the launch point)."""
        scenario = scenario or self.scenario
        concerned = mission_constraints(scenario, mission, self._cell_m)
        ids = {*concerned.jammers, *concerned.threats}
        cells = None
        if scenario.terrain.mode == "3d":
            by_threat = cells_by_threat(scenario, mission, concerned.grid)
            by_jammer = {
                t: c for t, (kind, c) in by_threat.items() if kind == "friendly_jammer" and t in ids
            }
            blocked = {
                t: c for t, (kind, c) in by_threat.items() if kind != "friendly_jammer" and t in ids
            }
            blocked[LINK] = link_cells(scenario, mission, concerned.grid)
            cells = LevelCells(concerned.grid, scenario.terrain.levels_m, by_jammer, blocked)
        return MissionRuntime(
            mission=mission,
            constraints=concerned,
            frame=LocalFrame(mission.launch),
            shapes=[z for z in mission_shapes(scenario, mission) if z.threat in ids],
            cells=cells,
        )

    def subscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.append(subscriber)

    def submit(self, command: Command) -> list[Event]:
        """Apply ``command`` and publish the resulting events.

        Raises CommandRejected, leaving the state unchanged.
        """
        events = self._apply(command)
        self._publish(events)
        self._replan_after(events)
        match command:
            case RequestMission():
                created = next(e.mission for e in events if isinstance(e, MissionCreated))
                self.request_plans([created.id])
            case UpdateMission():
                self.request_plans([command.mission])
            case AddThreat() | RemoveThreat():
                change = next(e for e in events if isinstance(e, ThreatAdded | ThreatRemoved))
                self.request_plans(change.missions)
        return events

    def _replan_after(self, events: list[Event]) -> None:
        """Replan every mission concerned by a cut ``events`` scheduled, confirmed or
        cancelled."""
        changed = (CutScheduled, CutConfirmed, CutCancelled)
        jammers = {e.cut.jammer for e in events if isinstance(e, changed)}
        if jammers:
            self.request_plans(
                r.mission.id for r in self.missions.values() if jammers & set(r.constraints.jammers)
            )

    def request_plans(self, mission_ids: Iterable[str] | None = None) -> None:
        """Ask for a plan of each mission (every one by default) on the current cuts, unless
        it has left the ground."""
        if self._replanner is None:
            return
        ids = self.missions if mission_ids is None else mission_ids
        runtimes = [self.missions[i] for i in ids if self.state.missions[i] == "planned"]
        clock = self.clock.state()
        for queued, runtime in enumerate(runtimes, start=1):
            lead_s = 0.0 if clock.paused else queued * PLAN_LEAD_WALL_S * clock.factor
            runtime.revision += 1
            runtime.awaiting = runtime.revision
            cuts = [c for c in self.state.cuts.values() if runtime.concerns(c.jammer)]
            self._replanner.request(
                self.scenario,
                runtime.mission,
                runtime.constraints.grid,
                runtime.revision,
                cuts,
                clock.sim_time_s + lead_s,
                self._plan_done_for(runtime),
            )

    def _plan_done_for(self, runtime: MissionRuntime) -> Callable[[int, MissionPlan | None], None]:
        def done(revision: int, plan: MissionPlan | None) -> None:
            mission = runtime.mission
            if revision != runtime.revision or self.state.missions[mission.id] != "planned":
                return  # a newer plan is on its way, or the drone already flies
            runtime.awaiting = None
            if plan is None:
                text = f"{mission.id}: no route to the target within battery."
                self._publish([Notification(to=mission.pilot_callsign, text=text)])
                return
            runtime.plan = self._plan_updated(runtime, revision, plan)
            self._publish([runtime.plan])

        return done

    def tick(self) -> list[Event]:
        """Advance time-driven behaviour to the current simulated time."""
        now = self.clock.now()
        previous, self._last_tick_s = self._last_tick_s, now
        if now <= previous:
            return []
        scripted = self._scripted(previous, now)
        events = self._cut_events(previous, now)
        for runtime in self.missions.values():
            request = runtime.request
            if (
                request is not None
                and request.status == "pending"
                and request.deadline_s is not None
                and now >= request.deadline_s
            ):
                # No cut without the command: an unanswered request is refused; the pilot's
                # plan B (detour, or straight through anyway) decides the flight.
                events += self._resolve(runtime, False, TIMEOUT_ACTOR)
        stale = [
            runtime.mission.id
            for runtime in self.missions.values()
            if self.state.missions[runtime.mission.id] == "planned"
            and runtime.plan is not None
            and runtime.awaiting is None
            and takeoff_s(runtime.plan.optimised_route) < now
        ]
        for runtime in self.missions.values():
            if runtime.flight is not None:
                events += self._fly(runtime, now)
        self._publish(events)
        self._replan_after(events)
        if stale:
            self.request_plans(stale)
        return scripted + events

    def _scripted(self, after: float, until: float) -> list[Event]:
        """Fire the scripted events with ``after < at_s <= until``, in time order; their events
        are published by ``submit``."""
        due = sorted((e for e in self.scenario.events if after < e.at_s <= until), key=_at)
        events: list[Event] = []
        for event in due:
            try:
                events += self.submit(self._scripted_command(event))
            except CommandRejected as exc:
                label = self.scenario.clock_label(event.at_s)
                text = f"Scripted {event.type} at {label} skipped: {exc}"
                skipped: list[Event] = [Notification(to=self.scenario.command_callsign, text=text)]
                self._publish(skipped)
                events += skipped
        return events

    def _scripted_command(self, event: ScriptedEvent) -> Command:
        command = self.scenario.command_callsign
        match event:
            case ScriptedRestart():
                jammer = self.scenario.friendly_jammer(event.jammer)
                operator = jammer.operator_callsign if jammer else command
                return RestartJammer(actor=operator, jammer=event.jammer)
            case ScriptedThreat():
                fields = event.model_dump(exclude={"type", "at_s"})
                return AddThreat(actor=command, **fields)
            case ScriptedRemoval():
                return RemoveThreat(actor=command, threat=event.threat)
            case ScriptedHold():
                return InjectDelay(
                    actor=SCENARIO_ACTOR, mission=event.mission, delay_s=event.delay_s
                )
            case _:
                assert_never(event)

    def _concerned(self, jammer: str) -> list[MissionRuntime]:
        return [r for r in self.missions.values() if r.concerns(jammer)]

    def _cut_events(self, after: float, until: float) -> list[Event]:
        events: list[Event] = []
        label = self.scenario.clock_label
        for cut, kind in cut_transitions(list(self.state.cuts.values()), after, until):
            jammer = self.scenario.friendly_jammer(cut.jammer)
            operator = jammer.operator_callsign if jammer else cut.jammer
            if kind == "start":
                text = (
                    f"Cut {cut.jammer} now ({label(cut.start_s)}), restart at {label(cut.end_s)}."
                )
            else:
                events.append(JammerRestarted(jammer=cut.jammer, cut=cut.id))
                text = f"Restart {cut.jammer} now ({label(cut.end_s)})."
            events.append(Notification(to=operator, text=text))
        return events

    def _fly(self, runtime: MissionRuntime, now: float) -> list[Event]:
        flight = runtime.flight
        assert flight is not None
        mission = runtime.mission
        events: list[Event] = []
        if not flight.fallback_requested:
            cuts = list(self.state.cuts.values())
            zone = schedule_violation(
                flight, now, runtime.frame, runtime.shapes, cuts, runtime.cells
            )
            threat = None
            if zone is None:
                threat = route_threat(flight, now, runtime.frame, runtime.shapes, runtime.cells)
            if zone is not None:
                text = (
                    f"{mission.drone_id} would cross {zone} outside a confirmed cut "
                    f"(late {flight.delay_s:.0f} s). Computing a safe route."
                )
            elif threat == LINK:
                text = f"{mission.drone_id} would fly out of the pilot's reach. {SAFE_ROUTE}"
            elif threat is not None:
                text = f"New threat {threat} on the route of {mission.drone_id}. {SAFE_ROUTE}"
            if zone is not None or threat is not None:
                flight.fallback_requested = True
                events.append(Notification(to=mission.pilot_callsign, text=text))
                self._request_fallback(runtime, now)
        position, action = position_at(flight.route, flight.route_time(now))
        runtime.drone = DroneState(
            mission=mission.id,
            position=position,
            action=action,
            battery_used_pct=round(100 * flight.airborne_s(now) / mission.battery_max_s, 1),
            delay_s=round(flight.delay_s, 1),
            revision=flight.revision,
        )
        events.append(runtime.drone)
        if flight.arrived(now):
            runtime.flight = None
            self.state.missions[mission.id] = "arrived"
            text = (
                f"{mission.drone_id} on target ({self.scenario.clock_label(now)}), "
                f"battery used {runtime.drone.battery_used_pct:.0f}%."
            )
            events += [
                MissionStatusChanged(mission=mission.id, status="arrived"),
                Notification(to=mission.pilot_callsign, text=text),
            ]
        return events

    def _request_fallback(self, runtime: MissionRuntime, now: float) -> None:
        if self._replanner is None or runtime.flight is None:
            return
        runtime.revision += 1
        route_t = runtime.flight.route_time(now)
        position = position_at(runtime.flight.route, route_t)[0]
        level = runtime.cells.level_at(runtime.flight.route, route_t) if runtime.cells else 0
        self._replanner.request_fallback(
            self.scenario,
            runtime.mission,
            runtime.constraints.grid,
            runtime.revision,
            position,
            now,
            self._fallback_done_for(runtime),
            level=level,
        )

    def _fallback_done_for(
        self, runtime: MissionRuntime
    ) -> Callable[[int, MissionPlan | None], None]:
        def done(revision: int, plan: MissionPlan | None) -> None:
            if revision != runtime.revision or runtime.flight is None:
                return
            pilot = runtime.mission.pilot_callsign
            if plan is None:
                text = "No safe route from the drone position: keep the current one."
                self._publish([Notification(to=pilot, text=text)])
                return
            now = self.clock.now()
            runtime.plan = self._plan_updated(runtime, revision, plan)
            runtime.flight = Flight(
                route=runtime.plan.optimised_route,  # as sent, with altitudes
                revision=revision,
                air_before_s=runtime.flight.airborne_s(now),
                escape_until_s=plan.escape_until_s or 0.0,
            )
            label = self.scenario.clock_label
            text = (
                f"Safe route rev {revision}: on target {label(plan.optimised_route[-1].t)}, "
                f"battery {plan.metrics.optimised_battery_used_pct:.0f}% for this leg."
            )
            self._publish([runtime.plan, Notification(to=pilot, text=text)])

        return done

    def _plan_updated(
        self, runtime: MissionRuntime, revision: int, plan: MissionPlan
    ) -> PlanUpdated:
        """The plan as sent: route points with their altitude above sea level, the profile,
        and the zones that shape each route."""
        terrain, grid, mission = self.scenario.terrain, runtime.constraints.grid, runtime.mission
        cuts = merge_cuts(c for c in self.state.cuts.values() if runtime.concerns(c.jammer))
        windows = [(c.jammer, c.start_s, c.end_s) for c in cuts]
        where = (mission.launch, mission.target, runtime.shapes)
        return PlanUpdated(
            mission=mission.id,
            revision=revision,
            status=plan.status,
            metrics=plan.metrics,
            baseline_route=with_altitude(terrain, grid, plan.baseline_route),
            optimised_route=with_altitude(terrain, grid, plan.optimised_route),
            profile=profile(terrain, plan.optimised_route, grid),
            # Each route choice with its altitude above sea level too, as the planned route.
            choices=[
                c.model_copy(
                    update={
                        "route": with_altitude(terrain, grid, c.route),
                        "reasons": choice_reasons(c, *where, windows, runtime.cells),
                    }
                )
                for c in plan.choices
            ],
            reasons=route_reasons(plan.optimised_route, *where, windows, runtime.cells),
        )

    def _publish(self, events: list[Event]) -> None:
        for event in events:
            for subscriber in self._subscribers:
                subscriber(event)

    def _apply(self, command: Command) -> list[Event]:
        match command:
            case ScheduleCut():
                return self._schedule_cut(command)
            case CancelCut():
                return self._cancel_cut(command)
            case RestartJammer():
                return self._restart_jammer(command)
            case ConfirmCut():
                return self._confirm_cut(command)
            case RequestMission():
                return self._request_mission(command)
            case UpdateMission():
                return self._update_mission(command)
            case ChooseRoute():
                return self._choose_route(command)
            case DecideRoute():
                return self._decide_route(command)
            case LaunchMission():
                return self._launch_mission(command)
            case InjectDelay():
                return self._inject_delay(command)
            case AddThreat():
                return self._add_threat(command)
            case RemoveThreat():
                return self._remove_threat(command)
            case SetClock():
                self.clock.set(factor=command.factor, paused=command.paused)
                return [ClockChanged(clock=self.clock.state())]
            case _:
                assert_never(command)

    def _runtime(self, mission_id: str) -> MissionRuntime:
        runtime = self.missions.get(mission_id)
        if runtime is None:
            raise CommandRejected(f"unknown mission {mission_id}")
        return runtime

    def _schedule_cut(self, command: ScheduleCut) -> list[Event]:
        jammer = self.scenario.friendly_jammer(command.jammer)
        if jammer is None:
            raise CommandRejected(f"unknown friendly jammer {command.jammer}")
        if command.mission is not None:
            self._runtime(command.mission)
        if command.start_s < self.clock.now():
            raise CommandRejected("cut starts in the past")
        for other in self.state.cuts.values():
            if (
                other.jammer == jammer.id
                and command.start_s < other.end_s
                and other.start_s < command.end_s
            ):
                raise CommandRejected(f"overlaps cut {other.id}")
        cut = Cut(
            id=f"cut-{next(self._cut_ids)}",
            jammer=jammer.id,
            start_s=command.start_s,
            end_s=command.end_s,
            status="scheduled",
            requested_by=command.actor,
            requested_for=command.mission,
        )
        self.state.cuts[cut.id] = cut
        label = self.scenario.clock_label
        purpose = f" for {command.mission}" if command.mission else ""
        text = (
            f"Cut {jammer.id} {label(cut.start_s)} to {label(cut.end_s)} "
            f"requested by {command.actor}{purpose}. Confirm?"
        )
        return [
            CutScheduled(cut=cut),
            Notification(
                to=self.scenario.command_callsign,
                text=text,
                action=NotificationAction(command="cut.confirm", cut=cut.id),
            ),
        ]

    def _cancel_cut(self, command: CancelCut) -> list[Event]:
        cut = self.state.cuts.pop(command.cut, None)
        if cut is None:
            raise CommandRejected(f"unknown cut {command.cut}")
        # Several drones may rely on the cut: tell every pilot still concerned.
        label = self.scenario.clock_label
        text = (
            f"Cut {cut.jammer} {label(cut.start_s)} to {label(cut.end_s)} "
            f"cancelled by {command.actor}."
        )
        return [
            CutCancelled(cut=cut),
            *(
                Notification(to=r.mission.pilot_callsign, text=text)
                for r in self._concerned(cut.jammer)
                if self.state.missions[r.mission.id] != "arrived"
            ),
        ]

    def _restart_jammer(self, command: RestartJammer) -> list[Event]:
        jammer = self.scenario.friendly_jammer(command.jammer)
        if jammer is None:
            raise CommandRejected(f"unknown friendly jammer {command.jammer}")
        if command.actor not in (jammer.operator_callsign, self.scenario.command_callsign):
            raise CommandRejected(f"only {jammer.operator_callsign} or the command can restart it")
        now = self.clock.now()
        covering = [
            c
            for c in self.state.cuts.values()
            if c.jammer == jammer.id and c.start_s <= now < c.end_s
        ]
        if not covering:
            raise CommandRejected(f"{jammer.id} is not cut now")
        label = self.scenario.clock_label
        planned_end = max(c.end_s for c in covering)
        text = (
            f"{jammer.id} back on at {label(now)}, before the end of its cut "
            f"({label(planned_end)}). Reported by {command.actor}."
        )
        for cut in covering:
            del self.state.cuts[cut.id]
        # Every pilot still concerned, the operator and the command: a drone in the zone now
        # has its control link jammed; the safety rule reroutes the flights at this tick.
        pilots = {
            r.mission.pilot_callsign
            for r in self._concerned(jammer.id)
            if self.state.missions[r.mission.id] != "arrived"
        }
        told = sorted(pilots | {jammer.operator_callsign, self.scenario.command_callsign})
        return [
            *(CutCancelled(cut=c) for c in covering),
            *(Notification(to=who, text=text) for who in told),
        ]

    def _confirm_cut(self, command: ConfirmCut) -> list[Event]:
        cut = self.state.cuts.get(command.cut)
        if cut is None:
            raise CommandRejected(f"unknown cut {command.cut}")
        if cut.status != "scheduled":
            raise CommandRejected(f"cut {cut.id} is already {cut.status}")
        if command.actor != self.scenario.command_callsign:
            raise CommandRejected("only the command can confirm a cut")
        return self._confirm(cut, command.actor)

    def _confirm(self, cut: Cut, by: str) -> list[Event]:
        """Confirm ``cut`` and tell its jammer operator, who carries it out."""
        confirmed = cut.model_copy(update={"status": "confirmed"})
        self.state.cuts[cut.id] = confirmed
        jammer = self.scenario.friendly_jammer(cut.jammer)
        operator = jammer.operator_callsign if jammer else cut.jammer
        label = self.scenario.clock_label
        text = (
            f"Cut {cut.jammer} {label(cut.start_s)} to {label(cut.end_s)} confirmed by {by}: "
            "switch off at the start, back on at the end."
        )
        return [CutConfirmed(cut=confirmed), Notification(to=operator, text=text)]

    def _choose_route(self, command: ChooseRoute) -> list[Event]:
        """The pilot picks a route of the current plan. ``safe`` stands at once; a route
        needing cuts schedules them and asks the command, which has until the deadline.

        Only the pilot picks: the command answers the cuts a route needs, it does not choose
        the route."""
        runtime = self._runtime(command.mission)
        mission = runtime.mission
        if command.actor != mission.pilot_callsign:
            raise CommandRejected(f"only the pilot of {mission.id} can choose its route")
        status = self.state.missions[mission.id]
        if status != "planned":
            raise CommandRejected(f"mission {mission.id} is already {status}")
        if runtime.plan is None:
            raise CommandRejected("no plan yet")
        choice = next((c for c in runtime.plan.choices if c.kind == command.kind), None)
        if choice is None:
            raise CommandRejected(f"no {command.kind} route in the current plan")
        if not choice.worth_asking:
            raise CommandRejected("this route saves too little to ask the command for a cut")
        deadline: float | None = None
        if choice.cuts:
            first = min([takeoff_s(choice.route), *(c.start_s for c in choice.cuts)])
            deadline = first - DECISION_LEAD_S
            if deadline < self.clock.now():
                raise CommandRejected(
                    "too late for the command to answer before take-off: fly the detour"
                )
            mine = (
                set(runtime.request.cuts) if runtime.request and self._pending(runtime) else set()
            )
            for proposed in choice.cuts:
                for other in self.state.cuts.values():
                    if (
                        other.id not in mine
                        and other.jammer == proposed.jammer
                        and proposed.start_s < other.end_s
                        and other.start_s < proposed.end_s
                    ):
                        raise CommandRejected(f"overlaps cut {other.id}")
        events = self._withdraw(runtime)
        cuts = [
            Cut(
                id=f"cut-{next(self._cut_ids)}",
                jammer=p.jammer,
                start_s=p.start_s,
                end_s=p.end_s,
                status="scheduled",
                requested_by=command.actor,
                requested_for=mission.id,
            )
            for p in choice.cuts
        ]
        for cut in cuts:
            self.state.cuts[cut.id] = cut
            events.append(CutScheduled(cut=cut))
        runtime.request = RouteRequest(
            mission=mission.id,
            kind=choice.kind,
            cuts=[c.id for c in cuts],
            saved_s=choice.saved_s,
            saved_pct=choice.saved_pct,
            deadline_s=deadline,
            on_timeout=command.on_timeout,
            route=choice.route,
            jammers=[c.jammer for c in cuts],
            status="pending" if cuts else "accepted",
            decided_by=None if cuts else command.actor,
        )
        events.append(RouteRequested(request=runtime.request))
        if deadline is not None:
            label = self.scenario.clock_label
            asked = ", ".join(f"{c.jammer} {label(c.start_s)} to {label(c.end_s)}" for c in cuts)
            plan_b = (
                "flies straight through anyway, jammer on"
                if command.on_timeout == "cross"
                else "flies the detour"
            )
            text = (
                f"{command.actor} asks to cut {asked} for {mission.id} "
                f"(-{choice.saved_s:.0f} s, -{choice.saved_pct:.0f}% flight time). "
                f"Answer by {label(deadline)}. If refused or unanswered, the pilot {plan_b}."
            )
            action = NotificationAction(command="route.decide", mission=mission.id)
            events.append(Notification(to=self.scenario.command_callsign, text=text, action=action))
        return events

    def _decide_route(self, command: DecideRoute) -> list[Event]:
        runtime = self._runtime(command.mission)
        if command.actor != self.scenario.command_callsign:
            raise CommandRejected("only the command can answer a route request")
        if not self._pending(runtime):
            raise CommandRejected(f"no route request of {command.mission} awaits an answer")
        return self._resolve(runtime, command.accept, command.actor)

    @staticmethod
    def _pending(runtime: MissionRuntime) -> bool:
        return runtime.request is not None and runtime.request.status == "pending"

    def _resolve(self, runtime: MissionRuntime, accept: bool, by: str) -> list[Event]:
        """Answer the pending request of ``runtime``: confirm or cancel its cuts and tell the
        pilot, and the command when the answer is not its own (timeout, launch)."""
        request = runtime.request
        assert request is not None
        events: list[Event] = []
        for cut_id in request.cuts:
            cut = self.state.cuts.get(cut_id)
            if cut is None:
                continue
            if accept:
                events += self._confirm(cut, by)
            else:
                del self.state.cuts[cut_id]
                events.append(CutCancelled(cut=cut))
        runtime.request = request.model_copy(
            update={"status": "accepted" if accept else "refused", "decided_by": by}
        )
        events.append(RouteRequested(request=runtime.request))
        mission = runtime.mission
        if accept:
            text = f"{mission.id}: {KIND_LABEL[request.kind]} accepted ({by}), fly it as planned."
        elif request.on_timeout == "cross":
            through = ", ".join(request.jammers)
            text = (
                f"{mission.id}: {KIND_LABEL[request.kind]} refused ({by}). As chosen, the drone "
                f"flies straight through {through} anyway: jammer on, control link at risk."
            )
        elif self._has_detour(runtime):
            text = f"{mission.id}: {KIND_LABEL[request.kind]} refused ({by}), fly the detour."
        else:
            text = (
                f"{mission.id}: {KIND_LABEL[request.kind]} refused ({by}). No route without a cut: "
                "the mission cannot start. Ask again or move the launch point."
            )
        events.append(Notification(to=mission.pilot_callsign, text=text))
        if by != self.scenario.command_callsign:
            events.append(Notification(to=self.scenario.command_callsign, text=text))
        return events

    @staticmethod
    def _has_detour(runtime: MissionRuntime) -> bool:
        plan = runtime.plan
        return plan is None or any(c.kind == "safe" for c in plan.choices)

    def _withdraw(self, runtime: MissionRuntime) -> list[Event]:
        """Drop the pending request of ``runtime`` and the cuts it asked for: the pilot picked
        another route."""
        if runtime.request is None or not self._pending(runtime):
            return []
        events: list[Event] = []
        for cut_id in runtime.request.cuts:
            cut = self.state.cuts.pop(cut_id, None)
            if cut is not None:
                events.append(CutCancelled(cut=cut))
        runtime.request = None
        return events

    def _request_mission(self, command: RequestMission) -> list[Event]:
        pilot = self.scenario.pilot(command.actor)
        if pilot is None:
            raise CommandRejected("only a pilot can request a mission")
        mission = pilot.mission(
            self._next_mission_id(), command.launch, command.target, command.departure_s
        )
        try:
            runtime = self._runtime_for(mission)
        except ValueError as exc:
            raise CommandRejected(f"no zones for this mission: {exc}") from exc
        self.scenario = self.scenario.model_copy(
            update={"missions": [*self.scenario.missions, mission]}
        )
        self.missions[mission.id] = runtime
        self.state.missions[mission.id] = "planned"
        text = f"Mission {mission.id} created for {mission.drone_id}, computing the route."
        return [MissionCreated(mission=mission), Notification(to=pilot.callsign, text=text)]

    def _update_mission(self, command: UpdateMission) -> list[Event]:
        runtime = self._runtime(command.mission)
        mission = runtime.mission
        if command.actor != mission.pilot_callsign:
            raise CommandRejected(f"only the pilot of {mission.id} can change it")
        status = self.state.missions[mission.id]
        if status != "planned":
            raise CommandRejected(f"mission {mission.id} is already {status}")
        cross_radars = (
            mission.cross_radars if command.cross_radars is None else command.cross_radars
        )
        updated = mission.model_copy(
            update={
                "launch": command.launch,
                "target": command.target,
                "departure_s": command.departure_s,
                "cross_radars": cross_radars,
            }
        )
        try:
            fresh = self._runtime_for(updated)
        except ValueError as exc:
            raise CommandRejected(f"no zones for this mission: {exc}") from exc
        # In place, revision kept: a plan still on its way for the old points is dropped.
        runtime.mission = updated
        runtime.constraints = fresh.constraints
        runtime.frame = fresh.frame
        runtime.shapes = fresh.shapes
        runtime.plan = None
        # The route picked was for the old points: drop it, and the cuts it asked for.
        dropped = self._drop_request(runtime)
        self.scenario = self.scenario.model_copy(
            update={
                "missions": [updated if m.id == mission.id else m for m in self.scenario.missions]
            }
        )
        text = f"Mission {mission.id} changed by {command.actor}, computing the new route."
        return [
            *dropped,
            MissionUpdated(mission=updated),
            Notification(to=mission.pilot_callsign, text=text),
        ]

    def _drop_request(self, runtime: MissionRuntime) -> list[Event]:
        """Forget the route request of ``runtime``, answered or not, and cancel its cuts."""
        if runtime.request is None:
            return []
        events: list[Event] = []
        for cut_id in runtime.request.cuts:
            cut = self.state.cuts.pop(cut_id, None)
            if cut is not None:
                events.append(CutCancelled(cut=cut))
        runtime.request = None
        return events

    def _next_mission_id(self) -> str:
        """``M<n>``, skipping ids the scenario already uses."""
        taken = self._taken_ids()
        return next(i for n in self._mission_ids if (i := f"M{n}") not in taken)

    def _taken_ids(self) -> set[str]:
        scenario = self.scenario
        return {
            *(m.id for m in scenario.missions),
            *(j.id for j in scenario.friendly_jammers),
            *(t.id for t in scenario.enemy_threats),
        }

    def _add_threat(self, command: AddThreat) -> list[Event]:
        self._command_only(command.actor, "add a threat")
        taken = self._taken_ids()
        prefix = THREAT_PREFIX[command.kind]
        threat_id = next(i for n in itertools.count(1) if (i := f"{prefix}{n}") not in taken)
        threat: EnemyThreat
        match command.kind:
            case "radar":
                assert command.radius_m is not None
                threat = EnemyRadar(
                    id=threat_id,
                    pos=command.pos,
                    radius_m=command.radius_m,
                    position_error_m=command.position_error_m,
                )
            case "jammer":
                assert command.eirp_w is not None and command.bands is not None
                threat = EnemyJammer(
                    id=threat_id,
                    pos=command.pos,
                    eirp_w=command.eirp_w,
                    bands=command.bands,
                    position_error_m=command.position_error_m,
                )
            case "avoid":
                assert command.radius_m is not None
                threat = AvoidZone(id=threat_id, pos=command.pos, radius_m=command.radius_m)
        self._set_threats([*self.scenario.enemy_threats, threat])
        missions = self._concerned_by(threat_id)
        what = {"radar": "radar", "jammer": "enemy jammer", "avoid": "zone to avoid"}
        text = f"New {what[command.kind]} {threat_id}"
        return [
            ThreatAdded(threat=threat, missions=missions),
            *self._threat_notices(text, missions),
        ]

    def _remove_threat(self, command: RemoveThreat) -> list[Event]:
        self._command_only(command.actor, "remove a threat")
        threats = self.scenario.enemy_threats
        if not any(t.id == command.threat for t in threats):
            raise CommandRejected(f"unknown enemy threat {command.threat}")
        missions = self._concerned_by(command.threat)
        self._set_threats([t for t in threats if t.id != command.threat])
        text = f"Threat {command.threat} removed"
        return [
            ThreatRemoved(threat=command.threat, missions=missions),
            *self._threat_notices(text, missions),
        ]

    def _command_only(self, actor: str, what: str) -> None:
        if actor != self.scenario.command_callsign:
            raise CommandRejected(f"only the command can {what}")

    def _set_threats(self, threats: list[EnemyThreat]) -> None:
        """Swap the enemy threats and recompute the zones of every mission.

        Raises CommandRejected, leaving the state unchanged, on a degenerate zone.
        """
        scenario = self.scenario.model_copy(update={"enemy_threats": threats})
        try:
            runtimes = {m: self._runtime_for(r.mission, scenario) for m, r in self.missions.items()}
        except ValueError as exc:
            raise CommandRejected(f"no zones with this threat: {exc}") from exc
        self.scenario = scenario
        for mission_id, fresh in runtimes.items():
            runtime = self.missions[mission_id]
            runtime.constraints, runtime.frame, runtime.shapes, runtime.cells = (
                fresh.constraints,
                fresh.frame,
                fresh.shapes,
                fresh.cells,
            )

    def _concerned_by(self, threat: str) -> list[str]:
        return [m for m, r in self.missions.items() if threat in r.constraints.threats]

    def _threat_notices(self, text: str, missions: list[str]) -> list[Event]:
        """``text`` to the command, with the missions it concerns, and to their pilots."""
        notices: list[Event] = []
        replanned = [m for m in missions if self.state.missions[m] == "planned"]
        flying = [m for m in missions if self.state.missions[m] == "flying"]
        detail = []
        if replanned:
            detail.append(f"replanning {', '.join(replanned)}")
        if flying:
            detail.append(f"in flight: {', '.join(flying)}")
        suffix = f" ({'; '.join(detail)})" if detail else ""
        notices.append(Notification(to=self.scenario.command_callsign, text=f"{text}{suffix}."))
        for mission_id in [*replanned, *flying]:
            mission = self.missions[mission_id].mission
            then = "new route coming" if mission_id in replanned else "check your route"
            notices.append(
                Notification(to=mission.pilot_callsign, text=f"{text} near {mission_id}: {then}.")
            )
        return notices

    def _launch_mission(self, command: LaunchMission) -> list[Event]:
        """With a planner, the drone flies the current plan, which must be flyable."""
        runtime = self._runtime(command.mission)
        mission = runtime.mission
        if command.actor != mission.pilot_callsign:
            raise CommandRejected(f"only the pilot of {mission.id} can launch it")
        status = self.state.missions[mission.id]
        if status != "planned":
            raise CommandRejected(f"mission {mission.id} is already {status}")
        events: list[Event] = []
        if self._replanner is not None:
            plan = runtime.plan
            if plan is None:
                raise CommandRejected("no plan yet")
            request = runtime.request
            through: frozenset[str] = frozenset()
            if (
                request is not None
                and request.cuts
                and request.on_timeout == "cross"
                and request.status in ("pending", "refused")
            ):
                # Plan B "straight anyway": the route picked, through the jammers still on.
                route = request.route
                through = frozenset(request.jammers)
                if request.status == "pending":
                    events += self._resolve(runtime, False, LAUNCH_ACTOR)
            elif self._pending(runtime):
                # Still no answer at take-off: the request lapses and the drone flies the detour.
                safe = next((c for c in plan.choices if c.kind == "safe"), None)
                if safe is None:
                    raise CommandRejected(
                        "no route without a cut (the mission starts in a jamming zone): "
                        "wait for the command's answer"
                    )
                route = safe.route
                events += self._resolve(runtime, False, LAUNCH_ACTOR)
            elif (
                request is not None
                and request.status == "accepted"
                and request.cuts
                and plan.status != "approved"
            ):
                raise CommandRejected("the route is being updated after the answer: try again")
            elif plan.status not in FLYABLE:
                raise CommandRejected("the plan relies on a cut not confirmed yet")
            else:
                route = plan.optimised_route
            revision = plan.revision
            if through:
                grid = runtime.constraints.grid
                # The plan becomes the route flown, so every view (map, mission card, altitude
                # profile, a client joining later) shows the straight route, not the detour.
                runtime.revision += 1
                revision = runtime.revision
                runtime.plan = plan.model_copy(
                    update={
                        "revision": revision,
                        "status": "straight",
                        "optimised_route": with_altitude(self.scenario.terrain, grid, route),
                        "metrics": route_metrics(mission, plan.baseline_route, route),
                        "profile": profile(self.scenario.terrain, route, grid),
                        "choices": [],
                    }
                )
                events.append(runtime.plan)
            # Launched after the planned take-off: the flight starts late by the difference.
            late = max(0.0, self.clock.now() - takeoff_s(route))
            runtime.flight = Flight(
                route=route, revision=revision, delay_s=late, through_jammers=through
            )
        self.state.missions[mission.id] = "flying"
        return [*events, MissionStatusChanged(mission=mission.id, status="flying")]

    def _inject_delay(self, command: InjectDelay) -> list[Event]:
        runtime = self._runtime(command.mission)
        if runtime.flight is None:
            raise CommandRejected(f"no drone in flight for {command.mission}")
        runtime.flight.hold(self.clock.now(), command.delay_s)
        mission = runtime.mission
        text = f"{mission.drone_id} held {command.delay_s:.0f} s by {command.actor}."
        return [Notification(to=mission.pilot_callsign, text=text)]


def _at(event: ScriptedEvent) -> float:
    return event.at_s
