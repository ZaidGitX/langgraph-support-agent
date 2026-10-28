"""Deterministic local operations shared with the GitHub Actions workflows.

The shell does how. These commands write plans and evidence. They do not
call AWS, AgentCore, OpenSearch, ECR, ECS, or S3.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from . import redaction
from .state import ALLOWED_TARGETS, FORBIDDEN_TARGETS

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
}
SKIP_FILES = {".env", ".env.local"}


def _emit(payload: dict, code: int) -> int:
    print(json.dumps(payload, indent=2))
    return code


def _ensure_dirs(root: Path) -> Path:
    dist = root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (root / "var").mkdir(parents=True, exist_ok=True)
    return dist


def package(root: Path) -> int:
    dist = _ensure_dirs(root)
    archive = dist / "langgraph-support-app.tgz"
    inputs = [
        "app",
        "mcp_server",
        "orchestrator",
        "resources",
        "requirements.txt",
        "requirements-dev.txt",
        "Dockerfile",
    ]
    present = [item for item in inputs if (root / item).exists()]
    if archive.exists():
        archive.unlink()
    subprocess.run(
        ["tar", "-czf", str(archive), "--exclude", "__pycache__", "--exclude", "*.pyc", *present],
        cwd=root,
        check=True,
    )
    return _emit(
        {
            "action": "package",
            "artifact": str(archive.relative_to(root)),
            "bytes": archive.stat().st_size,
            "includes": present,
            "s3": "not called",
            "past_uat": False,
        },
        0,
    )


def container(root: Path) -> int:
    dist = _ensure_dirs(root)
    dockerfile = root / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8") if dockerfile.is_file() else ""
    ok = "FROM " in text and "CMD " in text
    plan = {
        "action": "container",
        "dockerfile": dockerfile.name if dockerfile.is_file() else "missing",
        "has_from": "FROM " in text,
        "has_cmd": "CMD " in text,
        "local_action": "validated Dockerfile only",
        "ci_action": "docker build in the support image job; not run in this in-process demo",
        "sbom": "deferred",
        "registry_push": False,
        "past_uat": False,
    }
    (dist / "container-plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return _emit(plan, 0 if ok else 1)


def release(root: Path) -> int:
    dist = _ensure_dirs(root)
    plan = {
        "action": "release",
        "tag": "local-demo",
        "sign": "deferred",
        "ecr": "not called",
        "past_uat": False,
    }
    (dist / "release-plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return _emit(plan, 0)


def deploy(root: Path, target: str) -> int:
    dist = _ensure_dirs(root)
    normalized = target.strip().lower()
    refused = normalized in FORBIDDEN_TARGETS
    allowed = normalized in ALLOWED_TARGETS
    plan = {
        "action": "deploy",
        "target": normalized,
        "allowed": allowed and not refused,
        "refused": refused or not allowed,
        "reason": (
            "No path past UAT. Production is refused."
            if refused
            else ("Target is not in local, dev, staging, uat." if not allowed else "Gated plan only.")
        ),
        "ecs": "not called",
        "eks": "not called",
        "agentcore": "not called",
        "aws_called": False,
        "past_uat": False,
    }
    (dist / "deploy-plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return _emit(plan, 2 if plan["refused"] else 0)


def uat(root: Path, target: str) -> int:
    dist = _ensure_dirs(root)
    plan = {
        "action": "uat",
        "target": target.strip().lower(),
        "checklist": [
            "build evidence attached",
            "tests recorded",
            "human sign-off still required in the graph interrupt",
            "no production promotion exists",
        ],
        "aws_called": False,
        "past_uat": False,
    }
    (dist / "uat-checklist.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return _emit(plan, 0)


def secrets(root: Path) -> int:
    findings: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name in SKIP_FILES or path.suffix in {".png", ".jpg", ".whl", ".tgz", ".sqlite"}:
            continue
        if path.stat().st_size > 1_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if redaction.file_has_secret(text):
            findings.append(path.relative_to(root).as_posix())
    return _emit(
        {
            "action": "secrets",
            "tool": "local-redacted-scan",
            "gitleaks": "used in CI when this binary is absent",
            "findings": findings,
            "count": len(findings),
            "patterns": len(redaction.FILE_SCAN_PATTERNS),
            "note": "Match text is not printed. .env is not scanned.",
        },
        1 if findings else 0,
    )


def skip_tool(tool: str, ci: str) -> int:
    return _emit(
        {
            "action": "skip-tool",
            "tool": tool,
            "status": "skipped",
            "reason": f"{tool} is not on PATH in this local demo",
            "ci": ci,
            "past_uat": False,
        },
        0,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local CI/CD operations. No AWS calls.")
    parser.add_argument(
        "action",
        choices=["package", "container", "release", "deploy", "uat", "secrets", "skip-tool"],
    )
    parser.add_argument("--target", default="local")
    parser.add_argument("--root", default=".")
    parser.add_argument("--tool", default="")
    parser.add_argument("--ci", default="")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if args.action == "package":
        return package(root)
    if args.action == "container":
        return container(root)
    if args.action == "release":
        return release(root)
    if args.action == "deploy":
        return deploy(root, args.target)
    if args.action == "uat":
        return uat(root, args.target)
    if args.action == "secrets":
        return secrets(root)
    return skip_tool(args.tool or "tool", args.ci or "workflow")


if __name__ == "__main__":
    raise SystemExit(main())
