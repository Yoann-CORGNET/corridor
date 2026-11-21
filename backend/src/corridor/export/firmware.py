"""Mission files for flight controllers: INAV (MW XML) and ArduPilot (QGC WPL 110).

INAV has no absolute time: the route's timing holds only if the pilot takes off at the
take-off time, and a hold becomes a fixed-length loiter (``POSHOLD_TIME``). A late drone
stays late.

ArduPilot's ``NAV_DELAY`` waits until a UTC time of day: the take-off and every hold
departure are "not before" times, so a late drone catches up instead of waiting the full
hold. It needs GPS time; without it the delay ends at once. Users report the wait running
up to about a minute past the set time: test on the target vehicle.

Both rely on GNSS for navigation. If friendly jamming covers GNSS bands, they do not apply.
"""

from __future__ import annotations

import math
from datetime import UTC
from typing import Literal
from xml.etree.ElementTree import Comment, Element, SubElement, indent, tostring

from corridor.export.route import FlightRoute, briefing

INAV_MAX_WAYPOINTS = 120
INAV_LAST_FLAG = "165"
"""0xA5: required on the last mission item."""
INAV_MIN_SPEED_CMS = 50
"""INAV ignores a leg speed at or below this (and above ``nav_auto_speed``)."""

EndAction = Literal["hold", "rth"]
"""After the target: ``hold`` loiters there (INAV treats a final WAYPOINT as POSHOLD_UNLIM,
ArduPilot holds after the last item); ``rth`` returns to launch."""

# MAVLink
FRAME_GLOBAL = 0
FRAME_GLOBAL_RELATIVE_ALT = 3
NAV_WAYPOINT = 16
NAV_RETURN_TO_LAUNCH = 20
NAV_TAKEOFF = 22
NAV_DELAY = 93
DO_CHANGE_SPEED = 178


def _comment_text(route: FlightRoute) -> str:
    # "--" is not allowed inside an XML comment.
    return " " + " | ".join(briefing(route)).replace("--", "-") + " "


def inav_mission(route: FlightRoute, end: EndAction = "hold") -> str:
    """MW XML mission, as read by INAV Configurator, mwp and ezgui. Altitudes relative to home.

    WAYPOINT ``parameter1`` is the leg speed (cm/s, multicopters only); POSHOLD_TIME
    ``parameter1`` is the loiter time (s) and ``parameter2`` the leg speed.
    """
    speed = round(route.cruise_speed_mps * 100)
    speed = speed if speed > INAV_MIN_SPEED_CMS else 0
    items: list[dict[str, str]] = []
    for wp in route.waypoints:
        common = {"lat": f"{wp.lat:.7f}", "lon": f"{wp.lon:.7f}", "alt": str(round(wp.alt_m))}
        if wp.kind == "hold":
            params = ("POSHOLD_TIME", str(math.ceil(wp.hold_s)), str(speed))
        else:
            params = ("WAYPOINT", str(speed), "0")
        items.append(
            {"action": params[0], **common, "parameter1": params[1], "parameter2": params[2]}
        )
    if end == "rth":
        zero = {"lat": "0", "lon": "0", "alt": "0", "parameter1": "0", "parameter2": "0"}
        items.append({"action": "RTH", **zero})
    if len(items) > INAV_MAX_WAYPOINTS:
        raise ValueError(f"INAV takes at most {INAV_MAX_WAYPOINTS} mission items")

    root = Element("mission")
    root.append(Comment(_comment_text(route)))
    SubElement(root, "version", value="2.3-pre8")
    home = route.launch
    SubElement(
        root,
        "mwp",
        {
            "cx": f"{home.lon:.7f}",
            "cy": f"{home.lat:.7f}",
            "home-x": f"{home.lon:.7f}",
            "home-y": f"{home.lat:.7f}",
            "zoom": "14",
        },
    )
    for number, item in enumerate(items, start=1):
        attrs = {"no": str(number), **item, "parameter3": "0"}
        if number == len(items):
            attrs["flag"] = INAV_LAST_FLAG
        SubElement(root, "missionitem", attrs)
    indent(root)
    body: str = tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + body + "\n"


def ardupilot_waypoints(route: FlightRoute, end: EndAction = "hold") -> str:
    """QGC WPL 110 (Mission Planner, QGroundControl), altitudes relative to home.

    Item 0 is home. Then: leg speed, wait until the take-off time, take-off, and the
    waypoints; a hold is a waypoint followed by a wait until its departure time.
    """
    rows: list[tuple[int, int, float, float, float, float, float, float, float]] = []

    def item(
        command: int,
        p: tuple[float, float, float, float] = (0, 0, 0, 0),
        lat: float = 0.0,
        lon: float = 0.0,
        alt: float = 0.0,
        frame: int = FRAME_GLOBAL_RELATIVE_ALT,
    ) -> None:
        rows.append((frame, command, *p, lat, lon, alt))

    def wait_until(t: float) -> None:
        utc = route.wall(t, up=True).astimezone(UTC)
        item(NAV_DELAY, (-1, utc.hour, utc.minute, utc.second))

    item(NAV_WAYPOINT, lat=route.launch.lat, lon=route.launch.lon, frame=FRAME_GLOBAL)
    item(DO_CHANGE_SPEED, (1, route.cruise_speed_mps, -1, 0))
    if not route.airborne:
        wait_until(route.takeoff_s)
        item(NAV_TAKEOFF, alt=route.waypoints[0].alt_m)
    for wp in route.waypoints:
        item(NAV_WAYPOINT, lat=wp.lat, lon=wp.lon, alt=wp.alt_m)
        if wp.kind == "hold":
            wait_until(wp.depart_s)
    if end == "rth":
        item(NAV_RETURN_TO_LAUNCH)

    lines = ["QGC WPL 110"]
    for index, (frame, command, p1, p2, p3, p4, lat, lon, alt) in enumerate(rows):
        current = 1 if index == 0 else 0
        values = "\t".join(f"{v:.8f}" for v in (p1, p2, p3, p4, lat, lon))
        lines.append(f"{index}\t{current}\t{frame}\t{command}\t{values}\t{alt:.6f}\t1")
    return "\n".join(lines) + "\n"
