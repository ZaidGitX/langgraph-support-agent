#!/usr/bin/env python3
"""Validate GitHub Actions workflow files for this repository's rules.

  python3 validate_workflow.py .github/workflows
  python3 validate_workflow.py --json .github/workflows/gen-container_scan.yml

Rules: parses as YAML; every workflow except orchestrator.yml triggers only on
workflow_dispatch; orchestrator.yml triggers on push with `actions: write`; jobs exist with
runs-on and steps; every `uses:` is pinned; a top-level permissions block exists; secrets
referenced are in the credential registry. Runs actionlint when it is on PATH.
Parsing uses PyYAML from the current interpreter, else the project interpreter, else a
regex fallback.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from policy_rules import ALL_ENV_NAMES  # noqa: E402

PROJECT_PY = "/opt/anaconda3/envs/tcs_practice_env/bin/python"


def load_yaml(path: Path) -> tuple[dict | None, str]:
    try:
        import yaml  # type: ignore

        return yaml.safe_load(path.read_text(encoding="utf-8")), "pyyaml"
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 - report the parse error
        return None, f"yaml error: {exc}"
    if Path(PROJECT_PY).is_file():
        code = "import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1], encoding='utf-8'))))"
        try:
            completed = subprocess.run([PROJECT_PY, "-c", code, str(path)], capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, f"project python unavailable: {exc}"
        if completed.returncode == 0:
            try:
                return json.loads(completed.stdout), "project-pyyaml"
            except json.JSONDecodeError:
                return None, "could not decode parsed yaml"
        return None, f"yaml error: {completed.stderr.strip()[:200]}"
    return None, "no yaml parser"


def triggers_of(doc: dict) -> list[str]:
    on = doc.get("on", doc.get(True))  # PyYAML parses a bare `on:` key as True
    if on is None:
        return []
    if isinstance(on, str):
        return [on]
    if isinstance(on, list):
        return [str(x) for x in on]
    if isinstance(on, dict):
        return [str(k) for k in on]
    return []


def validate(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    errors: list[str] = []
    warnings: list[str] = []
    doc, parser = load_yaml(path)
    name = path.name
    if doc is None:
        if parser.startswith("yaml error"):
            errors.append(parser)
            return {"file": str(path), "errors": errors, "warnings": warnings}
        warnings.append(f"regex fallback ({parser})")
        triggers = re.findall(r"(?m)^\s{0,4}(push|pull_request|schedule|workflow_dispatch|workflow_run)\s*:", text)
        has_jobs = "jobs:" in text
        has_perms = re.search(r"(?m)^permissions:", text) is not None
    else:
        triggers = triggers_of(doc)
        has_jobs = isinstance(doc.get("jobs"), dict) and bool(doc.get("jobs"))
        has_perms = "permissions" in doc
        for job_name, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict):
                errors.append(f"job {job_name} is not a mapping")
                continue
            if "runs-on" not in job and "uses" not in job:
                errors.append(f"job {job_name} has no runs-on")
            if "steps" not in job and "uses" not in job:
                errors.append(f"job {job_name} has no steps")
    if name == "orchestrator.yml":
        if "push" not in triggers:
            errors.append("orchestrator.yml must trigger on push")
        if "pull_request" in triggers:
            errors.append("orchestrator.yml must not trigger on pull_request")
        if not re.search(r"actions:\s*write", text):
            errors.append("orchestrator.yml needs permissions.actions: write to dispatch stage workflows")
    else:
        extra = [t for t in triggers if t != "workflow_dispatch"]
        if extra:
            errors.append(f"only workflow_dispatch is allowed; found {extra}")
        if "workflow_dispatch" not in triggers:
            errors.append("workflow_dispatch trigger is missing")
    if not has_jobs:
        errors.append("no jobs")
    if not has_perms:
        warnings.append("no top-level permissions block")
    for match in re.finditer(r"(?m)^\s*-?\s*uses:\s*([^\s#]+)", text):
        ref = match.group(1)
        if ref.startswith(("./", "docker://")):
            continue
        if "@" not in ref:
            errors.append(f"unpinned action {ref}")
        elif re.search(r"@(main|master|latest)$", ref):
            warnings.append(f"floating ref {ref}")
    for match in re.finditer(r"\$\{\{\s*secrets\.([A-Z0-9_]+)\s*\}\}", text):
        secret = match.group(1)
        if ALL_ENV_NAMES and secret not in ALL_ENV_NAMES and secret != "GITHUB_TOKEN":
            warnings.append(f"secrets.{secret} is not in the credential registry")
    if shutil.which("actionlint"):
        try:
            completed = subprocess.run(["actionlint", "-no-color", str(path)], capture_output=True, text=True, timeout=30, check=False)
            if completed.returncode != 0:
                errors.append("actionlint: " + (completed.stdout or completed.stderr).strip()[:600])
        except (OSError, subprocess.TimeoutExpired):
            warnings.append("actionlint could not run")
    return {"file": str(path), "parser": parser, "triggers": triggers, "errors": errors, "warnings": warnings}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", default=[".github/workflows"])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    files: list[Path] = []
    for raw in args.paths:
        target = Path(raw)
        if target.is_dir():
            files.extend(sorted(p for p in target.iterdir() if p.suffix in {".yml", ".yaml"}))
        elif target.is_file():
            files.append(target)
    results = [validate(p) for p in files]
    total_errors = sum(len(r["errors"]) for r in results)
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            status = "ok" if not r["errors"] else "ERROR"
            print(f"{status:5} {r['file']} triggers={r.get('triggers')}")
            for e in r["errors"]:
                print(f"      error: {e}")
            for w in r["warnings"]:
                print(f"      warn:  {w}")
        print(f"workflows: {len(results)} checked, {total_errors} error(s)")
    return 1 if total_errors else 0


if __name__ == "__main__":
    sys.exit(main())
