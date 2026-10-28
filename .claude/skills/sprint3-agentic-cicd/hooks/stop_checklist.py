#!/usr/bin/env python3
"""Stop (Claude Code) / stop (Cursor): remind to run the sprint 3 checks when code changed.

Non-blocking by default: it adds a system message when sprint files changed since the last
green run of scripts/run_sprint3_checks.sh (stamp var/.sprint3-checks.json). With
SPRINT3_STRICT_STOP=1 it blocks the stop once (Claude Code) or sends one follow-up (Cursor).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True

from _common import emit, hooks_off, host_of, project_root, read_payload, run  # noqa: E402

WATCH_PREFIXES = ("orchestrator/", "app/", "frontend/src/", "tests/", "scripts/", ".github/workflows/", "docker-compose.yml", "Dockerfile", ".env.example")


def _changed_files(root: Path) -> list[str]:
    code, output = run(["git", "status", "--porcelain"], cwd=root, timeout=10)
    if code != 0:
        return []
    files: list[str] = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().split(" -> ")[-1]
        if path.startswith(WATCH_PREFIXES):
            files.append(path)
    return files


def _stamp(root: Path) -> dict:
    path = root / "var" / ".sprint3-checks.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> int:
    payload = read_payload()
    if hooks_off():
        return 0
    host = host_of(payload)
    if host == "claude" and payload.get("stop_hook_active"):
        return 0
    if host == "cursor" and int(payload.get("loop_count") or 0) >= 1:
        return 0
    root = project_root(payload)
    changed = _changed_files(root)
    if not changed:
        return 0
    stamp = _stamp(root)
    newest = 0.0
    for rel in changed:
        try:
            newest = max(newest, (root / rel).stat().st_mtime)
        except OSError:
            continue
    stale = (not stamp) or (not stamp.get("ok")) or float(stamp.get("timestamp") or 0) < newest
    if not stale:
        return 0
    shown = ", ".join(changed[:8]) + (" …" if len(changed) > 8 else "")
    message = (
        f"[sprint3] {len(changed)} sprint file(s) changed since the last green check run ({shown}). "
        "Run `bash .claude/skills/sprint3-agentic-cicd/scripts/run_sprint3_checks.sh` and report the result "
        "before calling the phase done."
    )
    strict = os.environ.get("SPRINT3_STRICT_STOP", "") == "1"
    if host == "cursor":
        if strict:
            emit({"followup_message": message})
        return 0
    if strict:
        emit({"decision": "block", "reason": message})
    else:
        emit({"systemMessage": message})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
