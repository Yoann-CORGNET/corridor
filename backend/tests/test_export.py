"""Route export: waypoints from the plan, INAV and ArduPilot files, ATAK CoT, HTTP endpoints."""

import socket
import socketserver
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree.ElementTree import fromstring

import pytest
from fastapi.testclient import TestClient

from corridor.adapters.ws.app import create_app
from corridor.domain.events import RouteAction, RoutePoint
from corridor.domain.geo import Grid, LocalFrame, Position
from corridor.domain.planner.plan import plan_fallback, plan_mission
from corridor.domain.safety import Flight, schedule_violation
from corridor.domain.scenario import Mission, Scenario
from corridor.domain.state import Cut
from corridor.domain.zones import mission_constraints, mission_shapes
from corridor.engine.replan import PlanDone
from corridor.export.atak import TakTarget, chat_cot, delete_cot, route_cot, sender_identity
from corridor.export.cli import write_files
from corridor.export.firmware import ardupilot_waypoints, inav_mission
from corridor.export.route import (
    LATEST_HORIZON_S,
    LATEST_STEP_S,
    FlightRoute,
    briefing,
    flight_route,
)
from corridor.tak.cot import self_sa


def m1(scenario: Scenario) -> Mission:
    return scenario.missions[0]


def grid_of(scenario: Scenario) -> Grid:
    return mission_constraints(scenario, m1(scenario), 100).grid


def _points(scenario: Scenario) -> list[RoutePoint]:
    """Ground until 300 s, a turn, a hold 500-620 s, the target at 800 s."""
    frame = LocalFrame(m1(scenario).launch)

    def at(x: float, y: float, t: float, action: RouteAction) -> RoutePoint:
        p = frame.to_position(x, y)
        return RoutePoint(lat=p.lat, lon=p.lon, t=t, action=action)

    return [
        at(0, 0, 0, "ground"),
        at(0, 0, 300, "fly"),
        at(0.2, 0.2, 300, "fly"),  # launch on a cell centre: merged
        at(1500, 0, 400, "fly"),
        at(1500, 1500, 500, "hover"),
        at(1500, 1500, 620, "fly"),
        at(3000, 3000, 800, "fly"),
    ]


@pytest.fixture
def route(scenario: Scenario) -> FlightRoute:
    return flight_route(
        scenario, m1(scenario), _points(scenario), 100, status="approved", revision=3
    )


def test_waypoints_drop_ground_wait_and_merge_hover(route: FlightRoute) -> None:
    assert route.takeoff_s == 300
    assert not route.airborne
    assert [w.kind for w in route.waypoints] == ["waypoint", "hold", "target"]
    assert [w.number for w in route.waypoints] == [1, 2, 3]
    hold = route.waypoints[1]
    assert (hold.arrive_s, hold.depart_s, hold.hold_s) == (500, 620, 120)
    assert all(w.alt_m == 100 for w in route.waypoints)


def test_labels_round_not_before_times_up(scenario: Scenario, route: FlightRoute) -> None:
    assert route.label(300) == "14:05:00Z"
    assert route.label(300.2, up=True) == "14:05:01Z"
    lines = briefing(route)
    assert "T/O not before 14:05:00Z" in lines[1]
    assert any("HOLD, leave not before 14:10:20Z" in line for line in lines)
    assert not any("J3" in line or "E1" in line for line in lines), "need to know"


def test_route_through_a_cut_has_a_latest_takeoff(scenario: Scenario) -> None:
    cut = Cut(
        id="c1", jammer="J3", start_s=900, end_s=1380, status="confirmed", requested_by="EW-J3"
    )
    plan = plan_mission(scenario, m1(scenario), grid_of(scenario), [cut])
    assert plan is not None
    assert plan.status == "approved"
    route = flight_route(scenario, m1(scenario), plan.optimised_route, cuts=[cut])
    latest = route.takeoff_latest_s
    assert latest is not None
    assert route.takeoff_s < latest < route.takeoff_s + LATEST_HORIZON_S
    frame, discs = LocalFrame(m1(scenario).launch), mission_shapes(scenario, m1(scenario))
    too_late = latest + 2 * LATEST_STEP_S - route.takeoff_s
    flight = Flight(route=list(plan.optimised_route), revision=0, delay_s=too_late)
    assert schedule_violation(flight, route.takeoff_s + too_late, frame, discs, [cut]) == "J3"
    window = f"{route.label(route.takeoff_s, up=True)}-{route.label(latest)}"
    assert f"T/O {window}" in briefing(route)[1]
    names = [link.get("callsign") for link in fromstring(route_cot(route, 0.0)).iter("link")]
    assert names[0] == f"T/O {window}"
    # Without the cut it relies on, the same route has no latest time.
    assert flight_route(scenario, m1(scenario), plan.optimised_route).takeoff_latest_s is None


def test_route_needs_a_takeoff_and_a_waypoint(scenario: Scenario) -> None:
    mission, takeoff_only = m1(scenario), _points(scenario)[:1]
    with pytest.raises(ValueError):
        flight_route(scenario, mission, takeoff_only)


def test_inav_mission(route: FlightRoute) -> None:
    root = fromstring(inav_mission(route))
    items = root.findall("missionitem")
    assert [i.get("action") for i in items] == ["WAYPOINT", "POSHOLD_TIME", "WAYPOINT"]
    assert [i.get("no") for i in items] == ["1", "2", "3"]
    assert items[1].get("parameter1") == "120"
    speed = str(round(route.cruise_speed_mps * 100))
    assert items[0].get("parameter1") == speed
    assert items[1].get("parameter2") == speed
    assert {i.get("alt") for i in items} == {"100"}
    assert [i.get("flag") for i in items] == [None, None, "165"]
    home = root.find("mwp")
    assert home is not None
    assert float(home.get("home-y", "0")) == pytest.approx(route.launch.lat)

    with_rth = fromstring(inav_mission(route, end="rth")).findall("missionitem")
    assert with_rth[-1].get("action") == "RTH"
    assert [i.get("flag") for i in with_rth][-2:] == [None, "165"]


def _rows(text: str) -> list[list[str]]:
    lines = text.splitlines()
    assert lines[0] == "QGC WPL 110"
    return [line.split("\t") for line in lines[1:]]


def test_ardupilot_waits_until_utc_times(route: FlightRoute) -> None:
    rows = _rows(ardupilot_waypoints(route))
    commands = [int(r[3]) for r in rows]
    # home, speed, wait take-off, take-off, WP1, WP2, wait hold, target
    assert commands == [16, 178, 93, 22, 16, 16, 93, 16]
    assert [int(r[0]) for r in rows] == list(range(8))
    assert rows[0][1] == "1"
    assert all(len(r) == 12 for r in rows)
    takeoff_wait = [float(v) for v in rows[2][4:8]]
    assert takeoff_wait == [-1, 14, 5, 0]
    assert float(rows[3][10]) == 100
    assert [float(v) for v in rows[6][4:8]] == [-1, 14, 10, 20]


def test_ardupilot_converts_local_time_to_utc(scenario: Scenario) -> None:
    paris = timezone(timedelta(hours=1))
    local = scenario.model_copy(update={"start_time": datetime(2026, 11, 21, 15, tzinfo=paris)})
    route = flight_route(local, m1(local), _points(local))
    assert route.label(0) == "15:00:00"
    wait = _rows(ardupilot_waypoints(route))[2]
    assert [float(v) for v in wait[4:8]] == [-1, 14, 5, 0]


def test_fallback_route_is_airborne(scenario: Scenario) -> None:
    grid = grid_of(scenario)
    planned = plan_mission(scenario, m1(scenario), grid, [])
    assert planned is not None
    mid = planned.optimised_route[len(planned.optimised_route) // 2]
    here = Position(lat=mid.lat, lon=mid.lon)
    plan = plan_fallback(scenario, m1(scenario), grid, here, mid.t)
    assert plan is not None
    route = flight_route(scenario, m1(scenario), plan.optimised_route, status="fallback")
    assert route.airborne
    assert route.start.lat == pytest.approx(here.lat, abs=1e-6)
    commands = [int(r[3]) for r in _rows(ardupilot_waypoints(route))]
    assert 22 not in commands
    assert briefing(route)[1].startswith("New route from")


def test_atak_route_goes_to_the_pilot_only(route: FlightRoute) -> None:
    ev = fromstring(route_cot(route, 0.0))
    assert ev.get("type") == "b-m-r"
    assert ev.get("uid") == "corridor-route-M1"
    links = ev.findall("detail/link")
    assert [link.get("type") for link in links] == ["b-m-p-w"] * 4
    names = [link.get("callsign", "") for link in links]
    assert names[0] == "T/O 14:05:00Z"
    assert names[2] == "WP2 100m HOLD til 14:10:20Z"
    assert names[3].startswith("TGT 100m ")
    dests = ev.findall("detail/marti/dest")
    assert [d.get("callsign") for d in dests] == [route.pilot_callsign]
    attr = ev.find("detail/link_attr")
    assert attr is not None
    assert attr.get("method") == "Flying"
    remarks = ev.find("detail/remarks")
    assert remarks is not None
    assert "never early, never past the latest time" in (remarks.text or "")


def test_atak_chat_and_delete(route: FlightRoute) -> None:
    me = sender_identity(TakTarget("h"), route)
    chat = fromstring(chat_cot(me, "PILOT-1", "ANDROID-1", "hello", 0.0))
    assert chat.get("type") == "b-t-f"
    group = chat.find("detail/__chat")
    assert group is not None
    assert (group.get("id"), group.get("chatroom")) == ("ANDROID-1", "PILOT-1")
    assert chat.findtext("detail/remarks") == "hello"
    assert chat.find("detail/marti/dest") is not None

    gone = fromstring(delete_cot("corridor-route-M1", 0.0, "PILOT-1"))
    assert gone.get("type") == "t-x-d-d"
    link = gone.find("detail/link")
    assert link is not None
    assert link.get("uid") == "corridor-route-M1"


def test_files_stay_inside_the_output_folder(tmp_path: Path, route: FlightRoute) -> None:
    written = write_files(route, Path("out"), base=tmp_path)
    assert {p.name for p in written} == {
        "M1.mission",
        "M1.waypoints",
        "M1-atak-route.xml",
        "M1-briefing.txt",
    }
    assert all(p.parent == tmp_path / "out" for p in written)
    with pytest.raises(ValueError, match="outside"):
        write_files(route, Path("../escape"), base=tmp_path)
    sneaky = replace(route, mission_id="../../M1")
    with pytest.raises(ValueError, match="outside"):
        write_files(sneaky, Path("out"), base=tmp_path)


# --- HTTP ---------------------------------------------------------------------------

M1 = {"mission_id": "M1"}


class SyncReplanner:
    """Runs the real planner at once, in the calling thread."""

    def __init__(self, scenario: Scenario, grids: dict[str, Grid]) -> None:
        self.scenario, self.grids = scenario, grids

    def _mission(self, mission_id: str) -> Mission:
        mission = self.scenario.mission(mission_id)
        assert mission is not None
        return mission

    def request(
        self,
        mission: str,
        revision: int,
        cuts: list[Cut],
        earliest_departure_s: float,
        done: PlanDone,
    ) -> None:
        m, grid = self._mission(mission), self.grids[mission]
        done(revision, plan_mission(self.scenario, m, grid, cuts, earliest_departure_s))

    def request_fallback(
        self, mission: str, revision: int, position: Position, now_s: float, done: PlanDone
    ) -> None:
        m, grid = self._mission(mission), self.grids[mission]
        done(revision, plan_fallback(self.scenario, m, grid, position, now_s))

    def close(self) -> None:
        pass


class FakeOts:
    """Accepts one CoT client, reports the pilot's position, records what it receives."""

    def __init__(self, pilot: str) -> None:
        received = self.received = bytearray()
        sa = self_sa("ANDROID-pilot", pilot, 48.0, 11.0, 0.0)

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                self.request.settimeout(5)
                self.request.sendall(sa)
                while data := self.request.recv(65536):
                    received.extend(data)

        self.server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def ots(scenario: Scenario) -> Iterator[FakeOts]:
    fake = FakeOts(m1(scenario).pilot_callsign)
    yield fake
    fake.close()


def test_http_exports_the_current_plan(scenario: Scenario) -> None:
    app = create_app(scenario, clock_factor=20, cell_m=100, replanner=SyncReplanner)
    with TestClient(app) as client:
        inav = client.get("/api/export/inav", params=M1 | {"altitude_m": 80})
        assert inav.status_code == 200
        assert 'filename="M1.mission"' in inav.headers["content-disposition"]
        items = fromstring(inav.text).findall("missionitem")
        assert items
        assert {i.get("alt") for i in items} == {"80"}
        assert client.get("/api/export/ardupilot", params=M1).text.startswith("QGC WPL 110")
        assert "T/O not before" in client.get("/api/export/briefing", params=M1).text
        assert client.get("/api/export/inav", params=M1 | {"altitude_m": 0}).status_code == 422
        assert client.get("/api/export/inav").status_code == 422  # mission_id is required
        assert client.get("/api/export/inav", params={"mission_id": "M9"}).status_code == 404
        assert client.post("/api/export/atak", params=M1).status_code == 503


def test_http_exports_each_mission_its_own_plan(two_missions: Scenario) -> None:
    app = create_app(two_missions, clock_factor=20, cell_m=100, replanner=SyncReplanner)
    with TestClient(app) as client:
        for mission in two_missions.missions:
            briefing_text = client.get(
                "/api/export/briefing", params={"mission_id": mission.id}
            ).text
            assert mission.drone_id in briefing_text


def test_http_refuses_a_preview_plan(scenario: Scenario) -> None:
    app = create_app(scenario, clock_factor=20, cell_m=100, replanner=SyncReplanner)
    with TestClient(app) as client, client.websocket_connect("/ws?callsign=CMD") as ws:
        ws.receive_json()
        cut = {"type": "cut.schedule", "actor": "CMD", "jammer": "J3", "start_s": 900}
        ws.send_json({"req_id": "r1", "command": cut | {"end_s": 1380}})
        while ws.receive_json()["payload"]["type"] != "ack":
            pass
        response = client.get("/api/export/inav", params=M1)
        assert response.status_code == 409
        assert "unconfirmed" in response.json()["detail"]


def test_http_sends_the_route_to_atak(scenario: Scenario, ots: FakeOts) -> None:
    tak = TakTarget("127.0.0.1", ots.port)
    app = create_app(scenario, clock_factor=20, cell_m=100, replanner=SyncReplanner, tak=tak)
    with TestClient(app) as client:
        response = client.post("/api/export/atak", params=M1)
    assert response.status_code == 200
    body = response.json()
    assert body["chat_sent"] is True
    assert body["pilot_callsign"] == m1(scenario).pilot_callsign
    sent = bytes(ots.received)
    assert b'type="b-m-r"' in sent
    assert b'type="b-t-f"' in sent
    assert b'<dest callsign="PILOT-1"' in sent


def test_http_reports_an_unreachable_ots(scenario: Scenario) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed once the block ends: nothing listens there
    tak = TakTarget("127.0.0.1", port)
    app = create_app(scenario, clock_factor=20, cell_m=100, replanner=SyncReplanner, tak=tak)
    with TestClient(app) as client:
        assert client.post("/api/export/atak", params=M1).status_code == 502
