"""ATAK destination: the route as a native ATAK route, sent to the pilot only.

The route is a ``b-m-r`` event with one ``b-m-p-w`` waypoint per ``<link>``; each waypoint
name carries its altitude and time constraint, so the pilot reads them on the map. The route
remarks and a GeoChat direct message carry the full briefing.

Need to know: only the route and its times are sent, never a zone or a jammer, and every
event carries ``<marti><dest callsign=...>``. OpenTAKServer then routes it to that callsign's
queue only, instead of broadcasting it to the group (read in the OTS source; check it on the
running server with ``corridor-tak route-smoke``).

Like the shapes in ``corridor.tak.cot``, the route layout is written from known ATAK output,
not from a schema: capture a route drawn on the target ATAK with ``corridor-tak listen
--capture`` and align on it.

Re-sending with the same uid replaces the route on the phone; ``delete_cot`` removes it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from xml.etree.ElementTree import Element, SubElement

from corridor.export.route import FlightRoute, Waypoint, briefing, takeoff_window
from corridor.tak.client import Identity, TakClient
from corridor.tak.cot import SHAPE_STALE_S, _event, _point, argb, cot_time, encode

ROUTE_BLUE = argb(255, 30, 110, 230)
UID_WAIT_S = 5.0
"""How long to listen for the pilot's position report, to learn their uid for the chat."""


@dataclass(frozen=True)
class TakTarget:
    host: str
    port: int = 8088
    callsign: str = "CORRIDOR"


@dataclass(frozen=True)
class Delivery:
    route_uid: str
    pilot_callsign: str
    pilot_uid: str | None
    """None when the pilot sent no position report in time: the route went, the chat did not."""
    chat_sent: bool


def route_uid(route: FlightRoute) -> str:
    return f"corridor-route-{route.mission_id}"


def _dest(detail: Element, callsign: str) -> None:
    SubElement(SubElement(detail, "marti"), "dest", callsign=callsign)


def start_name(route: FlightRoute) -> str:
    if route.airborne:
        return f"START {route.label(route.takeoff_s)}"
    window = takeoff_window(route).removeprefix("not before ")
    return f"T/O {window}"


def waypoint_name(route: FlightRoute, wp: Waypoint) -> str:
    alt = f"{wp.alt_m:.0f}m"
    if wp.kind == "hold":
        return f"WP{wp.number} {alt} HOLD til {route.label(wp.depart_s, up=True)}"
    if wp.kind == "target":
        return f"TGT {alt} {route.label(wp.arrive_s)}"
    return f"WP{wp.number} {alt} {route.label(wp.arrive_s)}"


def route_cot(route: FlightRoute, t: float, to: str | None = None) -> bytes:
    """ATAK route; ``to`` is the recipient callsign (default: the mission pilot)."""
    uid = route_uid(route)
    ev = _event(uid, "b-m-r", "h-e", t, SHAPE_STALE_S)
    _point(ev, route.start.lat, route.start.lon)
    detail = SubElement(ev, "detail")

    def link(suffix: str, name: str, lat: float, lon: float) -> None:
        SubElement(
            detail,
            "link",
            uid=f"{uid}-{suffix}",
            callsign=name,
            type="b-m-p-w",
            point=f"{lat:.7f},{lon:.7f}",
            remarks="",
            relation="c",
        )

    link("sp", start_name(route), route.start.lat, route.start.lon)
    for wp in route.waypoints:
        link(f"wp{wp.number}", waypoint_name(route, wp), wp.lat, wp.lon)
    SubElement(
        detail,
        "link_attr",
        planningmethod="Infil",
        color=str(ROUTE_BLUE),
        method="Flying",
        prefix="CP",
        type="Vehicle",
        stroke="3",
        direction="Infil",
        routetype="Primary",
        order="Ascending Check Points",
    )
    SubElement(detail, "strokeColor", value=str(ROUTE_BLUE))
    SubElement(detail, "strokeWeight", value="3.0")
    SubElement(SubElement(detail, "__routeinfo"), "__navcues")
    SubElement(detail, "contact", callsign=f"{route.title} {start_name(route)}")
    SubElement(detail, "remarks").text = "\n".join(briefing(route))
    SubElement(detail, "labels_on", value="true")
    SubElement(detail, "color", value=str(ROUTE_BLUE))
    _dest(detail, to or route.pilot_callsign)
    return encode(ev)


def chat_cot(sender: Identity, to_callsign: str, to_uid: str, text: str, t: float) -> bytes:
    """GeoChat direct message, as ATAK sends one between two contacts."""
    message_id = uuid.uuid4().hex
    ev = _event(f"GeoChat.{sender.uid}.{to_uid}.{message_id}", "b-t-f", "h-g-i-g-o", t, 86400)
    _point(ev, sender.lat, sender.lon)
    detail = SubElement(ev, "detail")
    chat = SubElement(
        detail,
        "__chat",
        {
            "parent": "RootContactGroup",
            "groupOwner": "false",
            "messageId": message_id,
            "chatroom": to_callsign,
            "id": to_uid,
            "senderCallsign": sender.callsign,
        },
    )
    SubElement(chat, "chatgrp", uid0=sender.uid, uid1=to_uid, id=to_uid)
    SubElement(detail, "link", uid=sender.uid, type="a-f-G-U-C", relation="p-p")
    remarks = SubElement(
        detail, "remarks", source=f"BAO.F.ATAK.{sender.uid}", to=to_uid, time=cot_time(t)
    )
    remarks.text = text
    _dest(detail, to_callsign)
    return encode(ev)


def delete_cot(target_uid: str, t: float, to: str) -> bytes:
    """Remove ``target_uid`` (a route) from the recipient's map."""
    ev = _event(f"corridor-delete-{uuid.uuid4().hex[:8]}", "t-x-d-d", "h-g-i-g-o", t, 60)
    _point(ev, 0.0, 0.0)
    detail = SubElement(ev, "detail")
    SubElement(detail, "link", uid=target_uid, relation="none", type="none")
    SubElement(detail, "__forcedelete")
    _dest(detail, to)
    return encode(ev)


def sender_identity(target: TakTarget, route: FlightRoute) -> Identity:
    """Stable uid, so ATAK keeps one chat thread with Corridor across sends."""
    uid = f"corridor-{target.callsign.lower()}"
    return Identity(uid, target.callsign, route.launch.lat, route.launch.lon)


async def find_uid(client: TakClient, callsign: str, wait_s: float) -> str | None:
    """Uid of ``callsign``, from the next position report OTS routes to us."""

    async def scan() -> str | None:
        async for ev in client.events():
            contact = ev.find("detail/contact")
            if (
                contact is not None
                and contact.get("callsign") == callsign
                and ev.get("type", "").startswith("a-")
            ):
                return ev.get("uid")
        return None

    try:
        return await asyncio.wait_for(scan(), wait_s)
    except TimeoutError:
        return None


async def deliver(
    target: TakTarget,
    route: FlightRoute,
    *,
    pilot_uid: str | None = None,
    uid_wait_s: float = UID_WAIT_S,
) -> Delivery:
    """Send the route, then the briefing as a direct message, to the pilot only.

    Raises OSError when OpenTAKServer cannot be reached.
    """
    client = TakClient(target.host, target.port, sender_identity(target, route))
    await client.connect()
    try:
        pilot = route.pilot_callsign
        uid = pilot_uid or await find_uid(client, pilot, uid_wait_s)
        now = time.time()
        await client.send(route_cot(route, now))
        if uid:
            await client.send(chat_cot(client.me, pilot, uid, "\n".join(briefing(route)), now))
        await asyncio.sleep(0.3)  # let OTS read everything before the socket closes
    finally:
        await client.close()
    return Delivery(route_uid(route), pilot, uid, chat_sent=uid is not None)


async def revoke(target: TakTarget, route: FlightRoute) -> None:
    client = TakClient(target.host, target.port, sender_identity(target, route))
    await client.connect()
    try:
        await client.send(delete_cot(route_uid(route), time.time(), route.pilot_callsign))
        await asyncio.sleep(0.3)
    finally:
        await client.close()
