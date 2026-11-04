"""Vault -> GitHub repository secrets (Sprint 4, phase 12): values travel on stdin only."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from orchestrator import connect

FAKE = "sync-test-value-0123456789abcdef"


def test_sync_sends_values_on_stdin_and_reports_names_only():
    calls: list[tuple[list[str], str]] = []

    def runner(argv: list[str], value: str) -> tuple[bool, str]:
        calls.append((list(argv), value))
        return (False, "HTTP 403: not permitted " + FAKE) if argv[3] == "AWS_ACCESS_KEY_ID" else (True, "")

    env = {"GITHUB_REPOSITORY": "owner/name", "OPENAI_API_KEY": FAKE, "AWS_ACCESS_KEY_ID": FAKE, "AWS_SECRET_ACCESS_KEY": ""}
    result = connect.sync_secrets("github", groups=["aws", "app"], env=env, runner=runner)
    assert result["repository"] == "owner/name"
    assert result["synced"] == ["OPENAI_API_KEY"]
    assert result["skipped"] and "AWS_SECRET_ACCESS_KEY" in result["skipped"] and "AWS_SESSION_TOKEN" in result["skipped"]
    assert result["failed"] == ["AWS_ACCESS_KEY_ID: HTTP 403: not permitted " + FAKE]
    assert all(FAKE not in " ".join(argv) for argv, _ in calls)
    assert {argv[3] for argv, _ in calls} == {"OPENAI_API_KEY", "AWS_ACCESS_KEY_ID"}
    # gh reads the value from stdin when --body is absent; --body-file is not a gh flag (live run 4).
    assert all(argv[:3] == ["gh", "secret", "set"] and argv[4:] == ["--repo", "owner/name"] for argv, _ in calls)
    assert all("--body" not in " ".join(argv) for argv, _ in calls)
    assert all(value == FAKE for _, value in calls)
    # A value never reaches the result except inside a redacted failure line from the CLI itself.
    assert FAKE not in str(result["synced"]) + str(result["skipped"])


def test_sync_needs_a_repository_and_a_known_target():
    with pytest.raises(connect.ConnectError, match="no GitHub repository"):
        connect.sync_secrets("github", env={"GITHUB_REPOSITORY": ""}, runner=lambda argv, value: (True, ""))
    with pytest.raises(connect.ConnectError, match="github only"):
        connect.sync_secrets("jenkins", env={}, runner=lambda argv, value: (True, ""))


def test_repository_comes_from_the_origin_remote(tmp_path):
    assert connect.github_repository({"GITHUB_REPOSITORY": "a/b"}) == "a/b"
    assert connect._GITHUB_REMOTE.search("git@github.com:ZaidGitX/langgraph-support-agent.git").groups() == ("ZaidGitX", "langgraph-support-agent")
    assert connect._GITHUB_REMOTE.search("https://github.com/owner/repo").groups() == ("owner", "repo")
    assert connect.github_repository({}, cwd=str(tmp_path)) == "" and connect.github_repository(None, cwd=str(tmp_path)) == ""


def test_sync_route_reports_names_only(monkeypatch):
    def fake_sync(target, *, groups=None, names=None, env=None, runner=None):
        assert target == "github" and groups == ["aws"]
        return {"target": "github", "repository": "owner/name", "synced": ["AWS_ACCESS_KEY_ID"], "skipped": [], "failed": [], "at": "now"}

    monkeypatch.setattr(connect, "sync_secrets", fake_sync)
    with TestClient(app) as client:
        response = client.post("/api/orchestrator/connections/github/sync", json={"groups": ["aws"]})
        assert response.status_code == 200
        assert response.json()["sync"]["synced"] == ["AWS_ACCESS_KEY_ID"]
        assert client.post("/api/orchestrator/connections/aws/sync", json={}).status_code == 409
        assert client.post("/api/orchestrator/connections/nope/sync", json={}).status_code == 404


def test_cli_prints_names_only(monkeypatch, capsys):
    monkeypatch.setattr(
        connect,
        "sync_secrets",
        lambda target, *, groups=None, names=None, env=None, runner=None: {
            "target": target, "repository": "o/r", "synced": ["OPENAI_API_KEY"], "skipped": ["AWS_SESSION_TOKEN"], "failed": [], "at": "now",
        },
    )
    assert connect.main(["sync-github", "--group", "app"]) == 0
    out = capsys.readouterr().out
    assert "o/r: synced OPENAI_API_KEY" in out and "no value here for AWS_SESSION_TOKEN" in out
