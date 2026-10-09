"""Checkout and ecosystem discovery from the local working tree.

graphify and OpenSearch are deferred. This scan is the retrieval stand-in.
"""

from __future__ import annotations

import json
from pathlib import Path

from .state import Question

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "var",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
}

POLICY_QUESTIONS: tuple[Question, ...] = (
    {
        "id": "env_target",
        "prompt": "Which environment should this run target: local, dev, staging, or uat?",
        "reason": "The repo has no deployment-target document. Production is not a target.",
    },
    {
        "id": "test_depth",
        "prompt": "What test depth should this run use: unit, or unit plus the HTTP smoke?",
        "reason": "pytest is present, but the release bar is not written down.",
    },
    {
        "id": "release_policy",
        "prompt": "Confirm the release policy. This system stops at UAT and has no production edge.",
        "reason": "No release-policy file is in the checkout.",
    },
)


def _walk(root: Path):
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            yield path


def scan_checkout(root: str) -> dict:
    base = Path(root)
    suffixes: set[str] = set()
    manifests: list[str] = []
    known = {
        "requirements.txt",
        "requirements-dev.txt",
        "Dockerfile",
        "docker-compose.yml",
        "frontend/package.json",
        "pyproject.toml",
        "package.json",
    }
    for path in _walk(base):
        if path.suffix:
            suffixes.add(path.suffix.lower())
        relative = path.relative_to(base).as_posix()
        if relative in known or path.name in {"requirements.txt", "Dockerfile", "package.json"}:
            manifests.append(relative)

    languages: list[str] = []
    if ".py" in suffixes:
        languages.append("Python")
    if suffixes & {".js", ".jsx", ".ts", ".tsx"}:
        languages.append("JavaScript")

    build_tools: list[str] = []
    if (base / "requirements.txt").is_file():
        build_tools.append("pip")
    if (base / "requirements-dev.txt").is_file() or (base / "tests").is_dir():
        build_tools.append("pytest")
    if (base / "frontend" / "package.json").is_file():
        build_tools.append("npm")
    if (base / "Dockerfile").is_file():
        build_tools.append("docker")
    if (base / ".github" / "workflows").is_dir():
        build_tools.append("github-actions")

    return {
        "root": str(base),
        "languages": languages,
        "build_tools": build_tools,
        "dependency_signals": sorted(set(manifests)),
    }


def _entry_points(root: Path) -> list[str]:
    found: list[str] = []
    for relative in (
        "app/main.py",
        "tests/test_app.py",
        "mcp_server/server.py",
        "Dockerfile",
        "frontend/package.json",
    ):
        if (root / relative).is_file():
            found.append(relative)
    package_json = root / "frontend" / "package.json"
    if package_json.is_file():
        try:
            scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts") or {}
        except json.JSONDecodeError:
            scripts = {}
        if scripts:
            found.append("frontend scripts: " + ", ".join(sorted(scripts)))
    return found


def discover(root: str, answers: dict | None) -> tuple[dict, list[Question]]:
    base = Path(root)
    profile = scan_checkout(root)
    given = answers or {}
    questions = [question for question in POLICY_QUESTIONS if not str(given.get(question["id"], "")).strip()]
    dast_note = (
        "FastAPI exposes /status, so .github/workflows/dast.yml runs an HTTP smoke. "
        "ZAP is omitted: it is not installed and this sprint does not run an active scanner."
    )
    ecosystem = {
        "languages": profile["languages"],
        "manifests": profile["dependency_signals"],
        "entry_points": _entry_points(base),
        "build_tools": profile["build_tools"],
        "gaps": [question["reason"] for question in questions],
        "retrieval": (
            "Local filesystem scan of this checkout. "
            "graphify and OpenSearch are deferred and were not queried."
        ),
        "dast": dast_note,
        "scanners": {
            "sast": "Semgrep in scanners.yml; ruff runs locally and in build.yml.",
            "sca": "pip-audit via scripts/audit_gate.py, plus Trivy filesystem scan in CI.",
            "secrets": "Gitleaks in CI. Local scan redacts matches and skips .env.",
            "dast": dast_note,
        },
    }
    return ecosystem, questions
