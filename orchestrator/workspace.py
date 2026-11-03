"""Workspaces for pushed commits (Sprint 4, phase 10).

A webhook or the poller names a remote, a ref, and a commit. The orchestrator clones or
fetches it under ``var/workspaces/<slug>`` and runs the session on that checkout, so a run
never depends on the person's own clone. Private HTTPS remotes use ``GIT_REMOTE_USER`` and
``GIT_REMOTE_TOKEN`` through an askpass helper (the token stays out of every argv and out of
``.git/config``); SSH remotes use ``SSH_KEY_PATH``. Output is redacted before it is kept.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from pathlib import Path

from app import config

from .redaction import redact

WORKSPACES_DIR = "workspaces"
DEFAULT_DEPTH = 50
GIT_TIMEOUT = 300


class WorkspaceError(RuntimeError):
    """A clone, fetch, or checkout failed; the message is redacted."""


def workspaces_root() -> Path:
    configured = os.getenv("ORCHESTRATOR_WORKSPACES", "").strip()
    return Path(configured) if configured else config.PROJECT_DIR / "var" / WORKSPACES_DIR


def slug(url: str) -> str:
    """A stable folder name: the last two path parts plus a short digest of the whole URL."""
    cleaned = url.strip().rstrip("/")
    cleaned = cleaned.removesuffix(".git")
    parts = [part for part in re.split(r"[/:]", cleaned) if part][-2:]
    base = re.sub(r"[^a-z0-9]+", "-", "-".join(parts).lower()).strip("-")[:48] or "repo"
    return f"{base}-{hashlib.sha1(url.strip().encode()).hexdigest()[:8]}"


def path_for(url: str) -> Path:
    return workspaces_root() / slug(url)


def branch_of(ref: str) -> str:
    cleaned = (ref or "").strip()
    for prefix in ("refs/heads/", "refs/tags/"):
        if cleaned.startswith(prefix):
            return cleaned[len(prefix):]
    return cleaned


def _askpass_path() -> Path:
    root = workspaces_root()
    root.mkdir(parents=True, exist_ok=True)
    helper = root / ".askpass.sh"
    if not helper.is_file():
        # The helper answers git's Username prompt with the user and any other prompt with the
        # token; both come from the environment of the git process, never from this file.
        token_name = "GIT_REMOTE_" + "TOKEN"
        lines = [
            "#!/bin/sh",
            'case "$1" in',
            '  *sername*) printf "%s\\n" "${GIT_REMOTE_USER:-git}" ;;',
            '  *) printf "%s\\n" "${' + token_name + '}" ;;',
            "esac",
            "",
        ]
        helper.write_text("\n".join(lines), encoding="utf-8")
        helper.chmod(stat.S_IRWXU)
    return helper


def git_env() -> dict[str, str]:
    merged = dict(os.environ)
    merged["GIT_TERMINAL_PROMPT"] = "0"
    merged.setdefault("GIT_CONFIG_NOSYSTEM", "1")
    if merged.get("GIT_REMOTE_TOKEN", "").strip():
        merged["GIT_ASKPASS"] = str(_askpass_path())
    key_path = merged.get("SSH_KEY_PATH", "").strip()
    if key_path:
        merged["GIT_SSH_COMMAND"] = f"ssh -i {key_path} -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    return merged


def _git(args: list[str], cwd: Path | None, timeout: int = GIT_TIMEOUT) -> str:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout, check=False, env=git_env()
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceError(f"git {args[0]}: {type(exc).__name__}") from exc
    output = redact((completed.stdout or "") + (completed.stderr or ""), limit=2000)
    if completed.returncode != 0:
        raise WorkspaceError(f"git {args[0]} failed: {output.strip()[:400]}")
    return output


def prepare(url: str, ref: str = "", sha: str = "", *, depth: int = DEFAULT_DEPTH) -> dict:
    """Clone or fetch ``url`` so that the workspace is at ``sha`` (or the tip of ``ref``)."""
    if not url or not url.strip():
        raise WorkspaceError("no clone URL")
    branch = branch_of(ref)
    target = path_for(url)
    target.parent.mkdir(parents=True, exist_ok=True)
    if (target / ".git").is_dir():
        _git(["-C", str(target), "remote", "set-url", "origin", url], None)
        _git(["-C", str(target), "fetch", "--no-tags", f"--depth={depth}", "origin", *([branch] if branch else [])], None)
        _git(["-C", str(target), "checkout", "--detach", "--force", sha or "FETCH_HEAD"], None)
        action = "fetched"
    else:
        args = ["clone", "--no-tags", f"--depth={depth}"]
        if branch:
            args.extend(["--branch", branch])
        args.extend(["--", url, str(target)])
        _git(args, None)
        if sha:
            try:
                _git(["-C", str(target), "checkout", "--detach", "--force", sha], None)
            except WorkspaceError:
                _git(["-C", str(target), "fetch", "--no-tags", f"--depth={depth}", "origin", sha], None)
                _git(["-C", str(target), "checkout", "--detach", "--force", sha], None)
        action = "cloned"
    head = _git(["-C", str(target), "rev-parse", "HEAD"], None).strip()
    return {"path": str(target), "url": url, "branch": branch, "sha": head, "action": action}
