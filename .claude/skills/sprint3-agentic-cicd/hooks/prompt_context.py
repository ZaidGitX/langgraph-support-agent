#!/usr/bin/env python3
"""UserPromptSubmit (Claude Code only): nudge the model to load the sprint 3 skill.

When the prompt is about the orchestrator (Astra, Jev, DAG, stages, providers, runners,
workflows, dashboard, deploy targets, scanners), add one line of context pointing at the
skill. Cheap, and it fixes the under-triggering that skill descriptions alone suffer from.
"""

from __future__ import annotations

import re
import sys

sys.dont_write_bytecode = True

from _common import emit, hooks_off, read_payload  # noqa: E402

KEYWORDS = re.compile(
    r"\b(sprint\s*3|sprint3|astra|jev|typesafe|dag|stage(s)?|candidate(s)?|pipeline(s)?|orchestrator|"
    r"provider(s)?|runner(s)?|scanner(s)?|semgrep|trivy|gitleaks|snyk|codeql|sonar|zap|stackhawk|"
    r"deploy|release|uat|workflow(s)?|github actions|jenkins|codebuild|cloud ?build|azure pipelines|"
    r"ecs|ecr|cloud run|container apps|kubernetes|kubectl|docker(file)?|compose|readiness|credential(s)?|"
    r"placeholder(s)?|env(ironment)? (target|platform)|rag|retrieval|episode(s)?|dashboard|session ui|"
    r"langgraph|interrupt|quality gate|guardrail(s)?|policy)\b",
    re.IGNORECASE,
)


def main() -> int:
    payload = read_payload()
    if hooks_off():
        return 0
    prompt = str(payload.get("prompt") or "")
    if not prompt or "sprint3-agentic-cicd" in prompt:
        return 0
    if not KEYWORDS.search(prompt):
        return 0
    emit({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "This request concerns the agentic CI/CD orchestrator (Sprint 3). Load the "
                "sprint3-agentic-cicd skill (Skill tool) before planning or editing: it holds the "
                "phase plan, component specs, scripts, templates, and guardrails, and it names the "
                "project interpreter /opt/anaconda3/envs/tcs_practice_env/bin/python."
            ),
        }
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
