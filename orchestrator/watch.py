"""Remote poller (Sprint 4, phase 10): start a run when a watched branch moves.

For git hosts that cannot reach the orchestrator with a webhook. ``ORCHESTRATOR_WATCH_REMOTE``
names the remote, ``ORCHESTRATOR_WATCH_INTERVAL`` the seconds between polls, and the run
profile's ``remotes.branches`` which heads count (empty = every head). The last seen commits
live in ``var/workspaces/.watch.json`` so a restart does not replay old pushes.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time

from . import workspace
from .redaction import redact


def heads(url: str, timeout: int = 60) -> dict[str, str]:
    """``git ls-remote --heads`` as {branch: sha}. Raises WorkspaceError on failure."""
    try:
        completed = subprocess.run(
            ["git", "ls-remote", "--heads", "--", url], capture_output=True, text=True, timeout=timeout, check=False, env=workspace.git_env()
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise workspace.WorkspaceError(f"git ls-remote: {type(exc).__name__}") from exc
    if completed.returncode != 0:
        raise workspace.WorkspaceError("git ls-remote failed: " + redact(completed.stderr or "", limit=300).strip())
    result: dict[str, str] = {}
    for line in (completed.stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            result[parts[1][len("refs/heads/"):]] = parts[0]
    return result


def _state_path():
    return workspace.workspaces_root() / ".watch.json"


def load_seen() -> dict[str, str]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def save_seen(seen: dict[str, str]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def check_once(service, url: str, branches: list[str] | None = None, seen: dict[str, str] | None = None, *, first_run_starts: bool = False) -> list[dict]:
    """Compare the remote heads with ``seen``; prepare a workspace and start a run per new commit.

    The first poll only records what is there unless ``first_run_starts`` is set, so a service
    restart never replays the current tip. Returns the triggers it started.
    """
    current = heads(url)
    watched = [name for name in (branches or []) if name] or sorted(current)
    seen = seen if seen is not None else load_seen()
    started: list[dict] = []
    initial = not seen
    for branch in watched:
        sha = current.get(branch)
        if not sha:
            continue
        if seen.get(branch) == sha:
            continue
        seen[branch] = sha
        if initial and not first_run_starts:
            continue
        prepared = workspace.prepare(url, f"refs/heads/{branch}", sha)
        trigger = {
            "source": "poll",
            "repo": url,
            "ref": f"refs/heads/{branch}",
            "sha": sha,
            "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        service.start(repo_root=prepared["path"], trigger=trigger)
        started.append(trigger)
    save_seen(seen)
    return started


class RemoteWatcher(threading.Thread):
    """A daemon thread that polls one remote until ``stop`` is called."""

    def __init__(self, service, url: str, interval: int = 60, branches: list[str] | None = None) -> None:
        super().__init__(name="orchestrator-watch", daemon=True)
        self.service = service
        self.url = url
        self.interval = max(10, int(interval))
        self.branches = list(branches or [])
        self._stop = threading.Event()
        self.last_error = ""
        self.polls = 0

    def run(self) -> None:
        seen = load_seen()
        while not self._stop.is_set():
            try:
                check_once(self.service, self.url, self.branches, seen)
                self.last_error = ""
            except workspace.WorkspaceError as exc:
                self.last_error = str(exc)
            self.polls += 1
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
