from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from orchestrator import connect, vault

FAKE_LOGIN = Path(__file__).with_name("fakes") / "fake_login.py"


def _fake_value(tag: str) -> str:
    return "api-test-" + tag + "-" + "0123456789abcdef"


def test_connections_list_names_only_and_store_is_write_only():
    with TestClient(app) as client:
        listing = client.get("/api/orchestrator/connections")
        assert listing.status_code == 200
        body = listing.json()
        keys = [group["key"] for group in body["groups"]]
        assert {"github", "aws", "jenkins", "git"} <= set(keys) and "app" not in keys
        jenkins = next(group for group in body["groups"] if group["key"] == "jenkins")
        assert jenkins["login"]["available"] is False and "vault form" in jenkins["login"]["reason"]
        assert jenkins["verify"] is True and jenkins["mode"] == "plan"

        value = _fake_value("jenkins")
        stored = client.post("/api/orchestrator/connections/jenkins/secrets", json={"values": {"JENKINS_API_TOKEN": value, "JENKINS_URL": "http://jenkins.internal:8443"}})
        assert stored.status_code == 200
        assert stored.json()["stored"] == ["JENKINS_API_TOKEN", "JENKINS_URL"]
        assert value not in stored.text and "jenkins.internal" not in stored.text
        row = next(item for item in stored.json()["connection"]["env"] if item["name"] == "JENKINS_API_TOKEN")
        assert row["set"] is True and row["source"] == "vault"
        assert vault.names() == ["JENKINS_API_TOKEN", "JENKINS_URL"]

        wrong_group = client.post("/api/orchestrator/connections/aws/secrets", json={"values": {"JENKINS_API_TOKEN": value}})
        assert wrong_group.status_code == 400
        unknown = client.post("/api/orchestrator/connections/nope/secrets", json={"values": {"X": "y"}})
        assert unknown.status_code == 404

        everything = client.get("/api/orchestrator/connections").text + client.get("/api/orchestrator/readiness").text
        assert value not in everything

        removed = client.delete("/api/orchestrator/connections/jenkins/secrets/JENKINS_API_TOKEN")
        assert removed.status_code == 200 and removed.json()["removed"] is True
        assert vault.names() == ["JENKINS_URL"]


def test_mutating_routes_require_the_dashboard_token(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_UI_TOKEN", "t0ken-for-tests")
    with TestClient(app) as client:
        assert client.get("/api/orchestrator/connections").json()["token_required"] is True
        denied = client.post("/api/orchestrator/connections/jenkins/secrets", json={"values": {"JENKINS_URL": "http://127.0.0.1:8080"}})
        assert denied.status_code == 401
        denied_session = client.post("/api/orchestrator/session")
        assert denied_session.status_code == 401
        allowed = client.post(
            "/api/orchestrator/connections/jenkins/secrets",
            json={"values": {"JENKINS_URL": "http://127.0.0.1:8080"}},
            headers={"X-Orchestrator-Token": "t0ken-for-tests"},
        )
        assert allowed.status_code == 200
        assert client.get("/api/orchestrator/session").status_code == 200


def test_login_flow_over_http(monkeypatch):
    spec = {"argv": [sys.executable, str(FAKE_LOGIN)], "url_pattern": r"https://github\.com/login/device", "code_pattern": r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b", "input": "", "requires": []}
    monkeypatch.setattr(connect, "login_spec", lambda group: spec if group == "github" else None)
    monkeypatch.setattr(connect, "verify_spec", lambda group: {"argv": [sys.executable, "-c", "print('ok')"]} if group == "github" else None)
    with TestClient(app) as client:
        assert client.post("/api/orchestrator/connections/jenkins/login").status_code == 409
        started = client.post("/api/orchestrator/connections/github/login")
        assert started.status_code == 200
        flow = started.json()["flow"]
        deadline = time.time() + 10
        while flow["status"] not in {"done", "failed", "expired"} and time.time() < deadline:
            time.sleep(0.05)
            flow = client.get(f"/api/orchestrator/connections/github/login/{flow['id']}").json()["flow"]
        assert flow["status"] == "done" and flow["code"] == "ABCD-1234"
        assert client.get(f"/api/orchestrator/connections/aws/login/{flow['id']}").status_code == 404
        deadline = time.time() + 5
        while connect.verified("github") is None and time.time() < deadline:
            time.sleep(0.05)
        verified = client.post("/api/orchestrator/connections/github/verify")
        assert verified.status_code == 200 and verified.json()["verified"]["ok"] is True
        github = verified.json()["connection"]
        assert github["authenticated"] is True and github["checked_at"]
        assert json.dumps(github).count("ok") >= 1
