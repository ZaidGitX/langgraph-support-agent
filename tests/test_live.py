from __future__ import annotations

import sys
import time

from fastapi.testclient import TestClient

from app.main import app
from orchestrator.session import SessionService
from orchestrator.state import STAGE_NAMES
from tests.test_autopilot import PROFILE
from tests.test_orchestrator import FakeAstra, FakeJev


def _slow_build_commands() -> dict[str, list[list[str]]]:
    quick = [sys.executable, "-c", "print('ok')"]
    slow = [sys.executable, "-c", "import time; time.sleep(0.9); print('ok')"]
    return {name: [slow if name == "build" else quick] for name in STAGE_NAMES}


def _until(predicate, timeout: float = 20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def test_live_view_marks_running_stages_and_records_events(tmp_path):
    (tmp_path / "orchestrator.yaml").write_text(PROFILE, encoding="utf-8")
    service = SessionService(db_path=str(tmp_path / "live.sqlite"), jev=FakeJev(), astra=FakeAstra(), commands=_slow_build_commands(), repo_root=str(tmp_path))
    try:
        started = service.start(trigger={"source": "manual"})
        assert started["seq"] == 0 and started["started_at"]
        # Autopilot answers and approves; build then runs for ~0.9 s, long enough to be seen.
        running = _until(lambda: "build" in (service.snapshot().get("running_stages") or []) and service.snapshot())
        board = {stage["name"]: stage["status"] for stage in running["stages"]}
        assert board["build"] == "running" and running["phase"] == "running"
        wave = next(item for item in running["events"] if item["kind"] == "wave")
        assert "build" in wave["stages"] and wave["seq"] > 0
        view = _until(lambda: service.snapshot() if service.snapshot()["phase"] == "uat" else None)
        kinds = {item["kind"] for item in view["events"]}
        assert {"node", "gate", "jev", "plan", "stage", "wave", "autopilot", "note"} <= kinds, kinds
        build_event = next(item for item in view["events"] if item["kind"] == "stage" and item["name"] == "build")
        assert build_event["status"] == "pass" and build_event["runner"] == "local"
        assert view["running_stages"] == [] and view["seq"] >= len(view["events"])
        nodes = [item["node"] for item in view["timeline"]]
        assert nodes[:2] == ["checkout", "discover_ecosystem"] and "dispatch_stages" in nodes
        assert all(item["t"] >= 0 for item in view["timeline"])
        newer = service.events_since(wave["seq"])
        assert newer["seq"] == view["seq"] and all(item["seq"] > wave["seq"] for item in newer["events"])
        assert service.events_since(view["seq"])["events"] == []
    finally:
        service.close()


def test_events_endpoint_and_sse_stream(tmp_path):
    (tmp_path / "orchestrator.yaml").write_text(PROFILE, encoding="utf-8")
    service = SessionService(db_path=str(tmp_path / "sse.sqlite"), jev=FakeJev(), astra=FakeAstra(), commands=_slow_build_commands(), repo_root=str(tmp_path))
    with TestClient(app) as client:
        original = app.state.sessions
        app.state.sessions = service
        try:
            idle = client.get("/api/orchestrator/session/events")
            assert idle.status_code == 200 and idle.json() == {"seq": 0, "phase": "idle", "run_id": None, "events": [], "more": False}
            with client.stream("GET", "/api/orchestrator/session/stream?max=1") as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                text = response.read().decode("utf-8")
            assert text.startswith("retry: 2000") and "event: change" in text and '"phase": "idle"' in text
            client.post("/api/orchestrator/session")
            _until(lambda: service.snapshot()["phase"] == "uat")
            page = client.get("/api/orchestrator/session/events?since=0&limit=5").json()
            assert len(page["events"]) == 5 and page["more"] is True and page["phase"] == "uat"
            rest = client.get(f"/api/orchestrator/session/events?since={page['events'][-1]['seq']}&limit=1000").json()
            assert rest["more"] is False and rest["events"][0]["seq"] == page["events"][-1]["seq"] + 1
            assert client.get("/api/orchestrator/session/events?since=-1").status_code == 422
            with client.stream("GET", "/api/orchestrator/session/stream?max=1") as response:
                frame = response.read().decode("utf-8")
            assert '"phase": "uat"' in frame and '"gate": "uat_signoff"' in frame
        finally:
            service.close()
            app.state.sessions = original
