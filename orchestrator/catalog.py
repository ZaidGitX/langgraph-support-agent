"""Shell commands the stage workers run.

Workflow files under .github/workflows call these same entry points.
Optional scanners that are not installed record a skip and name the CI job.
"""

from __future__ import annotations

import shlex
import shutil
import sys
from pathlib import Path

from .state import STAGE_NAMES, target_of


def _python() -> str:
    return sys.executable


def _tool(name: str) -> str | None:
    sibling = Path(sys.executable).parent / name
    if sibling.is_file():
        return str(sibling)
    return shutil.which(name)


def _skip(tool: str, ci: str) -> dict:
    return {
        "argv": [
            _python(),
            "-m",
            "orchestrator.local_ops",
            "skip-tool",
            "--tool",
            tool,
            "--ci",
            ci,
        ],
        "timeout": 30,
        "label": tool,
    }


def _scanner(tool: str, argv: list[str], ci: str, timeout: int = 120) -> dict:
    if shutil.which(tool):
        return {"argv": argv, "timeout": timeout, "label": tool}
    return _skip(tool, ci)


def commands_for(stage: str, answers: dict | None, overrides: dict | None) -> list[dict]:
    if overrides and stage in overrides:
        return [
            {"argv": list(argv), "timeout": 30, "label": stage}
            for argv in overrides[stage]
        ]
    target = target_of(answers)
    py = _python()
    if stage == "build":
        pip_audit = _tool("pip-audit")
        audit_argv = (
            [
                "bash",
                "-c",
                (
                    "set +e; "
                    f"{shlex.quote(pip_audit)} -r requirements.txt --format json -o var/audit.json; "
                    f"set -e; {shlex.quote(py)} scripts/audit_gate.py var/audit.json"
                ),
            ]
            if pip_audit
            else [py, "-c", "import sys; print('pip-audit is not installed'); sys.exit(1)"]
        )
        return [
            {
                "argv": [py, "-m", "compileall", "-q", "app", "mcp_server", "orchestrator"],
                "timeout": 60,
                "label": "compileall",
            },
            {
                "argv": [py, "-m", "ruff", "check", "app", "mcp_server", "orchestrator", "tests", "scripts"],
                "timeout": 60,
                "label": "ruff",
            },
            {"argv": audit_argv, "timeout": 120, "label": "pip-audit"},
            {
                "argv": [py, "-m", "orchestrator.local_ops", "secrets"],
                "timeout": 60,
                "label": "secrets",
            },
            _scanner(
                "semgrep",
                ["semgrep", "scan", "--config", "p/python", "--metrics=off", "--quiet", "app", "mcp_server", "orchestrator"],
                ".github/workflows/scanners.yml",
            ),
            _scanner(
                "trivy",
                ["trivy", "fs", "--quiet", "--severity", "HIGH,CRITICAL", "."],
                ".github/workflows/scanners.yml",
            ),
            _scanner(
                "gitleaks",
                ["gitleaks", "detect", "--no-git", "--redact", "--source", "."],
                ".github/workflows/scanners.yml",
            ),
        ]
    if stage == "test":
        return [{"argv": [py, "-m", "pytest", "-q"], "timeout": 180, "label": "pytest"}]
    if stage == "package":
        return [{"argv": [py, "-m", "orchestrator.local_ops", "package"], "timeout": 60, "label": "package"}]
    if stage == "container":
        return [{"argv": [py, "-m", "orchestrator.local_ops", "container"], "timeout": 30, "label": "container"}]
    if stage == "release":
        return [{"argv": [py, "-m", "orchestrator.local_ops", "release"], "timeout": 30, "label": "release"}]
    if stage == "deploy":
        return [
            {
                "argv": [py, "-m", "orchestrator.local_ops", "deploy", "--target", target],
                "timeout": 30,
                "label": "deploy",
            }
        ]
    if stage == "uat":
        return [
            {
                "argv": [py, "-m", "orchestrator.local_ops", "uat", "--target", target],
                "timeout": 30,
                "label": "uat",
            }
        ]
    raise ValueError(f"unknown stage {stage}; expected one of {STAGE_NAMES}")
