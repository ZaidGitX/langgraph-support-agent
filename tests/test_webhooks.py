from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from orchestrator import watch, workspace
from orchestrator.session import SessionService
from tests.test_autopilot import PROFILE
from tests.test_orchestrator import FakeAstra, FakeJev, _echo

SECRET = "webhook-test-" + "secret-0123456789"


def _git(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", *args],
        cwd=str(cwd), capture_output=True, text=True, check=True, timeout=60,
    )
    return completed.stdout.strip()


def _bare_repo(tmp_path: Path) -> tuple[str, Path]:
    """A bare remote with one commit on main; returns its file:// URL and the work clone."""
    bare = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(bare)], check=True, capture_output=True, timeout=60)
    subprocess.run(["git", "init", "--initial-branch=main", str(work)], check=True, capture_output=True, timeout=60)
    (work / "orchestrator.yaml").write_text(PROFILE, encoding="utf-8")
    (work / "README.md").write_text("# pushed repo\n", encoding="utf-8")
    _git(["add", "."], work)
    _git(["commit", "-q", "-m", "first"], work)
    _git(["remote", "add", "origin", str(bare)], work)
    _git(["push", "-q", "origin", "main"], work)
    return f"file://{bare}", work


def _push_commit(work: Path, text: str) -> str:
    (work / "README.md").write_text(text, encoding="utf-8")
    _git(["commit", "-q", "-am", text], work)
    _git(["push", "-q", "origin", "main"], work)
    return _git(["rev-parse", "HEAD"], work)


def _signed(body: dict) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": "sha256=" + signature, "X-GitHub-Event": "push", "content-type": "application/json"}


def _wait_phase(service: SessionService, phases: set[str], timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        view = service.snapshot()
        if view["phase"] in phases:
            return view
        if view["phase"] == "error":
            raise AssertionError(view["error"])
        time.sleep(0.02)
    raise AssertionError(service.snapshot())


def test_workspace_clone_then_fetch(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_WORKSPACES", str(tmp_path / "ws"))
    url, work = _bare_repo(tmp_path)
    first = workspace.prepare(url, "refs/heads/main")
    assert first["action"] == "cloned" and (Path(first["path"]) / "orchestrator.yaml").is_file()
    sha = _push_commit(work, "# second\n")
    second = workspace.prepare(url, "refs/heads/main", sha)
    assert second["action"] == "fetched" and second["sha"] == sha and second["path"] == first["path"]
    assert (Path(second["path"]) / "README.md").read_text() == "# second\n"
    assert workspace.slug(url) == workspace.slug(url) and workspace.branch_of("refs/heads/main") == "main"


def test_signed_push_clones_and_starts_a_run(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_WORKSPACES", str(tmp_path / "ws"))
    monkeypatch.setenv("ORCHESTRATOR_WEBHOOK_SECRET", SECRET)
    url, work = _bare_repo(tmp_path)
    sha = _git(["rev-parse", "HEAD"], work)
    service = SessionService(db_path=str(tmp_path / "hook.sqlite"), jev=FakeJev(), astra=FakeAstra(), commands=_echo(), repo_root=str(tmp_path))
    with TestClient(app) as client:
        original = app.state.sessions
        app.state.sessions = service
        try:
            raw, headers = _signed({"ref": "refs/heads/main", "after": sha, "repository": {"clone_url": url, "full_name": "demo/pushed"}})
            unsigned = client.post("/api/orchestrator/hooks/push", content=raw, headers={"content-type": "application/json"})
            assert unsigned.status_code == 401
            bad = client.post("/api/orchestrator/hooks/push", content=raw, headers={**headers, "X-Hub-Signature-256": "sha256=" + "0" * 64})
            assert bad.status_code == 401
            ping = client.post("/api/orchestrator/hooks/push", content=raw, headers={**headers, "X-GitHub-Event": "ping"})
            assert ping.status_code == 202 and ping.json()["event"] == "ping"
            accepted = client.post("/api/orchestrator/hooks/push", content=raw, headers=headers)
            assert accepted.status_code == 202, accepted.text
            body = accepted.json()
            assert body["host"] == "github" and body["sha"] == sha and body["run_id"] and body["queued"] == 0
            assert (Path(body["workspace"]) / "orchestrator.yaml").is_file()
            view = _wait_phase(service, {"uat"})
            assert view["trigger"]["source"] == "webhook" and view["trigger"]["sha"] == sha and view["trigger"]["repo"] == "demo/pushed"
            assert view["autopilot"]["enabled"] is True
            # A second push while the first run waits at UAT is queued, not dropped.
            sha2 = _push_commit(work, "# second push\n")
            raw2, headers2 = _signed({"ref": "refs/heads/main", "after": sha2, "repository": {"clone_url": url, "full_name": "demo/pushed"}})
            queued = client.post("/api/orchestrator/hooks/push", content=raw2, headers=headers2)
            assert queued.status_code == 202 and queued.json()["queued"] == 1 and queued.json()["run_id"] is None
            assert service.snapshot()["queue"][0]["sha"] == sha2
            other = client.post("/api/orchestrator/hooks/push", content=raw2, headers={**headers2, "X-GitHub-Event": "issues"})
            assert other.status_code == 202 and "skipped" in other.json()
        finally:
            service.close()
            app.state.sessions = original


def test_push_is_refused_without_a_secret_and_branch_filter_skips(tmp_path, monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_WEBHOOK_SECRET", raising=False)
    service = SessionService(db_path=str(tmp_path / "x.sqlite"), jev=FakeJev(), astra=FakeAstra(), commands=_echo(), repo_root=str(tmp_path))
    (tmp_path / "orchestrator.yaml").write_text(PROFILE + "remotes:\n  branches: [main]\n", encoding="utf-8")
    with TestClient(app) as client:
        original = app.state.sessions
        app.state.sessions = service
        try:
            raw, headers = _signed({"ref": "refs/heads/main", "after": "a" * 40, "repository": {"clone_url": "file:///nowhere"}})
            assert client.post("/api/orchestrator/hooks/push", content=raw, headers=headers).status_code == 503
            monkeypatch.setenv("ORCHESTRATOR_WEBHOOK_SECRET", SECRET)
            raw, headers = _signed({"ref": "refs/heads/feature", "after": "a" * 40, "repository": {"clone_url": "file:///nowhere"}})
            skipped = client.post("/api/orchestrator/hooks/push", content=raw, headers=headers)
            assert skipped.status_code == 202 and "not in remotes.branches" in skipped.json()["skipped"]
            raw, headers = _signed({"ref": "refs/heads/main", "after": "a" * 40, "repository": {"clone_url": "file:///nowhere"}})
            failed = client.post("/api/orchestrator/hooks/push", content=raw, headers=headers)
            assert failed.status_code == 502
            gitlab = client.post("/api/orchestrator/hooks/push", content=raw, headers={"X-Gitlab-Token": SECRET, "content-type": "application/json"})
            assert gitlab.status_code == 400  # signed, but a GitHub-shaped body is not a GitLab push
        finally:
            service.close()
            app.state.sessions = original


class Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.repo_root = "."

    def start(self, repo_root=None, trigger=None):
        self.calls.append({"repo_root": repo_root, "trigger": trigger})
        return {"run_id": "r", "queue": []}


def test_poller_starts_a_run_only_for_a_new_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_WORKSPACES", str(tmp_path / "ws"))
    url, work = _bare_repo(tmp_path)
    recorder = Recorder()
    seen: dict[str, str] = {}
    assert watch.check_once(recorder, url, ["main"], seen) == [] and seen["main"]
    assert recorder.calls == []
    sha = _push_commit(work, "# moved\n")
    started = watch.check_once(recorder, url, ["main"], seen)
    assert len(started) == 1 and started[0]["sha"] == sha and started[0]["source"] == "poll"
    assert recorder.calls[0]["trigger"]["sha"] == sha and (Path(recorder.calls[0]["repo_root"]) / "README.md").read_text() == "# moved\n"
    assert watch.check_once(recorder, url, ["main"], seen) == []
    assert watch.load_seen()["main"] == sha
