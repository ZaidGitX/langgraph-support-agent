#!/usr/bin/env python3
"""PreToolUse (Claude Code, matcher Bash) / beforeShellExecution (Cursor) guard.

Denies destructive commands, production targets, and secret printing. Asks before any
command that changes cloud, registry, or remote state (including `git push`). Allows
everything else, so ordinary development is untouched. Set SPRINT3_HOOKS_OFF=1 to bypass.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

from _common import SCRIPTS_DIR, emit, hooks_off, host_of, read_payload  # noqa: E402

sys.path.insert(0, str(SCRIPTS_DIR))
from policy_rules import classify_command  # noqa: E402


def main() -> int:
    payload = read_payload()
    host = host_of(payload)
    command = payload.get("command")
    if not command:
        tool_input = payload.get("tool_input") or {}
        command = tool_input.get("command") if isinstance(tool_input, dict) else ""
    command = str(command or "")

    if hooks_off() or not command.strip():
        if host == "cursor":
            emit({"permission": "allow"})
        return 0

    decision, rule, message = classify_command(command)
    reason = f"[sprint3 guard] {message} (rule {rule})" if rule else ""

    if decision == "allow":
        if host == "cursor":
            emit({"permission": "allow"})
        return 0

    if decision == "deny":
        text = (
            f"{reason}. The sprint 3 policy blocks this in generated artifacts and in agent "
            "commands alike. Use a plan-mode or read-only equivalent, or ask the user to run it."
        )
        if host == "cursor":
            emit({"permission": "deny", "user_message": text, "agent_message": text})
        else:
            emit({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": text,
                }
            })
        return 0

    text = (
        f"{reason}. Cloud, registry, and remote mutations need the user's go-ahead "
        "(ORCHESTRATOR_CLOUD_APPLY only governs the orchestrator, not coding agents)."
    )
    if host == "cursor":
        emit({"permission": "ask", "user_message": text, "agent_message": text})
    else:
        emit({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": text,
            }
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
