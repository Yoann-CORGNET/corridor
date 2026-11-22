from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from corridor.domain.scenario import EnemyRadar, Scenario, load_scenario
from corridor.domain.state import WorldState
from corridor.domain.zones import constraints_by_mission


def _data(scenario: Scenario) -> dict[str, Any]:
    data: dict[str, Any] = scenario.model_dump(mode="json")
    return data


def test_demo_scenario_loads(scenario: Scenario) -> None:
    jammer = scenario.friendly_jammer("J3")
    assert jammer is not None
    assert jammer.operator_callsign == "EW-J3"
    assert scenario.friendly_jammer("R1") is None
    assert isinstance(scenario.enemy_threats[0], EnemyRadar)


def test_enemy_radar_threat_loads() -> None:
    data = {
        "start_time": "2026-11-21T14:00:00Z",
        "js_threshold_db": 0,
        "reference_link": {"eirp_w": 10, "range_m": 5000},
        "command_callsign": "CMD",
        "friendly_jammers": [],
        "enemy_threats": [
            {"type": "radar", "id": "R1", "pos": {"lat": 48.0, "lon": 11.0}, "radius_m": 2000}
        ],
        "pilots": [
            {
                "callsign": "PILOT-1",
                "drone": {
                    "id": "UAV-1",
                    "band": "2.4GHz",
                    "link_eirp_w": 10,
                    "cruise_speed_mps": 15,
                    "battery_max_s": 1800,
                },
            }
        ],
        "missions": [
            {
                "id": "M1",
                "pilot_callsign": "PILOT-1",
                "launch": {"lat": 48.0, "lon": 11.0},
                "target": {"lat": 48.1, "lon": 11.1},
                "departure_s": 600,
            }
        ],
    }
    s = Scenario.model_validate(data)
    assert isinstance(s.enemy_threats[0], EnemyRadar)


def test_clock_label_uses_start_time(scenario: Scenario) -> None:
    assert scenario.clock_label(15 * 60 + 30) == "14:15:30"


def test_scenario_cuts_start_scheduled(scenario: Scenario) -> None:
    data = _data(scenario)
    data["friendly_jammers"][0]["cuts"] = [{"start_s": 900, "end_s": 1260}]
    state = WorldState.from_scenario(Scenario.model_validate(data))
    (cut,) = state.cuts.values()
    assert (cut.jammer, cut.start_s, cut.end_s, cut.status) == ("J3", 900, 1260, "scheduled")


def test_overlapping_scenario_cuts_are_rejected(scenario: Scenario) -> None:
    data = _data(scenario)
    data["friendly_jammers"][0]["cuts"] = [
        {"start_s": 900, "end_s": 1260},
        {"start_s": 1200, "end_s": 1500},
    ]
    with pytest.raises(ValidationError, match="overlapping cuts"):
        Scenario.model_validate(data)


def test_duplicate_ids_are_rejected(scenario: Scenario) -> None:
    data = _data(scenario)
    data["enemy_threats"][0]["id"] = "J3"
    with pytest.raises(ValidationError, match="duplicate ids"):
        Scenario.model_validate(data)


def test_unknown_threat_type_is_rejected(scenario: Scenario) -> None:
    data = _data(scenario)
    data["enemy_threats"][0]["type"] = "sam"
    with pytest.raises(ValidationError):
        Scenario.model_validate(data)


def test_unknown_field_is_rejected(scenario: Scenario) -> None:
    data = _data(scenario)
    data["missions"][0]["cruise_speed_ms"] = 15
    with pytest.raises(ValidationError):
        Scenario.model_validate(data)


def test_roles_come_from_the_scenario(two_missions: Scenario) -> None:
    roles = {c: two_missions.role_of(c) for c in ("CMD", "PILOT-2", "EW-J4", "NOBODY")}
    assert roles == {"CMD": "command", "PILOT-2": "pilot", "EW-J4": "operator", "NOBODY": None}
    assert two_missions.mission("M2") is not None
    assert two_missions.mission("J4") is None


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("callsign", "PILOT-1", "duplicate callsigns"),
        ("callsign", "CMD", "duplicate callsigns"),
        ("callsign", "EW-J3", "duplicate callsigns"),
    ],
)
def test_pilots_need_their_own_callsign(
    two_missions: Scenario, field: str, value: str, reason: str
) -> None:
    data = _data(two_missions)
    data["pilots"][1][field] = value
    with pytest.raises(ValidationError, match=reason):
        Scenario.model_validate(data)


def test_pilots_need_their_own_drone(two_missions: Scenario) -> None:
    data = _data(two_missions)
    data["pilots"][1]["drone"]["id"] = "UAV-1"
    with pytest.raises(ValidationError, match="duplicate drones"):
        Scenario.model_validate(data)


def test_missions_need_their_own_id(two_missions: Scenario) -> None:
    data = _data(two_missions)
    data["missions"][1]["id"] = "M1"
    with pytest.raises(ValidationError, match="duplicate ids"):
        Scenario.model_validate(data)


def test_a_file_mission_takes_its_drone_from_the_pilot(two_missions: Scenario) -> None:
    data = _data(two_missions)
    m2 = data["missions"][1]
    for key in ("drone_id", "band", "link_eirp_w", "cruise_speed_mps", "battery_max_s"):
        del m2[key]
    m2["pilot_callsign"] = "PILOT-1"
    mission = Scenario.model_validate(data).missions[1]
    assert (mission.pilot_callsign, mission.drone_id) == ("PILOT-1", "UAV-1")


def test_a_mission_needs_a_known_pilot(two_missions: Scenario) -> None:
    data = _data(two_missions)
    data["missions"][1]["pilot_callsign"] = "NOBODY"
    with pytest.raises(ValidationError, match="unknown pilot"):
        Scenario.model_validate(data)


def test_a_mission_flies_its_pilot_drone(two_missions: Scenario) -> None:
    data = _data(two_missions)
    data["missions"][1]["band"] = "5.8GHz"
    with pytest.raises(ValidationError, match="drone fields differ"):
        Scenario.model_validate(data)


def test_one_operator_may_run_several_jammers(two_missions: Scenario) -> None:
    data = _data(two_missions)
    data["friendly_jammers"][1]["operator_callsign"] = "EW-J3"
    assert Scenario.model_validate(data).role_of("EW-J3") == "operator"


def test_a_scenario_may_start_without_mission(scenario: Scenario) -> None:
    data = _data(scenario)
    data["missions"] = []
    assert Scenario.model_validate(data).role_of("PILOT-1") == "pilot"


def test_a_scenario_needs_a_pilot(scenario: Scenario) -> None:
    data = _data(scenario)
    data["missions"] = []
    data["pilots"] = []
    with pytest.raises(ValidationError):
        Scenario.model_validate(data)


@pytest.mark.parametrize("name", ["demo", "directional"])
def test_shipped_scenarios_give_each_mission_its_own_jammer(name: str) -> None:
    shipped = load_scenario(Path(__file__).resolve().parent.parent / "scenarios" / f"{name}.json")
    constraints = constraints_by_mission(shipped, 100)
    assert {m: c.jammers for m, c in constraints.items()} == {"M1": ["J3"], "M2": ["J4"]}


def test_scripted_events_load_and_name_known_things(scenario: Scenario) -> None:
    data = _data(scenario)
    data["events"] = [
        {"type": "jammer.restart", "at_s": 1000, "jammer": "J3"},
        {
            "type": "threat.add",
            "at_s": 1100,
            "kind": "radar",
            "pos": {"lat": 48.1, "lon": 11.5},
            "radius_m": 500,
        },
        {"type": "inject.delay", "at_s": 1200, "mission": "M1", "delay_s": 60},
        {"type": "threat.remove", "at_s": 1300, "threat": "R2"},
    ]
    assert [e.type for e in Scenario.model_validate(data).events] == [
        "jammer.restart",
        "threat.add",
        "inject.delay",
        "threat.remove",
    ]
    for bad, match in (
        ({"type": "jammer.restart", "at_s": 10, "jammer": "J9"}, "unknown friendly jammer J9"),
        ({"type": "inject.delay", "at_s": 10, "mission": "M9", "delay_s": 5}, "unknown mission"),
        (
            {"type": "threat.add", "at_s": 10, "kind": "radar", "pos": {"lat": 48, "lon": 11}},
            "takes radius_m",
        ),
    ):
        with pytest.raises(ValidationError, match=match):
            Scenario.model_validate({**data, "events": [bad]})


def test_the_scripted_hong_kong_demo_loads() -> None:
    from corridor.domain.scenario import SCENARIOS_DIR

    scripted = load_scenario(SCENARIOS_DIR / "hongkong_scripted.json")
    assert [e.type for e in scripted.events] == ["jammer.restart"]
