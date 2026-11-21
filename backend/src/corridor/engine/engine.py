"""The engine: single owner of world state, applies commands and publishes events.

Everything runs on the asyncio event loop thread and ``submit`` never awaits, so no two
commands interleave and the state needs no lock. Subscribers are called synchronously and
must not block (the WebSocket hub only enqueues).

Several missions share the jammers and the cuts. Each mission has its own runtime: plan,
flight, drone, revision. A mission only reacts to the friendly jammers that concern it
(``MissionConstraints.jammers``).

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
    CancelCut,
    Command,
    ConfirmCut,
    InjectDelay,
    LaunchMission,
    ScheduleCut,
    SetClock,
)
from corridor.domain.events import (
    ClockChanged,
    CutCancelled,
    CutConfirmed,
    CutScheduled,
    DroneState,
    Event,
    JammerRestarted,
    MissionStatusChanged,
    Notification,
    NotificationAction,
    PlanUpdated,
)
from corridor.domain.geo import LocalFrame
from corridor.domain.planner.plan import MissionPlan
from corridor.domain.safety import (
    Flight,
    cut_transitions,
    position_at,
    schedule_violation,
    takeoff_s,
)
from corridor.domain.scenario import Mission, Scenario
from corridor.domain.state import Cut, WorldState
from corridor.domain.zones import MissionConstraints, ThreatShape, mission_shapes
from corridor.engine.clock import SimClock
from corridor.engine.replan import Replanner

Subscriber = Callable[[Event], None]

PLAN_LEAD_WALL_S = 3.0
"""Wall-clock seconds a plan may take. A replan's earliest takeoff is that far ahead in
simulated time, per plan queued before it, so the route does not start in the past by the
time it arrives."""


FLYABLE = ("approved", "baseline")
"""Plan statuses a drone may launch on. ``preview`` waits for the operator's confirmation."""


class CommandRejected(Exception):
    """The command breaks a domain rule. The message is shown to the sender."""


@dataclass
class MissionRuntime:
    """What the engine keeps for one mission, next to its status in ``WorldState``."""

    mission: Mission
    constraints: MissionConstraints
    frame: LocalFrame
    shapes: list[ThreatShape]
    """Zones of the concerned threats, in ``frame``, for the safety rule."""
    plan: PlanUpdated | None = None
    flight: Flight | None = None
    drone: DroneState | None = None
    revision: int = 0
    awaiting: int | None = None
    """Revision of the mission plan on its way, None when none is."""

    def concerns(self, jammer: str) -> bool:
        return jammer in self.constraints.jammers


class Engine:
    def __init__(
        self,
        scenario: Scenario,
        constraints: dict[str, MissionConstraints],
        clock: SimClock,
        replanner: Replanner | None = None,
    ) -> None:
        """``constraints`` by mission id, one per mission. Without ``replanner``, nothing is
        planned."""
        self.scenario = scenario
        self.clock = clock
        self.state = WorldState.from_scenario(scenario)
        self.missions: dict[str, MissionRuntime] = {}
        for mission in scenario.missions:
            concerned = constraints[mission.id]
            ids = {*concerned.jammers, *concerned.threats}
            self.missions[mission.id] = MissionRuntime(
                mission=mission,
                constraints=concerned,
                frame=LocalFrame(mission.launch),
                shapes=[z for z in mission_shapes(scenario, mission) if z.threat in ids],
            )
        self._replanner = replanner
        self._subscribers: list[Subscriber] = []
        self._cut_ids = itertools.count(1)
        self._last_tick_s = clock.now()

    def subscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.append(subscriber)

    def submit(self, command: Command) -> list[Event]:
        """Apply ``command`` and publish the resulting events.

        Raises CommandRejected, leaving the state unchanged.
        """
        events = self._apply(command)
        self._publish(events)
        match command:
            case ScheduleCut():
                self.request_plans(m.mission.id for m in self._concerned(command.jammer))
            case ConfirmCut() | CancelCut():
                cut = next(e.cut for e in events if isinstance(e, CutConfirmed | CutCancelled))
                self.request_plans(m.mission.id for m in self._concerned(cut.jammer))
        return events

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
                runtime.mission.id,
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
            runtime.plan = _plan_updated(mission.id, revision, plan)
            self._publish([runtime.plan])

        return done

    def tick(self) -> list[Event]:
        """Advance time-driven behaviour to the current simulated time."""
        now = self.clock.now()
        previous, self._last_tick_s = self._last_tick_s, now
        if now <= previous:
            return []
        events = self._cut_events(previous, now)
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
        if stale:
            self.request_plans(stale)
        return events

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
            zone = schedule_violation(flight, now, runtime.frame, runtime.shapes, cuts)
            if zone is not None:
                flight.fallback_requested = True
                text = (
                    f"{mission.drone_id} would cross {zone} outside a confirmed cut "
                    f"(late {flight.delay_s:.0f} s). Computing a safe route."
                )
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
        position = position_at(runtime.flight.route, runtime.flight.route_time(now))[0]
        self._replanner.request_fallback(
            runtime.mission.id,
            runtime.revision,
            position,
            now,
            self._fallback_done_for(runtime),
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
            runtime.plan = _plan_updated(runtime.mission.id, revision, plan)
            runtime.flight = Flight(
                route=plan.optimised_route,
                revision=revision,
                air_before_s=runtime.flight.airborne_s(now),
            )
            label = self.scenario.clock_label
            text = (
                f"Safe route rev {revision}: on target {label(plan.optimised_route[-1].t)}, "
                f"battery {plan.metrics.optimised_battery_used_pct:.0f}% for this leg."
            )
            self._publish([runtime.plan, Notification(to=pilot, text=text)])

        return done

    def notify(self, to: str, text: str) -> None:
        """Feed message from an adapter (e.g. the route was sent to the pilot's ATAK)."""
        self._publish([Notification(to=to, text=text)])

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
            case ConfirmCut():
                return self._confirm_cut(command)
            case LaunchMission():
                return self._launch_mission(command)
            case InjectDelay():
                return self._inject_delay(command)
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
                to=jammer.operator_callsign,
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

    def _confirm_cut(self, command: ConfirmCut) -> list[Event]:
        cut = self.state.cuts.get(command.cut)
        if cut is None:
            raise CommandRejected(f"unknown cut {command.cut}")
        if cut.status != "scheduled":
            raise CommandRejected(f"cut {cut.id} is already {cut.status}")
        jammer = self.scenario.friendly_jammer(cut.jammer)
        if jammer is None or command.actor != jammer.operator_callsign:
            raise CommandRejected(f"only the operator of {cut.jammer} can confirm this cut")
        confirmed = cut.model_copy(update={"status": "confirmed"})
        self.state.cuts[cut.id] = confirmed
        return [CutConfirmed(cut=confirmed)]

    def _launch_mission(self, command: LaunchMission) -> list[Event]:
        """With a planner, the drone flies the current plan, which must be flyable."""
        runtime = self._runtime(command.mission)
        mission = runtime.mission
        if command.actor not in (mission.pilot_callsign, self.scenario.command_callsign):
            raise CommandRejected(f"only the pilot of {mission.id} or the command can launch it")
        status = self.state.missions[mission.id]
        if status != "planned":
            raise CommandRejected(f"mission {mission.id} is already {status}")
        if self._replanner is not None:
            plan = runtime.plan
            if plan is None:
                raise CommandRejected("no plan yet")
            if plan.status not in FLYABLE:
                raise CommandRejected("the plan relies on a cut not confirmed yet")
            route = plan.optimised_route
            # Launched after the planned take-off: the flight starts late by the difference.
            late = max(0.0, self.clock.now() - takeoff_s(route))
            runtime.flight = Flight(route=route, revision=plan.revision, delay_s=late)
        self.state.missions[mission.id] = "flying"
        return [MissionStatusChanged(mission=mission.id, status="flying")]

    def _inject_delay(self, command: InjectDelay) -> list[Event]:
        runtime = self._runtime(command.mission)
        if runtime.flight is None:
            raise CommandRejected(f"no drone in flight for {command.mission}")
        runtime.flight.hold(self.clock.now(), command.delay_s)
        mission = runtime.mission
        text = f"{mission.drone_id} held {command.delay_s:.0f} s by {command.actor}."
        return [Notification(to=mission.pilot_callsign, text=text)]


def _plan_updated(mission_id: str, revision: int, plan: MissionPlan) -> PlanUpdated:
    return PlanUpdated(
        mission=mission_id,
        revision=revision,
        status=plan.status,
        metrics=plan.metrics,
        baseline_route=plan.baseline_route,
        optimised_route=plan.optimised_route,
    )
