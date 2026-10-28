#!/usr/bin/env python3
"""Infer the current Sprint 3 phase from the files and markers in the checkout.

  python3 sprint3_progress.py          # table
  python3 sprint3_progress.py --brief  # one line per phase + next action
  python3 sprint3_progress.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]

# (number, title, required files, (file, substring) markers, reference)
PHASES = [
    (0, "Preflight and baseline", [], [], "12-phase-plan.md"),
    (1, "Registry and schema v2", ["orchestrator/registry.py", "tests/test_registry.py"],
     [("orchestrator/schemas.py", "GeneratedArtifact"), ("orchestrator/state.py", "MAX_CLARIFY_ROUNDS")], "01-state-schema-and-registry.md"),
    (2, "Retrieval and episodes", ["orchestrator/retrieval.py", "orchestrator/episodes.py", "tests/test_retrieval.py", "tests/test_episodes.py"],
     [("orchestrator/graph.py", "retrieval")], "05-retrieval-and-episodes.md"),
    (3, "Credentials, redaction, policy", ["orchestrator/credentials.py", "orchestrator/credentials.json", "orchestrator/redaction.py", "orchestrator/policy.py", "tests/test_credentials.py", "tests/test_policy.py"],
     [("app/orchestrator_api.py", "/readiness")], "07-credentials-env-mcp.md"),
    (4, "Providers and runners", ["orchestrator/providers/__init__.py", "orchestrator/providers/base.py", "orchestrator/providers/registry.py", "orchestrator/providers/local.py", "orchestrator/providers/docker.py", "orchestrator/providers/github_actions.py", "orchestrator/providers/jenkins.py", "orchestrator/providers/aws.py", "orchestrator/providers/gcp.py", "orchestrator/providers/azure.py", "orchestrator/providers/ssh.py", "orchestrator/providers/kubernetes.py", "orchestrator/scanners.json", "tests/test_providers.py"],
     [("orchestrator/dispatch.py", "execute_stage_v2")], "06-providers-runners.md"),
    (5, "Generation, Jev edges, graph nodes", ["orchestrator/generator.py", "tests/test_generator.py"],
     [("orchestrator/graph.py", "generated_stage"), ("orchestrator/graph.py", "follow_up"), ("orchestrator/llm.py", "generate_artifacts"), ("orchestrator/llm.py", "artifact_review"), ("orchestrator/session.py", "follow_up_qa")], "02-graph-and-session.md"),
    (6, "UI and API", ["frontend/src/components/Readiness.jsx", "frontend/src/components/Candidates.jsx", "frontend/src/components/ArtifactViewer.jsx", "frontend/src/components/RetrievalPanel.jsx", "frontend/src/components/Episodes.jsx"],
     [("app/orchestrator_api.py", "/session/artifacts"), ("frontend/src/App.css", ".candidate-card")], "09-ui-and-api.md"),
    (7, "Demo rehearsal and docs", [], [("docs/ORCHESTRATOR_RUNBOOK.md", "Sprint 3"), ("docs/SYSTEM_MAP.md", "registry.py")], "10-testing-and-demo.md"),
]


def find_root() -> Path:
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root and (Path(env_root) / "orchestrator").is_dir():
        return Path(env_root)
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "orchestrator" / "graph.py").is_file():
            return candidate
    return SKILL_DIR.parents[2]


def _has(root: Path, rel: str, needle: str) -> bool:
    try:
        return needle in (root / rel).read_text(encoding="utf-8")
    except OSError:
        return False


def assess(root: Path) -> dict:
    phases = []
    stamp = {}
    try:
        stamp = json.loads((root / "var" / ".sprint3-checks.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    current = None
    for number, title, files, markers, ref in PHASES:
        missing_files = [f for f in files if not (root / f).is_file()]
        missing_markers = [f"{f}: {s}" for f, s in markers if not _has(root, f, s)]
        total = len(files) + len(markers)
        done = total - len(missing_files) - len(missing_markers)
        if number == 0:
            status = "done" if stamp.get("ok") else ("partial" if stamp else "pending")
        elif total == 0:
            status = "pending"
        elif done == total:
            status = "done"
        elif done > 0:
            status = "partial"
        else:
            status = "pending"
        if current is None and status != "done":
            current = number
        phases.append({"phase": number, "title": title, "status": status, "done": done, "total": total,
                       "missing": missing_files + missing_markers, "reference": ref})
    return {"root": str(root), "current_phase": current if current is not None else 7, "phases": phases,
            "last_checks": {"ok": stamp.get("ok"), "at": stamp.get("timestamp_iso", ""), "failed": stamp.get("failed", [])}}


def render(data: dict, brief: bool) -> str:
    lines = []
    for p in data["phases"]:
        mark = {"done": "x", "partial": "~", "pending": " "}[p["status"]]
        lines.append(f"[{mark}] phase {p['phase']}: {p['title']} ({p['done']}/{p['total']})")
        if not brief and p["missing"]:
            for item in p["missing"][:12]:
                lines.append(f"      missing: {item}")
    cur = data["current_phase"]
    ref = next(p["reference"] for p in data["phases"] if p["phase"] == cur)
    checks = data["last_checks"]
    lines.append(f"current phase: {cur} -> read references/{ref}; last checks ok={checks['ok']} at={checks['at'] or 'never'}"
                 + (f" failed={checks['failed']}" if checks.get("failed") else ""))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--brief", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    data = assess(find_root())
    print(json.dumps(data, indent=2) if args.json else render(data, args.brief))
    return 0


if __name__ == "__main__":
    sys.exit(main())
