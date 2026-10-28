"""Shared helpers for the sprint3-agentic-cicd hooks (stdlib only).

Every hook reads one JSON object from stdin, works out whether Claude Code or Cursor
invoked it, and prints at most one JSON object on stdout. Nothing here ever prints an
environment value.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
SKILL_DIR = HOOKS_DIR.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
PROJECT_PY = "/opt/anaconda3/envs/tcs_practice_env/bin/python"

CLAUDE_EVENTS = {
    "PreToolUse", "PostToolUse", "SessionStart", "Stop", "UserPromptSubmit", "SubagentStop",
    "PostToolUseFailure", "Notification", "SessionEnd", "PreCompact", "PostCompact",
}


def read_payload() -> dict:
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def host_of(payload: dict) -> str:
    event = str(payload.get("hook_event_name") or "")
    if event in CLAUDE_EVENTS:
        return "claude"
    if event:
        return "cursor"
    if "conversation_id" in payload or "workspace_roots" in payload:
        return "cursor"
    return "claude"


def project_root(payload: dict | None = None) -> Path:
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root and Path(env_root).is_dir():
        return Path(env_root)
    if payload:
        roots = payload.get("workspace_roots")
        if isinstance(roots, list) and roots:
            return Path(str(roots[0]))
        cwd = payload.get("cwd")
        if cwd and Path(str(cwd)).is_dir():
            return _walk_up(Path(str(cwd)))
    return _walk_up(Path.cwd())


def _walk_up(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "orchestrator" / "graph.py").is_file() or (candidate / ".git").exists():
            return candidate
    # The skill lives at <root>/.claude/skills/sprint3-agentic-cicd; fall back to that root.
    return SKILL_DIR.parents[2]


def emit(obj: dict | None) -> None:
    if obj is None:
        return
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def hooks_off() -> bool:
    return os.environ.get("SPRINT3_HOOKS_OFF", "") == "1"


def run(argv: list[str], cwd: Path | None = None, timeout: int = 20) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            argv, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    output = (completed.stdout or "") + (("\n" + completed.stderr) if completed.stderr else "")
    return int(completed.returncode), output.strip()


def project_python(root: Path) -> str:
    if Path(PROJECT_PY).is_file():
        return PROJECT_PY
    for candidate in (root / ".venv" / "bin" / "python", root / "venv" / "bin" / "python"):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def clip(text: str, limit: int = 1800) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n…[truncated]"
