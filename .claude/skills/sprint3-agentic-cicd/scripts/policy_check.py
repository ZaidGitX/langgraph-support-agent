#!/usr/bin/env python3
"""Static policy check for generated CI/CD artifacts (workflows, scripts, Dockerfiles, docs).

  python3 policy_check.py .github/workflows var/generated ci deploy
  python3 policy_check.py --json path/to/file.sh
  python3 policy_check.py --kind github_workflow some.yml

Exit 1 when any error-severity finding exists (warnings alone exit 0). Uses
scripts/policy_rules.py, the same rules the hooks and orchestrator/policy.py apply.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from policy_rules import check_path, check_text, kind_for_path  # noqa: E402

HAND_WRITTEN_WORKFLOW_RULES = ("secret.", "exfil.", "pin.", "perms.")

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", ".pytest_cache", ".ruff_cache"}
CHECKED_SUFFIXES = (".sh", ".yml", ".yaml", ".tpl", ".md", ".env", ".example")
CHECKED_NAMES = ("Jenkinsfile", "Dockerfile")


def iter_files(target: Path):
    if target.is_file():
        yield target
        return
    if not target.is_dir():
        return
    for path in sorted(target.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        name = path.name
        if name.endswith(CHECKED_SUFFIXES) or name.startswith(CHECKED_NAMES) or name.startswith(".env"):
            yield path


def relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", default=[".github/workflows", "var/generated", "ci", "deploy", "docker"])
    parser.add_argument("--kind", default="", help="force the artifact kind for every file")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--warn-only", action="store_true", help="exit 0 even on errors")
    parser.add_argument("--root", default=".", help="repo root for relative paths and path rules")
    args = parser.parse_args()
    root = Path(args.root).resolve()

    results: list[dict] = []
    checked = 0
    for raw in args.paths:
        target = Path(raw)
        if not target.is_absolute():
            target = root / raw
        for path in iter_files(target):
            rel = relative(path, root)
            name = path.name
            kind = args.kind or kind_for_path(rel.replace(".tpl", ""))
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            checked += 1
            findings = check_text(text, kind=kind, path=rel)
            if rel.startswith(".github/workflows/") and not name.startswith("gen-"):
                # Hand-written workflows are pinned by tests/test_pipeline_rules.py; here only
                # secret, exfiltration, pinning, and permissions rules apply to them.
                findings = [f for f in findings if f["rule"].startswith(HAND_WRITTEN_WORKFLOW_RULES)]
            if rel.startswith(".github/workflows/gen-") or rel.startswith(("ci/", "deploy/", "docker/", "var/generated/")):
                clean = rel
                if rel.startswith("var/generated/"):
                    parts = rel.split("/", 4)
                    clean = parts[4] if len(parts) > 4 else rel
                findings.extend(check_path(clean))
            results.extend(findings)

    errors = [f for f in results if f["severity"] == "error"]
    warns = [f for f in results if f["severity"] == "warn"]
    if args.json:
        print(json.dumps({"checked": checked, "errors": len(errors), "warnings": len(warns), "findings": results}, indent=2))
    else:
        for f in results:
            print(f"{f['severity'].upper():5} {f['path']}:{f['line']} {f['rule']}: {f['message']}")
        print(f"policy: {checked} file(s) checked, {len(errors)} error(s), {len(warns)} warning(s)")
    if errors and not args.warn_only:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
