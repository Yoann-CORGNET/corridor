"""CoT (Cursor on Target) XML encoding and stream framing.

Shapes follow the layout ATAK uses for drawn shapes (type ``u-d-f``: one ``<link point>``
per vertex, ``strokeColor`` / ``fillColor`` as signed ARGB integers). This is written from
known ATAK output, not from a schema: capture a shape drawn on the target ATAK version with
``corridor-tak listen --capture`` and align these builders on it before relying on them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from xml.etree.ElementTree import Element, ParseError, SubElement, fromstring, tostring

LatLon = tuple[float, float]

SA_STALE_S = 120.0
SHAPE_STALE_S = 24 * 3600.0
_UNKNOWN = "9999999.0"
_END = b"</event>"


def cot_time(t: float) -> str:
    dt = datetime.fromtimestamp(t, UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def argb(a: int, r: int, g: int, b: int) -> int:
    """ATAK colour: 32-bit ARGB packed into a signed Java int."""
    value = (a << 24) | (r << 16) | (g << 8) | b
    return value - (1 << 32) if value >= 1 << 31 else value


def _event(uid: str, cot_type: str, how: str, t: float, stale_s: float) -> Element:
    return Element(
        "event",
        version="2.0",
        uid=uid,
        type=cot_type,
        how=how,
        time=cot_time(t),
        start=cot_time(t),
        stale=cot_time(t + stale_s),
    )


def _point(ev: Element, lat: float, lon: float, ce: str = _UNKNOWN) -> None:
    SubElement(ev, "point", lat=f"{lat:.7f}", lon=f"{lon:.7f}", hae=_UNKNOWN, ce=ce, le=_UNKNOWN)


def encode(ev: Element) -> bytes:
    data: bytes = tostring(ev, encoding="utf-8", xml_declaration=False)
    return data


def self_sa(uid: str, callsign: str, lat: float, lon: float, t: float) -> bytes:
    """Self situational-awareness report.

    OpenTAKServer only registers a TCP client (and starts routing other clients' CoT to it)
    once it has sent an event carrying a ``<contact>`` element, so every connection opens
    with this event and repeats it periodically.
    """
    ev = _event(uid, "a-f-G-U-C", "h-e", t, SA_STALE_S)
    _point(ev, lat, lon, ce="10.0")
    detail = SubElement(ev, "detail")
    SubElement(detail, "contact", callsign=callsign, endpoint="*:-1:stcp")
    SubElement(detail, "__group", name="Cyan", role="HQ")
    SubElement(detail, "takv", platform="Corridor", version="0.1.0", device="server", os="linux")
    return encode(ev)


def marker(
    uid: str,
    callsign: str,
    cot_type: str,
    lat: float,
    lon: float,
    t: float,
    remarks: str = "",
    stale_s: float = SHAPE_STALE_S,
) -> bytes:
    ev = _event(uid, cot_type, "h-g-i-g-o", t, stale_s)
    _point(ev, lat, lon)
    detail = SubElement(ev, "detail")
    SubElement(detail, "contact", callsign=callsign)
    if remarks:
        SubElement(detail, "remarks").text = remarks
    return encode(ev)


def shape(
    uid: str,
    name: str,
    vertices: Sequence[LatLon],
    t: float,
    *,
    closed: bool,
    stroke: int,
    fill: int | None = None,
    stroke_weight: float = 3.0,
    remarks: str = "",
    stale_s: float = SHAPE_STALE_S,
) -> bytes:
    """Polygon (``closed=True``) or polyline drawn as an ATAK freehand shape."""
    if len(vertices) < 2:
        raise ValueError("a shape needs at least two vertices")
    lat = sum(v[0] for v in vertices) / len(vertices)
    lon = sum(v[1] for v in vertices) / len(vertices)
    ev = _event(uid, "u-d-f", "h-e", t, stale_s)
    _point(ev, lat, lon)
    detail = SubElement(ev, "detail")
    points = [*vertices, vertices[0]] if closed and vertices[0] != vertices[-1] else vertices
    for v_lat, v_lon in points:
        SubElement(detail, "link", point=f"{v_lat:.7f},{v_lon:.7f}")
    SubElement(detail, "strokeColor", value=str(stroke))
    SubElement(detail, "strokeWeight", value=f"{stroke_weight:.1f}")
    if fill is not None:
        SubElement(detail, "fillColor", value=str(fill))
    SubElement(detail, "contact", callsign=name)
    SubElement(detail, "labels_on", value="true")
    if remarks:
        SubElement(detail, "remarks").text = remarks
    return encode(ev)


ALL_CHAT = "All Chat Rooms"


def chat(
    sender_uid: str,
    sender_callsign: str,
    text: str,
    t: float,
    message_id: str,
    *,
    to_uid: str | None = None,
    to_callsign: str | None = None,
    lat: float = 0.0,
    lon: float = 0.0,
) -> bytes:
    """GeoChat message (``b-t-f``): direct to ``to_uid`` / ``to_callsign``, or to All Chat
    Rooms without them.

    Written from known ATAK GeoChat output, not from a schema, like the shapes: check against a
    capture (``corridor-tak listen --capture``) of a chat sent from the target ATAK version.
    """
    direct = to_uid is not None and to_callsign is not None
    room_id = to_uid if direct else ALL_CHAT
    room = to_callsign if direct else ALL_CHAT
    ev = _event(f"GeoChat.{sender_uid}.{room_id}.{message_id}", "b-t-f", "h-g-i-g-o", t, SA_STALE_S)
    _point(ev, lat, lon)
    detail = SubElement(ev, "detail")
    chat_el = SubElement(
        detail,
        "__chat",
        attrib={
            "parent": "RootContactGroup",
            "groupOwner": "false",
            "messageId": message_id,
            "chatroom": str(room),
            "id": str(room_id),
            "senderCallsign": sender_callsign,
        },
    )
    SubElement(chat_el, "chatgrp", uid0=sender_uid, uid1=str(room_id), id=str(room_id))
    SubElement(detail, "link", uid=sender_uid, type="a-f-G-U-C", relation="p-p")
    remarks = SubElement(
        detail, "remarks", source=f"BAO.F.ATAK.{sender_uid}", to=str(room_id), time=cot_time(t)
    )
    remarks.text = text
    if direct:
        marti = SubElement(detail, "marti")
        SubElement(marti, "dest", callsign=str(to_callsign))
    return encode(ev)


def delete(uid: str, t: float) -> bytes:
    """Remove a shape or marker this client published (``t-x-d-d``)."""
    ev = _event(f"{uid}.delete", "t-x-d-d", "h-g-i-g-o", t, 20.0)
    _point(ev, 0.0, 0.0)
    detail = SubElement(ev, "detail")
    SubElement(detail, "link", uid=uid, relation="none", type="none")
    SubElement(detail, "__forcedelete")
    return encode(ev)


@dataclass
class Framer:
    """Splits a TCP byte stream into CoT ``<event>`` elements."""

    buffer: bytes = b""

    def feed(self, data: bytes) -> list[Element]:
        self.buffer += data
        events: list[Element] = []
        while (end := self.buffer.find(_END)) != -1:
            chunk, self.buffer = self.buffer[: end + len(_END)], self.buffer[end + len(_END) :]
            start = chunk.find(b"<event")
            if start == -1:
                continue
            try:
                events.append(fromstring(chunk[start:]))
            except ParseError:
                continue
        return events


def summary(ev: Element) -> str:
    detail = ev.find("detail")
    contact = detail.find("contact") if detail is not None else None
    callsign = contact.get("callsign", "") if contact is not None else ""
    links = len(detail.findall("link")) if detail is not None else 0
    pt = ev.find("point")
    where = f"{pt.get('lat')},{pt.get('lon')}" if pt is not None else "?"
    extra = f" links={links}" if links else ""
    return f"{ev.get('type')} uid={ev.get('uid')} callsign={callsign!r} at={where}{extra}"
