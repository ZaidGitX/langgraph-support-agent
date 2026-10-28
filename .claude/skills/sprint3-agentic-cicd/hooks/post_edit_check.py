#!/usr/bin/env python3
"""PostToolUse (Claude Code: Edit|Write|MultiEdit) / postToolUse (Cursor) file check.

After a file is written: block if it contains a credential-shaped literal; lint Python
with ruff from the project interpreter; check workflows for the workflow_dispatch-only
rule; run the artifact policy on generated CI files; remind about docs for orchestrator
changes. Output is additional context (or a block for secrets). Fast: no tests are run.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True

from _common import SCRIPTS_DIR, clip, emit, hooks_off, host_of, project_python, project_root, read_payload, run  # noqa: E402

sys.path.insert(0, str(SCRIPTS_DIR))
from policy_rules import SECRET_PATTERNS, check_path, check_text, kind_for_path  # noqa: E402

PATH_KEYS = ("file_path", "path", "target_file", "relative_workspace_path", "filePath", "notebook_path")
GENERATED_PREFIXES = ("ci/", "deploy/", "docker/", "var/generated/", "docs/deploy/")
PY_DIRS = ("app/", "orchestrator/", "tests/", "scripts/", "mcp_server/")
_SECRET_RE = re.compile("|".join(f"(?:{p})" for p in SECRET_PATTERNS))


def _file_path(payload: dict) -> str:
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        for key in PATH_KEYS:
            value = tool_input.get(key)
            if value:
                return str(value)
    for key in PATH_KEYS:
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def _respond(host: str, context: str, *, block: bool = False) -> None:
    if not context:
        return
    if host == "cursor":
        emit({"additional_context": context})
        return
    if block:
        emit({"decision": "block", "reason": context})
        return
    emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}})


def main() -> int:
    payload = read_payload()
    host = host_of(payload)
    if hooks_off():
        return 0
    raw_path = _file_path(payload)
    if not raw_path:
        return 0
    root = project_root(payload)
    path = Path(raw_path)
    if not path.is_absolute():
        path = root / path
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        rel = path.as_posix()
    if not path.is_file():
        return 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0

    notes: list[str] = []

    # 1. Secrets never land in files.
    if rel != ".env" and not rel.startswith(".claude/skills/"):
        match = _SECRET_RE.search(text)
        if match:
            line = text.count("\n", 0, match.start()) + 1
            _respond(
                host,
                f"[sprint3] {rel}:{line} contains a credential-shaped literal. Remove it, rotate the "
                "credential if it is real, and reference it by placeholder name only.",
                block=True,
            )
            return 0

    # 2. Python: ruff on the one file.
    if rel.endswith(".py") and rel.startswith(PY_DIRS):
        code, output = run([project_python(root), "-m", "ruff", "check", "--output-format", "concise", str(path)], cwd=root, timeout=25)
        if code != 0 and output:
            notes.append(f"ruff on {rel}:\n{clip(output, 900)}")

    # 3. Workflows: only orchestrator.yml may run on push.
    if rel.startswith(".github/workflows/") and rel.endswith((".yml", ".yaml")):
        if Path(rel).name != "orchestrator.yml":
            findings = check_text(text, kind="github_workflow", path=rel)
            errors = [f for f in findings if f["severity"] == "error"]
            warns = [f for f in findings if f["severity"] == "warn"]
            if errors:
                notes.append(
                    f"workflow policy errors in {rel}: "
                    + "; ".join(f"line {f['line']} {f['rule']}: {f['message']}" for f in errors[:6])
                )
            if warns:
                notes.append("workflow warnings: " + "; ".join(f"{f['rule']}" for f in warns[:6]))
            if Path(rel).name.startswith("gen-"):
                notes.extend(f["message"] for f in check_path(rel))

    # 4. Generated CI files and templates: artifact policy.
    if rel.startswith(GENERATED_PREFIXES) or rel.startswith(".env.") or Path(rel).name.startswith(("Dockerfile.generated", "docker-compose.generated")):
        kind = kind_for_path(rel)
        findings = check_text(text, kind=kind, path=rel)
        errors = [f for f in findings if f["severity"] == "error"]
        if errors:
            notes.append(
                f"artifact policy errors in {rel} ({kind}): "
                + "; ".join(f"line {f['line']} {f['rule']}" for f in errors[:8])
                + ". The orchestrator's policy.py will reject this artifact."
            )

    # 5. Orchestrator changes carry docs.
    if rel.startswith("orchestrator/") and rel.endswith(".py"):
        code, status = run(["git", "status", "--porcelain", "--", "docs/SYSTEM_MAP.md"], cwd=root, timeout=10)
        if code == 0 and not status.strip():
            notes.append(
                "docs/SYSTEM_MAP.md is unchanged; update its module table when this phase adds a module, node, endpoint, or env name."
            )

    _respond(host, "\n".join(notes) if notes else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
