"""Scenario: the static input of a run, loaded from a JSON file.

Times are simulated seconds from ``start_time``. Jammers are described by their power: the
zone they deny is computed from the J/S ratio against the pilot link (``corridor.domain.zones``).
A friendly jammer may have a directional ``antenna``; without one it radiates the same power in
every direction. A jammer only matters to the mission if its ``bands`` contain the mission
``band``. Radars are discs (``radius_m``) and block every band. Enemy positions are estimates:
``position_error_m`` widens their zones (``corridor.domain.zones``). Friendly jammers are on
except during cuts. ``cuts`` in the file are cuts already scheduled at start; they still need
the command's confirmation, like any other cut.

Several missions share the same jammers, threats and cuts. Each mission has its own pilot
position, so its own zones (``corridor.domain.zones.mission_constraints``). ``reference_link``
gives every jammer a mission-independent footprint for the command view (``nominal_zones``).

``pilots`` lists the pilots and their drone. A pilot may fly several missions; most are
requested at runtime (``mission.request``), the file only holds those known at start. A mission
in the file names its pilot and takes the drone fields from it (``Pilot.mission``).

Every callsign has one role: the command post, a pilot or the operator of friendly jammers
(``Scenario.role_of``).
"""

from __future__ import annotations

import itertools
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from corridor.domain.geo import Position
from corridor.domain.model import Model


class PlannedCut(Model):
    start_s: float = Field(ge=0)
    end_s: float

    @model_validator(mode="after")
    def _end_after_start(self) -> Self:
        if self.end_s <= self.start_s:
            raise ValueError("end_s must be after start_s")
        return self


class Antenna(Model):
    """Directional antenna, seen from above: a main beam with a hard edge, sidelobes elsewhere.

    The cone is horizontal only: the planner is 2D plus time.
    """

    azimuth_deg: float = Field(
        ge=0, lt=360, description="Axis of the main beam, degrees clockwise from north."
    )
    beamwidth_deg: float = Field(gt=0, le=180, description="Full width of the main beam.")
    sidelobe_db: float = Field(
        le=0, description="Gain outside the main beam, relative to the main beam (e.g. -20)."
    )


class FriendlyJammer(Model):
    id: str
    operator_callsign: str
    pos: Position
    eirp_w: float = Field(
        gt=0,
        description="Effective isotropic radiated power, watts; in the main beam if `antenna`.",
    )
    bands: list[str] = Field(min_length=1)
    antenna: Antenna | None = Field(default=None, description="None: omnidirectional.")
    cuts: list[PlannedCut] = Field(default_factory=list)

    @model_validator(mode="after")
    def _cuts_do_not_overlap(self) -> Self:
        ordered = sorted(self.cuts, key=lambda c: c.start_s)
        for before, after in itertools.pairwise(ordered):
            if after.start_s < before.end_s:
                raise ValueError(f"{self.id}: overlapping cuts")
        return self


class EnemyRadar(Model):
    type: Literal["radar"] = "radar"
    id: str
    pos: Position
    radius_m: float = Field(gt=0, description="Estimated detection range.")
    position_error_m: float = Field(
        default=0,
        ge=0,
        description="Uncertainty on `pos`, metres: the zone covers every position this close.",
    )


class EnemyJammer(Model):
    type: Literal["jammer"] = "jammer"
    id: str
    pos: Position
    eirp_w: float = Field(gt=0, description="Estimated EIRP, watts.")
    bands: list[str] = Field(min_length=1)
    position_error_m: float = Field(
        default=0,
        ge=0,
        description="Uncertainty on `pos`, metres: the zone covers every position this close.",
    )


class AvoidZone(Model):
    """Area the command advises against (reported activity, no located emitter).

    Shown with the threats and in each mission's zones, but not forbidden: the planner may cross
    it. Making routes avoid it when there is a reasonable alternative is still to do.
    """

    type: Literal["avoid"] = "avoid"
    id: str
    pos: Position
    radius_m: float = Field(gt=0)


EnemyThreat = Annotated[EnemyRadar | EnemyJammer | AvoidZone, Field(discriminator="type")]
THREAT_PREFIX = {"radar": "R", "jammer": "E", "avoid": "A"}
"""Id prefix of a threat the command adds, by type."""


class Drone(Model):
    id: str
    band: str
    link_eirp_w: float = Field(gt=0, description="EIRP of the pilot's control link, watts.")
    link_min_dbm: float = Field(
        default=-100.0,
        description=(
            "Weakest control link signal the drone still uses, dBm (receiver sensitivity plus a"
            " margin). In 3D, cells where the pilot's signal is weaker are forbidden."
        ),
    )
    cruise_speed_mps: float = Field(gt=0)
    battery_max_s: float = Field(gt=0, description="Flight time on a full battery.")


class Pilot(Model):
    callsign: str
    drone: Drone

    def mission(
        self, mission_id: str, launch: Position, target: Position, departure_s: float
    ) -> Mission:
        return Mission(
            id=mission_id,
            pilot_callsign=self.callsign,
            launch=launch,
            target=target,
            departure_s=departure_s,
            **_drone_fields(self.drone),
        )


class Mission(Model):
    """Drone fields are copied from the pilot's drone, so a mission stands on its own."""

    id: str
    drone_id: str
    pilot_callsign: str = Field(description="The pilot stands at the launch point.")
    launch: Position
    target: Position
    band: str
    link_eirp_w: float = Field(gt=0, description="EIRP of the pilot's control link, watts.")
    link_min_dbm: float = Field(
        default=-100.0,
        description=(
            "Weakest control link signal the drone still uses, dBm (receiver sensitivity plus a"
            " margin). In 3D, cells where the pilot's signal is weaker are forbidden."
        ),
    )
    cruise_speed_mps: float = Field(gt=0)
    battery_max_s: float = Field(gt=0, description="Flight time on a full battery.")
    departure_s: float = Field(ge=0, description="Earliest departure.")
    cross_radars: bool = Field(
        default=False,
        description="The pilot accepts being seen: routes may cross radar zones (never jammers).",
    )


class Terrain(Model):
    """Ground under the area and flight levels above it (``corridor.domain.terrain``)."""

    dem: str | None = Field(
        default=None,
        description=(
            "Elevation file (GeoTIFF, metres above sea level), a path inside backend/data/."
            " None: flat ground."
        ),
    )
    levels_m: tuple[float, ...] = Field(
        default=(30.0, 60.0, 120.0, 250.0),
        min_length=1,
        description="Flight levels, metres above the ground, lowest first.",
    )
    buildings: str | None = Field(
        default=None,
        description=(
            "Building footprints with height_m (GeoJSON from tools/fetch_buildings.py), a path"
            " inside backend/data/; raised into the elevation. Needs dem."
        ),
    )
    mode: Literal["2d", "3d"] = Field(
        default="2d",
        description=(
            "2d: one layer, free-space zones (discs). 3d: the flight levels, zones through the"
            " terrain, climbs and descents."
        ),
    )

    @model_validator(mode="after")
    def _buildings_need_a_map(self) -> Self:
        if self.buildings is not None and self.dem is None:
            raise ValueError("buildings need an elevation file (dem)")
        return self

    @model_validator(mode="after")
    def _levels_rise(self) -> Self:
        if self.levels_m[0] <= 0 or any(b <= a for a, b in itertools.pairwise(self.levels_m)):
            raise ValueError("levels_m must be positive and strictly increasing")
        return self


class ReferenceLink(Model):
    """Control link assumed for the nominal footprint of a jammer, seen without any mission."""

    eirp_w: float = Field(gt=0, description="Link EIRP, watts.")
    range_m: float = Field(gt=0, description="Distance from the drone to its pilot.")


Role = Literal["command", "pilot", "operator"]

ThreatKind = Literal["radar", "jammer", "avoid"]


def check_threat_fields(
    kind: ThreatKind,
    radius_m: float | None,
    eirp_w: float | None,
    bands: list[str] | None,
    position_error_m: float,
) -> None:
    """A radar and a zone to avoid take ``radius_m``, an enemy jammer ``eirp_w`` and ``bands``.

    Raises ValueError otherwise."""
    power = eirp_w is not None, bands is not None
    if kind == "jammer":
        if not all(power) or radius_m is not None:
            raise ValueError("a jammer takes eirp_w and bands, not radius_m")
    elif any(power) or radius_m is None:
        raise ValueError("a radar or a zone to avoid takes radius_m, not eirp_w or bands")
    if kind == "avoid" and position_error_m:
        raise ValueError("a zone to avoid has no position error: widen its radius")


class ScriptedRestart(Model):
    """At ``at_s`` the friendly jammer comes back on, before the end of its cut."""

    type: Literal["jammer.restart"] = "jammer.restart"
    at_s: float = Field(gt=0, description="Simulated time, seconds.")
    jammer: str


class ScriptedThreat(Model):
    """At ``at_s`` the command reports an enemy threat or a zone to avoid (as ``threat.add``).
    The engine gives it the next free id: R2 after R1, E2 after E1, A1 for the first zone."""

    type: Literal["threat.add"] = "threat.add"
    at_s: float = Field(gt=0, description="Simulated time, seconds.")
    kind: ThreatKind
    pos: Position
    radius_m: float | None = Field(default=None, gt=0)
    eirp_w: float | None = Field(default=None, gt=0)
    bands: list[str] | None = Field(default=None, min_length=1)
    position_error_m: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _fields_match_kind(self) -> Self:
        check_threat_fields(
            self.kind, self.radius_m, self.eirp_w, self.bands, self.position_error_m
        )
        return self


class ScriptedRemoval(Model):
    """At ``at_s`` the command removes a threat (as ``threat.remove``)."""

    type: Literal["threat.remove"] = "threat.remove"
    at_s: float = Field(gt=0, description="Simulated time, seconds.")
    threat: str


class ScriptedHold(Model):
    """At ``at_s`` the flying drone of ``mission`` is held where it is (as ``inject.delay``)."""

    type: Literal["inject.delay"] = "inject.delay"
    at_s: float = Field(gt=0, description="Simulated time, seconds.")
    mission: str
    delay_s: float = Field(gt=0, le=1800)


ScriptedEvent = Annotated[
    ScriptedRestart | ScriptedThreat | ScriptedRemoval | ScriptedHold,
    Field(discriminator="type"),
]


class Scenario(Model):
    start_time: AwareDatetime = Field(description="Wall-clock time of simulated t = 0.")
    js_threshold_db: float = Field(
        description="The control link is lost where J/S at the drone reaches this value."
    )
    reference_link: ReferenceLink
    command_callsign: str = Field(description="Callsign of the command post, which sees all.")
    friendly_jammers: list[FriendlyJammer]
    enemy_threats: list[EnemyThreat] = Field(default_factory=list)
    terrain: Terrain = Field(default_factory=Terrain)
    pilots: list[Pilot] = Field(min_length=1)
    missions: list[Mission] = Field(
        default_factory=list,
        description="Missions known at start. In the file, drone fields may be left out.",
    )
    events: list[ScriptedEvent] = Field(
        default_factory=list,
        description=(
            "Scripted timeline for a demo: each fires once, when the simulated clock passes"
            " at_s, as if the command, the jammer operator or the demo panel had sent it."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _drone_fields_from_pilot(cls, data: Any) -> Any:
        """Fill the drone fields of each file mission from its pilot."""
        if not isinstance(data, dict):
            return data
        pilots = {p.get("callsign"): p for p in data.get("pilots", []) if isinstance(p, dict)}
        missions = []
        for mission in data.get("missions", []):
            pilot = pilots.get(mission.get("pilot_callsign")) if isinstance(mission, dict) else None
            drone = pilot.get("drone") if pilot else None
            if isinstance(drone, dict):
                mission = {**_drone_fields(Drone.model_validate(drone)), **mission}
            missions.append(mission)
        return {**data, "missions": missions}

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        _no_duplicates(
            "ids",
            [
                *(j.id for j in self.friendly_jammers),
                *(t.id for t in self.enemy_threats),
                *(m.id for m in self.missions),
            ],
        )
        _no_duplicates("drones", [p.drone.id for p in self.pilots])
        _no_duplicates(
            "callsigns",
            [
                self.command_callsign,
                *(p.callsign for p in self.pilots),
                # An operator may run several jammers: one callsign each, not per jammer.
                *{j.operator_callsign for j in self.friendly_jammers},
            ],
        )
        return self

    @model_validator(mode="after")
    def _missions_fly_their_pilot_drone(self) -> Self:
        for mission in self.missions:
            pilot = self.pilot(mission.pilot_callsign)
            if pilot is None:
                raise ValueError(f"{mission.id}: unknown pilot {mission.pilot_callsign}")
            drone = _drone_fields(pilot.drone)
            if any(getattr(mission, k) != v for k, v in drone.items()):
                raise ValueError(f"{mission.id}: drone fields differ from {pilot.callsign}'s drone")
        return self

    @model_validator(mode="after")
    def _scripted_events_name_known_things(self) -> Self:
        jammers = {j.id for j in self.friendly_jammers}
        missions = {m.id for m in self.missions}
        for event in self.events:
            if isinstance(event, ScriptedRestart) and event.jammer not in jammers:
                raise ValueError(f"event at {event.at_s} s: unknown friendly jammer {event.jammer}")
            if isinstance(event, ScriptedHold) and event.mission not in missions:
                raise ValueError(f"event at {event.at_s} s: unknown mission {event.mission}")
        return self

    def friendly_jammer(self, jammer_id: str) -> FriendlyJammer | None:
        return next((j for j in self.friendly_jammers if j.id == jammer_id), None)

    def mission(self, mission_id: str) -> Mission | None:
        return next((m for m in self.missions if m.id == mission_id), None)

    def pilot(self, callsign: str) -> Pilot | None:
        return next((p for p in self.pilots if p.callsign == callsign), None)

    def role_of(self, callsign: str) -> Role | None:
        """Role of ``callsign``; None if the scenario does not know it."""
        if callsign == self.command_callsign:
            return "command"
        if self.pilot(callsign) is not None:
            return "pilot"
        if any(j.operator_callsign == callsign for j in self.friendly_jammers):
            return "operator"
        return None

    def clock_label(self, t: float) -> str:
        """Simulated time as wall-clock ``HH:MM:SS``, for human-facing text."""
        return (self.start_time + timedelta(seconds=t)).strftime("%H:%M:%S")


def _drone_fields(drone: Drone) -> dict[str, Any]:
    """The fields of ``Mission`` that come from the drone."""
    return {
        "drone_id": drone.id,
        "band": drone.band,
        "link_eirp_w": drone.link_eirp_w,
        "link_min_dbm": drone.link_min_dbm,
        "cruise_speed_mps": drone.cruise_speed_mps,
        "battery_max_s": drone.battery_max_s,
    }


def _no_duplicates(what: str, values: list[str]) -> None:
    duplicates = sorted(v for v, n in Counter(values).items() if n > 1)
    if duplicates:
        raise ValueError(f"duplicate {what}: {duplicates}")


SCENARIOS_DIR = Path(__file__).resolve().parents[3] / "scenarios"
"""``backend/scenarios``: the only folder scenario files are read from."""


def load_scenario(path: Path) -> Scenario:
    """Raises ValueError if ``path`` is outside ``SCENARIOS_DIR``, if the file is invalid or if
    its elevation file is missing: the server reads no file the team did not put there."""
    from corridor.domain.terrain import check_terrain  # terrain imports this module

    root = SCENARIOS_DIR.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"scenario files must be inside {root}: {path}")
    scenario = Scenario.model_validate_json(resolved.read_text(encoding="utf-8"))
    check_terrain(scenario.terrain)
    return scenario
