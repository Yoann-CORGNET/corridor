"""Engine with the real planner, run synchronously: launch rules, flight, safety rule."""

import pytest

from corridor.domain.commands import (
    AddThreat,
    CancelCut,
    ConfirmCut,
    InjectDelay,
    LaunchMission,
    RequestMission,
    RestartJammer,
    ScheduleCut,
    SetClock,
)
from corridor.domain.events import (
    DroneState,
    Event,
    JammerRestarted,
    MissionStatusChanged,
    Notification,
    PlanUpdated,
    ThreatAdded,
)
from corridor.domain.geo import Grid, LocalFrame, Position
from corridor.domain.planner.plan import plan_fallback, plan_mission
from corridor.domain.safety import Flight, position_at, schedule_violation
from corridor.domain.scenario import (
    Mission,
    Scenario,
    ScriptedEvent,
    ScriptedHold,
    ScriptedRestart,
    ScriptedThreat,
)
from corridor.domain.state import Cut
from corridor.domain.zones import mission_constraints, mission_shapes
from corridor.engine.clock import SimClock
from corridor.engine.engine import CommandRejected, Engine
from corridor.engine.replan import PlanDone

PILOT, EW, COMMAND = "PILOT-1", "EW-J3", "CMD"
CUT = (900.0, 1380.0)
"""8 min: with the demo powers, J3's zone takes about 300 s to cross, plus 30 s and 60 s."""


class SyncReplanner:
    """Runs the real planner at once, in the calling thread."""

    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario

    def request(
        self,
        scenario: Scenario,
        mission: Mission,
        grid: Grid,
        revision: int,
        cuts: list[Cut],
        earliest_departure_s: float,
        done: PlanDone,
    ) -> None:
        done(revision, plan_mission(scenario, mission, grid, cuts, earliest_departure_s))

    def request_fallback(
        self,
        scenario: Scenario,
        mission: Mission,
        grid: Grid,
        revision: int,
        position: Position,
        now_s: float,
        done: PlanDone,
        level: int = 0,
    ) -> None:
        done(revision, plan_fallback(scenario, mission, grid, position, now_s, level))

    def close(self) -> None:
        pass


class World:
    def __init__(self, scenario: Scenario) -> None:
        self.wall = 0.0
        self.engine = Engine(
            scenario,
            100,
            SimClock(1, wall=lambda: self.wall),
            SyncReplanner(scenario),
        )
        self.events: list[Event] = []
        self.engine.subscribe(self.events.append)
        self.engine.request_plans()
        self.m1 = self.engine.missions["M1"]

    def run_to(self, sim_s: float, step: float = 5.0) -> None:
        if self.engine.clock.state().paused:
            self.engine.submit(SetClock(actor="HQ", paused=False))
        while self.wall < sim_s:
            self.wall = min(sim_s, self.wall + step)
            self.engine.tick()

    def of[T](self, kind: type[T]) -> list[T]:
        return [e for e in self.events if isinstance(e, kind)]

    def cut(self, *, confirm: bool = True) -> str:
        start, end = CUT
        scheduled = self.engine.submit(
            ScheduleCut(actor=PILOT, jammer="J3", start_s=start, end_s=end)
        )[0]
        cut_id: str = scheduled.cut.id  # type: ignore[union-attr]
        if confirm:
            self.engine.submit(ConfirmCut(actor=COMMAND, cut=cut_id))
        return cut_id


@pytest.fixture(scope="module")
def flown(scenario_module: Scenario) -> World:
    """One full flight on a confirmed cut, shared by the tests that only read it."""
    world = World(scenario_module)
    world.cut()
    world.engine.submit(LaunchMission(actor=PILOT, mission="M1"))
    world.run_to(1800)
    return world


def test_launch_waits_for_the_operator(scenario: Scenario) -> None:
    world = World(scenario)
    world.cut(confirm=False)
    assert world.m1.plan is not None and world.m1.plan.status == "preview"
    with pytest.raises(CommandRejected, match="not confirmed"):
        world.engine.submit(LaunchMission(actor=PILOT, mission="M1"))


def test_inject_delay_needs_a_flight(scenario: Scenario) -> None:
    with pytest.raises(CommandRejected, match="no drone"):
        World(scenario).engine.submit(InjectDelay(actor="HQ", mission="M1", delay_s=60))


def test_on_time_flight_reaches_the_target(flown: World) -> None:
    states = flown.of(DroneState)
    assert states[0].action == "ground"
    assert any(s.action == "fly" for s in states)
    assert all(s.delay_s == 0 for s in states)
    assert flown.engine.state.missions["M1"] == "arrived"
    assert MissionStatusChanged(mission="M1", status="arrived") in flown.events
    assert not any(p.status == "fallback" for p in flown.of(PlanUpdated))
    approved = next(p for p in flown.of(PlanUpdated) if p.status == "approved")
    assert states[-1].battery_used_pct == pytest.approx(
        approved.metrics.optimised_battery_used_pct, abs=1.0
    )


def test_operator_is_told_when_to_cut_and_restart(flown: World) -> None:
    (restart,) = flown.of(JammerRestarted)
    assert restart.jammer == "J3"
    texts = [n.text for n in flown.of(Notification) if n.to == EW]
    assert any(t.startswith("Cut J3 now") for t in texts)
    assert any(t.startswith("Restart J3 now") for t in texts)


def test_late_drone_switches_to_a_safe_route(scenario: Scenario) -> None:
    world = World(scenario)
    world.cut()
    world.engine.submit(LaunchMission(actor=PILOT, mission="M1"))
    takeoff = next(p.t for p in world.m1.plan.optimised_route if p.action != "ground")  # type: ignore[union-attr]
    world.run_to(takeoff + 30)  # in the air, before J3
    world.engine.submit(InjectDelay(actor="HQ", mission="M1", delay_s=240))
    world.run_to(takeoff + 40)
    fallback = [p for p in world.of(PlanUpdated) if p.status == "fallback"]
    assert len(fallback) == 1
    assert world.m1.flight is not None
    assert world.m1.flight.revision == fallback[0].revision
    world.run_to(4000)
    assert world.engine.state.missions["M1"] == "arrived"
    # The battery counts the held seconds before the switch, not the whole hold.
    assert world.of(DroneState)[-1].battery_used_pct < 60


def test_cancelling_a_used_cut_in_flight_falls_back(scenario: Scenario) -> None:
    world = World(scenario)
    cut_id = world.cut()
    world.engine.submit(LaunchMission(actor=PILOT, mission="M1"))
    takeoff = next(p.t for p in world.m1.plan.optimised_route if p.action != "ground")  # type: ignore[union-attr]
    world.run_to(takeoff + 30)
    world.engine.submit(CancelCut(actor=EW, cut=cut_id))
    world.run_to(takeoff + 40)
    assert world.m1.plan is not None and world.m1.plan.status == "fallback"


def test_stale_plan_is_refreshed_before_launch(scenario: Scenario) -> None:
    world = World(scenario)
    first = world.m1.plan
    assert first is not None
    world.run_to(first.optimised_route[0].t + 60)
    assert len(world.of(PlanUpdated)) >= 2


def test_missions_fly_independently(two_missions: Scenario) -> None:
    world = World(two_missions)
    assert world.engine.missions["M2"].plan is not None
    world.engine.submit(LaunchMission(actor=PILOT, mission="M1"))
    world.run_to(1800)
    assert world.engine.state.missions == {"M1": "arrived", "M2": "planned"}
    assert {s.mission for s in world.of(DroneState)} == {"M1"}


def test_a_requested_mission_is_planned_and_flies(scenario: Scenario) -> None:
    world = World(scenario)
    m1 = world.m1.mission
    world.engine.submit(
        RequestMission(actor=PILOT, launch=m1.launch, target=m1.target, departure_s=600)
    )
    (plan,) = [p for p in world.of(PlanUpdated) if p.mission == "M2"]
    assert plan.status == "baseline"
    world.engine.submit(LaunchMission(actor=PILOT, mission="M2"))
    world.run_to(1800)
    assert world.engine.state.missions["M2"] == "arrived"


# --- 3D ------------------------------------------------------------------------------------


def three_d(scenario: Scenario) -> Scenario:
    terrain = scenario.terrain.model_copy(update={"mode": "3d"})
    return scenario.model_copy(update={"terrain": terrain})


def test_3d_flight_on_a_confirmed_cut_needs_no_fallback(scenario: Scenario) -> None:
    world = World(three_d(scenario))
    assert world.m1.cells is not None  # the safety rule checks the zones per level
    world.cut()
    plan = world.m1.plan
    assert plan is not None and plan.status == "approved"
    assert {p.alt_m for p in plan.optimised_route} - {0.0} <= set(scenario.terrain.levels_m)
    assert all(p.amsl_m is not None for p in plan.optimised_route)
    assert plan.profile and all(
        p.amsl_m is not None and p.amsl_m >= p.ground_m for p in plan.profile
    )
    world.engine.submit(LaunchMission(actor=PILOT, mission="M1"))
    world.run_to(1800)
    assert world.engine.state.missions["M1"] == "arrived"
    assert not any(p.status == "fallback" for p in world.of(PlanUpdated))


def test_3d_late_drone_falls_back_from_its_level(scenario: Scenario) -> None:
    world = World(three_d(scenario))
    world.cut()
    world.engine.submit(LaunchMission(actor=PILOT, mission="M1"))
    plan = world.m1.plan
    assert plan is not None
    takeoff = next(p.t for p in plan.optimised_route if p.action != "ground")
    world.run_to(takeoff + 60)
    world.engine.submit(InjectDelay(actor="HQ", mission="M1", delay_s=300))
    world.run_to(takeoff + 80)
    fallback = next(p for p in world.of(PlanUpdated) if p.status == "fallback")
    flight = world.m1.flight
    assert flight is not None and flight.route == fallback.optimised_route
    assert fallback.optimised_route[0].alt_m in scenario.terrain.levels_m


def test_safety_rule_leaves_a_jammer_the_pilot_flies_through_on_purpose(scenario: Scenario) -> None:

    mission = scenario.missions[0]
    grid = mission_constraints(scenario, mission, 100).grid
    j3_cut = Cut(id="c", jammer="J3", start_s=900, end_s=1260, status="confirmed", requested_by="t")
    planned = plan_mission(scenario, mission, grid, [j3_cut])
    assert planned is not None
    route = planned.optimised_route  # straight through J3 during its cut
    frame, shapes = LocalFrame(mission.launch), mission_shapes(scenario, mission)
    # Without the cut J3 is on: the rule flags the crossing...
    assert schedule_violation(Flight(route=route, revision=1), 0, frame, shapes, []) == "J3"
    # ...unless the pilot chose to fly straight through anyway.
    on_purpose = Flight(route=route, revision=1, through_jammers=frozenset({"J3"}))
    assert schedule_violation(on_purpose, 0, frame, shapes, []) is None


def test_two_cuts_end_to_end_leave_no_gap_for_the_safety_rule(scenario: Scenario) -> None:
    """A cut extended by a second one, starting when the first ends: the margins apply to the
    whole, so a drone crossing at the junction is not flagged."""
    mission = scenario.missions[0]
    grid = mission_constraints(scenario, mission, 100).grid
    whole = Cut(id="w", jammer="J3", start_s=900, end_s=1260, status="confirmed", requested_by="t")
    planned = plan_mission(scenario, mission, grid, [whole])
    assert planned is not None
    route = planned.optimised_route
    split = [
        whole.model_copy(update={"id": "a", "end_s": 1000}),
        whole.model_copy(update={"id": "b", "start_s": 1000}),
    ]
    frame, shapes = LocalFrame(mission.launch), mission_shapes(scenario, mission)
    assert schedule_violation(Flight(route=route, revision=1), 0, frame, shapes, split) is None


# --- threat added in flight -----------------------------------------------------------------


def launched(scenario: Scenario) -> tuple[World, float]:
    """M1 flying on a confirmed J3 cut, 30 s after take-off."""
    world = World(scenario)
    world.cut()
    world.engine.submit(LaunchMission(actor=PILOT, mission="M1"))
    plan = world.m1.plan
    assert plan is not None
    takeoff = next(p.t for p in plan.optimised_route if p.action != "ground")
    world.run_to(takeoff + 30)
    return world, takeoff


def add_radar(world: World, pos: Position, radius_m: float = 400) -> str:
    command = world.engine.scenario.command_callsign
    events = world.engine.submit(AddThreat(actor=command, kind="radar", pos=pos, radius_m=radius_m))
    return next(e.threat.id for e in events if isinstance(e, ThreatAdded))


def test_a_radar_added_on_the_route_in_flight_falls_back(scenario: Scenario) -> None:
    world, takeoff = launched(scenario)
    route = world.m1.plan.optimised_route  # type: ignore[union-attr]
    ahead = position_at(route, takeoff + 200)[0]
    radar = add_radar(world, ahead)
    world.run_to(takeoff + 40)
    fallback = [p for p in world.of(PlanUpdated) if p.status == "fallback"]
    assert len(fallback) == 1
    texts = [n.text for n in world.of(Notification) if n.to == PILOT]
    assert any(t.startswith(f"New threat {radar} on the route") for t in texts)
    frame = LocalFrame(world.m1.mission.launch)
    zone = next(z for z in world.m1.shapes if z.threat == radar)
    new = fallback[0].optimised_route
    for i in range(201):
        t = new[0].t + (new[-1].t - new[0].t) * i / 200
        assert not zone.contains(*frame.to_xy(position_at(new, t)[0]))
    world.run_to(4000)
    assert world.engine.state.missions["M1"] == "arrived"


def test_a_radar_added_over_the_drone_is_left_by_the_nearest_way(scenario: Scenario) -> None:
    world, takeoff = launched(scenario)
    here = position_at(world.m1.flight.route, takeoff + 30)[0]  # type: ignore[union-attr]
    radar = add_radar(world, here, radius_m=300)
    world.run_to(takeoff + 35)
    fallback = next(p for p in world.of(PlanUpdated) if p.status == "fallback")
    route = fallback.optimised_route
    frame = LocalFrame(world.m1.mission.launch)
    zone = next(z for z in world.m1.shapes if z.threat == radar)
    assert zone.contains(*frame.to_xy(Position(lat=route[0].lat, lon=route[0].lon)))
    # Out of the zone after the first leg, and it stays out.
    assert not zone.contains(*frame.to_xy(Position(lat=route[1].lat, lon=route[1].lon)))
    assert route[1].t - route[0].t < 600 / world.m1.mission.cruise_speed_mps


def test_a_threat_away_from_the_route_changes_nothing_in_flight(scenario: Scenario) -> None:
    world, takeoff = launched(scenario)
    target = world.m1.mission.target
    far = Position(lat=target.lat + 0.08, lon=target.lon)  # about 9 km north of the target
    add_radar(world, far, radius_m=300)
    world.run_to(takeoff + 60)
    assert not any(p.status == "fallback" for p in world.of(PlanUpdated))


def test_3d_a_radar_added_on_the_route_falls_back(scenario: Scenario) -> None:
    world, takeoff = launched(three_d(scenario))
    route = world.m1.plan.optimised_route  # type: ignore[union-attr]
    add_radar(world, position_at(route, takeoff + 200)[0])
    world.run_to(takeoff + 40)
    fallback = next(p for p in world.of(PlanUpdated) if p.status == "fallback")
    assert fallback.optimised_route[0].alt_m in scenario.terrain.levels_m


# --- reasons --------------------------------------------------------------------------------


def test_plans_carry_their_reasons(scenario: Scenario) -> None:
    world = World(scenario)
    baseline = world.m1.plan
    assert baseline is not None and baseline.status == "baseline"
    assert ("J3", "avoided") in [(r.threat, r.effect) for r in baseline.reasons]
    one_cut = next(c for c in baseline.choices if c.kind == "one_cut")
    assert ("J3", "crossed_in_cut") in [(r.threat, r.effect) for r in one_cut.reasons]
    world.cut()
    approved = world.m1.plan
    assert approved is not None and approved.status == "approved"
    j3 = next(r for r in approved.reasons if r.threat == "J3")
    assert j3.effect == "crossed_in_cut"
    sent = [p for p in world.of(PlanUpdated) if p.reasons]
    assert sent  # on the wire too


# --- scripted timeline, jammer back on ---------------------------------------------------------


def script(world: World, *events: ScriptedEvent) -> None:
    world.engine.scenario = world.engine.scenario.model_copy(update={"events": list(events)})


def crossing(world: World) -> tuple[float, float]:
    """When the approved route is inside J3's zone."""
    plan = world.m1.plan
    assert plan is not None and plan.status == "approved"
    j3 = next(r for r in plan.reasons if r.threat == "J3")
    assert j3.enter_s is not None and j3.exit_s is not None
    return j3.enter_s, j3.exit_s


def test_jammer_back_on_while_the_drone_crosses_reroutes_it_out(scenario: Scenario) -> None:
    world = World(scenario)
    world.cut()
    enter, _ = crossing(world)
    back_on = enter + 40
    script(world, ScriptedRestart(at_s=back_on, jammer="J3"))
    world.engine.submit(LaunchMission(actor=PILOT, mission="M1"))
    world.run_to(back_on + 5)
    assert not world.engine.state.cuts  # the cut ended early
    texts = [n.text for n in world.of(Notification) if n.to == PILOT]
    assert any(t.startswith("J3 back on at") for t in texts)
    assert any(n.to == COMMAND and n.text.startswith("J3 back on") for n in world.of(Notification))
    fallback = next(p for p in world.of(PlanUpdated) if p.status == "fallback")
    route = fallback.optimised_route
    frame = LocalFrame(world.m1.mission.launch)
    j3 = next(z for z in world.m1.shapes if z.threat == "J3")
    # Caught inside: straight out by the nearest way, then the safe route.
    assert j3.contains(*frame.to_xy(Position(lat=route[0].lat, lon=route[0].lon)))
    assert not j3.contains(*frame.to_xy(Position(lat=route[1].lat, lon=route[1].lon)))
    # Getting out is not a new violation: one fallback, then the drone reaches the target.
    world.run_to(route[1].t + 60)
    assert len([p for p in world.of(PlanUpdated) if p.status == "fallback"]) == 1
    world.run_to(4000)
    assert world.engine.state.missions["M1"] == "arrived"


def test_scripted_threat_and_hold_fire_once_at_their_time(scenario: Scenario) -> None:
    world, takeoff = launched(scenario)
    target = world.m1.mission.target
    far = Position(lat=target.lat + 0.08, lon=target.lon)
    script(
        world,
        ScriptedThreat(at_s=takeoff + 60, kind="radar", pos=far, radius_m=300),
        ScriptedHold(at_s=takeoff + 90, mission="M1", delay_s=30),
    )
    world.run_to(takeoff + 55)
    assert not world.of(ThreatAdded)
    world.run_to(takeoff + 200)
    (added,) = world.of(ThreatAdded)
    assert added.threat.id == "R2" and added.threat.pos == far
    assert world.m1.flight is not None and world.m1.flight.delay_s == 30
    assert any("held 30 s by scenario" in n.text for n in world.of(Notification))


def test_a_scripted_event_that_cannot_apply_is_skipped_and_told(scenario: Scenario) -> None:
    world = World(scenario)
    script(world, ScriptedHold(at_s=10, mission="M1", delay_s=30))  # still on the ground
    world.run_to(20)
    skipped = [n for n in world.of(Notification) if n.to == COMMAND and "skipped" in n.text]
    assert len(skipped) == 1 and "no drone in flight" in skipped[0].text
    world.run_to(40)
    assert len([n for n in world.of(Notification) if "skipped" in n.text]) == 1


def test_only_the_operator_or_the_command_restarts_a_jammer_that_is_cut(scenario: Scenario) -> None:
    world = World(scenario)
    world.cut()
    with pytest.raises(CommandRejected, match="not cut now"):
        world.engine.submit(RestartJammer(actor=EW, jammer="J3"))  # the cut starts at 900
    world.run_to(CUT[0] + 10)
    with pytest.raises(CommandRejected, match="only EW-J3 or the command"):
        world.engine.submit(RestartJammer(actor=PILOT, jammer="J3"))
    world.engine.submit(RestartJammer(actor=EW, jammer="J3"))
    assert not world.engine.state.cuts
