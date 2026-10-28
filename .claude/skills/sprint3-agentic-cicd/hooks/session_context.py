#!/usr/bin/env python3
"""SessionStart (Claude Code) / sessionStart (Cursor): inject sprint 3 readiness and progress.

Runs scripts/preflight.py --brief and scripts/sprint3_progress.py --brief and returns the
combined text as additional context. Names only; no values. Budget: a few seconds.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

from _common import SCRIPTS_DIR, clip, emit, hooks_off, host_of, project_root, read_payload, run  # noqa: E402


def main() -> int:
    payload = read_payload()
    if hooks_off():
        return 0
    host = host_of(payload)
    root = project_root(payload)
    parts: list[str] = [
        "[sprint3-agentic-cicd] This repository carries the Sprint 3 skill at "
        ".claude/skills/sprint3-agentic-cicd (single source of truth for the agentic CI/CD "
        "orchestrator). Invoke it before orchestrator, workflow, provider, or dashboard work."
    ]
    code, pre = run([sys.executable, str(SCRIPTS_DIR / "preflight.py"), "--brief"], cwd=root, timeout=12)
    if pre:
        parts.append(clip(pre, 3500))
    code, prog = run([sys.executable, str(SCRIPTS_DIR / "sprint3_progress.py"), "--brief"], cwd=root, timeout=8)
    if prog:
        parts.append(clip(prog, 2500))
    context = "\n\n".join(parts)
    if host == "cursor":
        emit({"additional_context": context})
    else:
        emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
