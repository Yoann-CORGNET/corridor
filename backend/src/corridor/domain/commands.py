"""Commands: requests to change the world, sent by any adapter.

``actor`` is the callsign of whoever sends the command. The engine checks it against the
domain rules (only the command confirms a cut), whatever the channel.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from corridor.domain.events import ChoiceKind, OnTimeout
from corridor.domain.geo import Position
from corridor.domain.model import Model
from corridor.domain.scenario import ThreatKind, check_threat_fields


class ScheduleCut(Model):
    type: Literal["cut.schedule"] = "cut.schedule"
    actor: str
    jammer: str
    start_s: float = Field(ge=0)
    end_s: float
    mission: str | None = Field(default=None, description="Mission the cut is asked for.")

    @model_validator(mode="after")
    def _end_after_start(self) -> Self:
        if self.end_s <= self.start_s:
            raise ValueError("end_s must be after start_s")
        return self


class CancelCut(Model):
    type: Literal["cut.cancel"] = "cut.cancel"
    actor: str
    cut: str


class RestartJammer(Model):
    """The friendly jammer is back on now, before the end of its cut: the cuts covering now
    end early (they are cancelled). Its operator or the command."""

    type: Literal["jammer.restart"] = "jammer.restart"
    actor: str
    jammer: str


class ConfirmCut(Model):
    """Only the command confirms a cut; the jammer operator then carries it out."""

    type: Literal["cut.confirm"] = "cut.confirm"
    actor: str
    cut: str


class RequestMission(Model):
    """A pilot asks for a route: a new mission with its drone, planned right away."""

    type: Literal["mission.request"] = "mission.request"
    actor: str
    launch: Position = Field(description="Where the pilot stands and the drone takes off.")
    target: Position
    departure_s: float = Field(
        ge=0, description="Earliest departure; one in the past means as soon as possible."
    )


class UpdateMission(Model):
    """Move the launch or target of a mission still on the ground, change its departure, or
    whether its routes may cross radars.

    Same rights as launch: its pilot only.
    """

    type: Literal["mission.update"] = "mission.update"
    actor: str
    mission: str
    launch: Position
    target: Position
    departure_s: float = Field(
        ge=0, description="Earliest departure; one in the past means as soon as possible."
    )
    cross_radars: bool | None = Field(
        default=None, description="Let routes cross radar zones; None keeps the mission's choice."
    )


class ChooseRoute(Model):
    """The pilot picks one of the route choices of its current plan.

    A choice needing cuts asks the command, which answers until a deadline; ``on_timeout``
    says what happens if it has not by then.
    """

    type: Literal["route.choose"] = "route.choose"
    actor: str
    mission: str
    kind: ChoiceKind
    on_timeout: OnTimeout = "avoid"


class DecideRoute(Model):
    """The command accepts or refuses the pending route request of a mission."""

    type: Literal["route.decide"] = "route.decide"
    actor: str
    mission: str
    accept: bool


class LaunchMission(Model):
    """Only the mission pilot may launch: the command only answers cut requests."""

    type: Literal["mission.launch"] = "mission.launch"
    actor: str
    mission: str


class SetClock(Model):
    type: Literal["clock.set"] = "clock.set"
    actor: str
    factor: float | None = Field(default=None, gt=0)
    paused: bool | None = None


class InjectDelay(Model):
    """Demo twist: hold the flying drone where it is for ``delay_s`` simulated seconds."""

    type: Literal["inject.delay"] = "inject.delay"
    actor: str
    mission: str
    delay_s: float = Field(gt=0, le=1800)


class AddThreat(Model):
    """The command reports an enemy threat or a zone to avoid; the server gives it an id.

    A radar and a zone to avoid take ``radius_m``, an enemy jammer ``eirp_w`` and ``bands``.
    """

    type: Literal["threat.add"] = "threat.add"
    actor: str
    kind: ThreatKind
    pos: Position
    radius_m: float | None = Field(
        default=None, gt=0, description="Radar range or radius of the zone to avoid, metres."
    )
    eirp_w: float | None = Field(default=None, gt=0, description="Jammer EIRP, watts.")
    bands: list[str] | None = Field(default=None, min_length=1, description="Jammer bands.")
    position_error_m: float = Field(
        default=0, ge=0, description="Uncertainty on `pos`, metres (radar and jammer)."
    )

    @model_validator(mode="after")
    def _fields_match_kind(self) -> Self:
        check_threat_fields(
            self.kind, self.radius_m, self.eirp_w, self.bands, self.position_error_m
        )
        return self


class RemoveThreat(Model):
    """The command removes an enemy threat or a zone to avoid, from the file or added."""

    type: Literal["threat.remove"] = "threat.remove"
    actor: str
    threat: str


Command = Annotated[
    ScheduleCut
    | CancelCut
    | RestartJammer
    | ConfirmCut
    | RequestMission
    | UpdateMission
    | ChooseRoute
    | DecideRoute
    | LaunchMission
    | SetClock
    | InjectDelay
    | AddThreat
    | RemoveThreat,
    Field(discriminator="type"),
]
