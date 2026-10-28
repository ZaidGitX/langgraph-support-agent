#!/usr/bin/env python3
"""Sprint 3 preflight: interpreter, packages, CLIs, daemon, git, placeholders, hooks.

Stdlib only. Prints names and booleans, never environment values.

  python3 preflight.py            # full report
  python3 preflight.py --brief    # ~20 lines, used by the SessionStart hook
  python3 preflight.py --json     # machine-readable
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
ASSETS = SKILL_DIR / "assets"
PROJECT_PY = "/opt/anaconda3/envs/tcs_practice_env/bin/python"
IMPORT_CHECK = (
    "import langgraph, langchain_core, langchain_openai, langchain_typesafe, "
    "langgraph.checkpoint.sqlite, langchain_mcp_adapters, fastapi, pydantic, yaml, numpy, "
    "tiktoken, pytest, ruff; import importlib.metadata as m; "
    "print('langgraph', m.version('langgraph'), 'langchain-core', m.version('langchain-core'), "
    "'langchain-typesafe', m.version('langchain-typesafe'))"
)
CLIS = ("aws", "gh", "docker", "kubectl", "gcloud", "az", "terraform", "helm", "semgrep", "trivy",
        "gitleaks", "snyk", "grype", "syft", "hawk", "actionlint", "hadolint", "shellcheck", "k6",
        "cloudflared", "node", "npm", "uvx", "jq", "git", "ssh")
VERSION_FLAGS = {"aws": ["--version"], "gh": ["--version"], "docker": ["--version"], "node": ["--version"],
                 "npm": ["--version"], "uvx": ["--version"], "git": ["--version"], "trivy": ["--version"],
                 "semgrep": ["--version"], "gitleaks": ["version"], "hawk": ["--version"]}
SPRINT3_FILES = ("orchestrator/registry.py", "orchestrator/retrieval.py", "orchestrator/episodes.py",
                 "orchestrator/credentials.py", "orchestrator/redaction.py", "orchestrator/policy.py",
                 "orchestrator/providers/base.py", "orchestrator/generator.py",
                 "frontend/src/components/Readiness.jsx")


def find_root() -> Path:
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root and (Path(env_root) / "orchestrator").is_dir():
        return Path(env_root)
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "orchestrator" / "graph.py").is_file():
            return candidate
    return SKILL_DIR.parents[2]


def run(argv: list[str], timeout: float = 5, cwd: Path | None = None) -> tuple[int, str]:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                                   cwd=str(cwd) if cwd else None,
                                   env={**os.environ, "AWS_EC2_METADATA_DISABLED": "true"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, type(exc).__name__
    out = (completed.stdout or completed.stderr or "").strip().splitlines()
    return int(completed.returncode), (out[0] if out else "")


def registry() -> dict:
    try:
        return json.loads((ASSETS / "credentials.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"groups": {}}


def env_file_keys(root: Path) -> set[str]:
    keys: set[str] = set()
    path = root / ".env"
    if not path.is_file():
        return keys
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() and value.strip():
                keys.add(key.strip())
    except OSError:
        pass
    return keys


def check_python(brief: bool) -> dict:
    info: dict = {"path": PROJECT_PY, "exists": Path(PROJECT_PY).is_file()}
    if not info["exists"]:
        info["note"] = "project interpreter missing; conda env tcs_practice_env expected"
        return info
    code, line = run([PROJECT_PY, "--version"], timeout=5)
    info["version"] = line
    if not brief:
        code, line = run([PROJECT_PY, "-c", IMPORT_CHECK], timeout=25)
        info["imports_ok"] = code == 0
        info["imports"] = line
    return info


def check_clis(brief: bool) -> dict:
    found: dict = {}
    for name in CLIS:
        path = shutil.which(name)
        entry: dict = {"present": bool(path), "path": path or ""}
        if path and not brief and name in VERSION_FLAGS:
            code, line = run([name, *VERSION_FLAGS[name]], timeout=6)
            entry["version"] = line[:80]
        found[name] = entry
    return found


def check_docker() -> dict:
    if not shutil.which("docker"):
        return {"cli": False, "daemon": False}
    code, line = run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=6)
    return {"cli": True, "daemon": code == 0, "server": line if code == 0 else ""}


def check_git(root: Path) -> dict:
    code, branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    code2, sha = run(["git", "rev-parse", "--short", "HEAD"], cwd=root)
    code3, status = run(["git", "status", "--porcelain"], cwd=root)
    try:
        dirty = len([l for l in subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=5).stdout.splitlines() if l.strip()])
    except (OSError, subprocess.TimeoutExpired):
        dirty = -1
    return {"branch": branch if code == 0 else "", "sha": sha if code2 == 0 else "", "dirty_files": dirty}


def check_placeholders(root: Path) -> dict:
    reg = registry()
    file_keys = env_file_keys(root)
    result: dict = {}
    for key, group in reg.get("groups", {}).items():
        present, missing = [], []
        for item in group.get("env", []):
            name = item["name"]
            is_set = bool(os.environ.get(name, "").strip()) or name in file_keys
            if is_set:
                present.append(name)
            elif item.get("required"):
                missing.append(name)
        alternatives = group.get("alternatives") or []
        if alternatives:
            satisfied = any(all(n in present for n in alt) for alt in alternatives)
            alt_names = {n for alt in alternatives for n in alt}
            missing = [n for n in missing if n not in alt_names]
            if not satisfied:
                missing.append(" or ".join("+".join(alt) for alt in alternatives))
        cli = group.get("cli")
        result[key] = {
            "cli": (bool(shutil.which(cli)) if cli else None),
            "present": present,
            "missing_required": missing,
            "mode": "apply" if not missing and (cli is None or shutil.which(cli)) else "plan",
        }
    return result


def check_hooks(root: Path) -> dict:
    marker = "sprint3-agentic-cicd/hooks/"
    def has(path: Path) -> bool:
        try:
            return marker in path.read_text(encoding="utf-8")
        except OSError:
            return False
    return {
        "claude_project": has(root / ".claude" / "settings.local.json") or has(root / ".claude" / "settings.json"),
        "claude_user": has(Path.home() / ".claude" / "settings.json"),
        "cursor_project": has(root / ".cursor" / "hooks.json"),
        "cursor_user": has(Path.home() / ".cursor" / "hooks.json"),
        "skill_frontmatter": has(SKILL_DIR / "SKILL.md"),
    }


def check_sprint3(root: Path) -> dict:
    return {rel: (root / rel).is_file() for rel in SPRINT3_FILES}


def check_frontend(root: Path) -> dict:
    return {
        "node_modules": (root / "frontend" / "node_modules").is_dir(),
        "dist": (root / "frontend" / "dist" / "index.html").is_file(),
    }


def check_var(root: Path) -> dict:
    stamp = root / "var" / ".sprint3-checks.json"
    data = {}
    try:
        data = json.loads(stamp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {
        "sqlite": (root / "var" / "orchestrator.sqlite").is_file(),
        "last_checks_ok": data.get("ok"),
        "last_checks_at": data.get("timestamp_iso", ""),
    }


def collect(root: Path, brief: bool) -> dict:
    return {
        "root": str(root),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "python": check_python(brief),
        "clis": check_clis(brief),
        "docker": check_docker(),
        "git": check_git(root),
        "placeholders": check_placeholders(root),
        "hooks": check_hooks(root),
        "sprint3_files": check_sprint3(root),
        "frontend": check_frontend(root),
        "var": check_var(root),
    }


def render_brief(data: dict) -> str:
    py = data["python"]
    clis = data["clis"]
    present = [n for n, e in clis.items() if e["present"]]
    absent = [n for n, e in clis.items() if not e["present"]]
    ph = data["placeholders"]
    lines = [
        f"Sprint 3 preflight for {data['root']}",
        f"- python: {py['path']} ({py.get('version', 'missing')})" + ("" if py["exists"] else "  <-- MISSING"),
        f"- git: {data['git']['branch']}@{data['git']['sha']} dirty={data['git']['dirty_files']}",
        f"- docker daemon: {'up' if data['docker'].get('daemon') else 'down or no access'}",
        f"- CLIs present: {', '.join(present)}",
        f"- CLIs absent: {', '.join(absent)}",
    ]
    for key in ("app", "github", "aws", "gcp", "azure", "jenkins", "kubernetes", "ssh"):
        entry = ph.get(key)
        if not entry:
            continue
        cli = entry["cli"]
        cli_text = "" if cli is None else (" cli=yes" if cli else " cli=no")
        miss = ", ".join(entry["missing_required"]) or "none"
        lines.append(f"- {key}:{cli_text} mode={entry['mode']} present={len(entry['present'])} missing={miss}")
    hooks = data["hooks"]
    installed = [k for k, v in hooks.items() if v]
    lines.append(f"- hooks installed: {', '.join(installed) or 'none'} (install: python3 .claude/skills/sprint3-agentic-cicd/scripts/install_hooks.py)")
    done = [k for k, v in data["sprint3_files"].items() if v]
    lines.append(f"- sprint 3 modules present: {len(done)}/{len(data['sprint3_files'])}")
    var = data["var"]
    lines.append(f"- last checks: ok={var['last_checks_ok']} at={var['last_checks_at'] or 'never'}")
    lines.append(f"- frontend: node_modules={data['frontend']['node_modules']} dist={data['frontend']['dist']}")
    return "\n".join(lines)


def render_full(data: dict) -> str:
    out = [render_brief(data), "", "Packages:", f"  {data['python'].get('imports', 'not checked')}  ok={data['python'].get('imports_ok')}", "", "CLI versions:"]
    for name, entry in data["clis"].items():
        if entry["present"]:
            out.append(f"  {name:12} {entry.get('version', '')}  {entry['path']}")
    out.append("")
    out.append("Placeholders (names only):")
    for key, entry in data["placeholders"].items():
        out.append(f"  {key:12} mode={entry['mode']:5} present={entry['present']} missing={entry['missing_required']}")
    out.append("")
    out.append("Sprint 3 modules:")
    for rel, ok in data["sprint3_files"].items():
        out.append(f"  [{'x' if ok else ' '}] {rel}")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--brief", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    root = find_root()
    data = collect(root, brief=args.brief)
    if args.json:
        print(json.dumps(data, indent=2))
    elif args.brief:
        print(render_brief(data))
    else:
        print(render_full(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
