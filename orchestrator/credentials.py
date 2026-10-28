"""Credential placeholders and environment readiness (Sprint 3, phase 3).

``credentials.json`` (a copy of the skill asset) is the single registry of every environment
variable a provider, scanner, or the app may need. This module only ever reports names and
booleans: whether a name is set, which CLI is on PATH, and whether a group could run now
(``apply``) or must plan. No value is read into state, logs, prompts, or files.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Mapping
from pathlib import Path

from app import config

from . import registry as stage_registry

REGISTRY_PATH = Path(__file__).with_name("credentials.json")
APP_GROUPS = ("app", "orchestrator")
LOCAL_RUNNERS = ("local", "docker")
# Scanner CLIs the readiness panel reports until the catalog (phase 4) takes over.
SCANNER_CLIS: dict[str, tuple[str, tuple[str, ...]]] = {
    "semgrep": ("semgrep", ()),
    "trivy": ("trivy", ()),
    "gitleaks": ("gitleaks", ()),
    "grype": ("grype", ()),
    "syft": ("syft", ()),
    "snyk": ("snyk", ("SNYK_TOKEN",)),
    "stackhawk": ("hawk", ("HAWK_API_KEY", "HAWK_APP_ID")),
    "sonar": ("sonar-scanner", ("SONAR_TOKEN", "SONAR_ORGANIZATION", "SONAR_PROJECT_KEY")),
}
TOGGLES = (
    ("ORCHESTRATOR_DISPATCH", "local"),
    ("ORCHESTRATOR_CLOUD_APPLY", "0"),
    ("ORCHESTRATOR_WRITE_ARTIFACTS", "0"),
    ("ORCHESTRATOR_EMBEDDINGS", "bm25"),
    ("ORCHESTRATOR_MCP", "0"),
    ("ORCHESTRATOR_ASTRA_TEMPERATURE", "0.7"),
)

_REGISTRY: dict | None = None


def registry(*, reload: bool = False) -> dict:
    global _REGISTRY
    if _REGISTRY is None or reload:
        _REGISTRY = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return _REGISTRY


def groups() -> dict[str, dict]:
    return dict(registry().get("groups") or {})


def _items() -> list[tuple[str, dict]]:
    return [(key, item) for key, group in groups().items() for item in group.get("env") or []]


def all_names() -> list[str]:
    return sorted({item["name"] for _key, item in _items()})


def kind_of(name: str) -> str:
    for _key, item in _items():
        if item["name"] == name:
            return str(item.get("kind") or "config")
    return "config"


def secret_names() -> list[str]:
    return sorted({item["name"] for _key, item in _items() if item.get("kind") == "secret"})


def github_secret_names() -> list[str]:
    return sorted({item["name"] for _key, item in _items() if item.get("github_secret")})


def placeholders_for(key: str) -> list[str]:
    """Every name a group may reference (required and optional), in registry order."""
    group = groups().get(key) or {}
    return [item["name"] for item in group.get("env") or []]


def names_for(key: str) -> list[str]:
    """Names a group needs to run: required items plus every name in an alternative set."""
    group = groups().get(key) or {}
    required = [item["name"] for item in group.get("env") or [] if item.get("required")]
    for alternative in group.get("alternatives") or []:
        for name in alternative:
            if name not in required:
                required.append(name)
    return required


def group_for_runner(runner: str) -> str | None:
    for key, group in groups().items():
        if runner in (group.get("runners") or []):
            return key
    return None


def _value(name: str, env: Mapping[str, str] | None) -> str:
    source = env if env is not None else os.environ
    return str(source.get(name, "") or "").strip()


def is_present(name: str, env: Mapping[str, str] | None = None) -> bool:
    """True when the variable has a non-empty value. The value itself is never returned."""
    if env is None:
        config.load_env_files()
    if kind_of(name) == "path":
        value = _value(name, env)
        return bool(value) and (Path(value).expanduser().exists() or name == "KUBECONFIG")
    return bool(_value(name, env))


def split_present(key: str, env: Mapping[str, str] | None = None) -> tuple[list[str], list[str]]:
    """(present, missing) names for a group, honouring alternative sets.

    ``missing`` lists required names that are absent; when no alternative set is complete it
    also lists the absent names of the first (primary) alternative.
    """
    group = groups().get(key) or {}
    alternatives = [list(alt) for alt in group.get("alternatives") or []]
    alternative_names = {name for alt in alternatives for name in alt}
    present = [item["name"] for item in group.get("env") or [] if is_present(item["name"], env)]
    missing = [
        item["name"]
        for item in group.get("env") or []
        if item.get("required") and item["name"] not in alternative_names and not is_present(item["name"], env)
    ]
    if alternatives and not any(all(is_present(name, env) for name in alt) for alt in alternatives):
        missing.extend(name for name in alternatives[0] if not is_present(name, env) and name not in missing)
    if key == "kubernetes" and "KUBECONFIG" in missing and (Path.home() / ".kube" / "config").is_file():
        missing.remove("KUBECONFIG")
    return present, missing


def cli_path(key: str) -> str:
    cli = (groups().get(key) or {}).get("cli")
    return shutil.which(cli) or "" if cli else ""


def cloud_apply(env: Mapping[str, str] | None = None) -> bool:
    return _value("ORCHESTRATOR_CLOUD_APPLY", env) == "1"


def group_status(key: str, env: Mapping[str, str] | None = None) -> dict:
    group = groups().get(key) or {}
    cli = group.get("cli")
    path = cli_path(key)
    present, missing = split_present(key, env)
    ready = (not cli or bool(path)) and not missing
    return {
        "label": group.get("label") or key,
        "cli": (bool(path) if cli else None),
        "cli_name": cli or "",
        "cli_path": path,
        "present": present,
        "missing": missing,
        "alternatives": [list(alt) for alt in group.get("alternatives") or []],
        "mode": "apply" if ready else "plan",
        "runners": list(group.get("runners") or []),
    }


def _runner_modes(group_statuses: dict[str, dict], env: Mapping[str, str] | None) -> dict[str, dict]:
    runners: dict[str, dict] = {}
    allow_cloud = cloud_apply(env)
    dispatch = _value("ORCHESTRATOR_DISPATCH", env).lower() or "local"
    for runner in stage_registry.RUNNERS:
        key = group_for_runner(runner)
        status = group_statuses.get(key or "", {})
        ready = runner == "local" or (bool(status) and status.get("mode") == "apply")
        if runner in LOCAL_RUNNERS:
            mode = "apply" if ready else "plan"
        elif runner == "github_actions":
            mode = "apply" if ready and (allow_cloud or dispatch == "github") else "plan"
        else:
            mode = "apply" if ready and allow_cloud else "plan"
        runners[runner] = {
            "group": key or "",
            "cli": True if runner == "local" else bool(status.get("cli", True)) if status else False,
            "missing": list(status.get("missing") or []) if status else [],
            "mode": mode,
        }
    return runners


def _scanner_status(env: Mapping[str, str] | None) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for tool, (cli, names) in SCANNER_CLIS.items():
        result[tool] = {
            "cli": bool(shutil.which(cli)),
            "env_missing": [name for name in names if not is_present(name, env)],
        }
    return result


def readiness(provider_statuses: Mapping[str, Mapping] | None = None, env: Mapping[str, str] | None = None) -> dict:
    """Names and booleans only: which CLIs and credential names are present, and what can apply.

    ``provider_statuses`` (phase 4) may override a runner's ``cli``/``missing``/``mode`` with
    what the provider itself detected, for example the Docker daemon being unreachable.
    """
    if env is None:
        config.load_env_files()
    statuses = {key: group_status(key, env) for key in groups() if key not in APP_GROUPS}
    app_present, app_missing = split_present("app", env)
    runners = _runner_modes(statuses, env)
    for runner, detected in (provider_statuses or {}).items():
        if runner in runners and isinstance(detected, Mapping):
            runners[runner].update({k: detected[k] for k in ("cli", "missing", "mode") if k in detected})
            runners[runner]["missing"] = list(runners[runner]["missing"])
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "app": {"present": app_present, "missing": app_missing},
        "providers": statuses,
        "runners": runners,
        "scanners": _scanner_status(env),
        "toggles": {
            name: (_value(name, env) or default)
            for name, default in TOGGLES
        }
        | {"cloud_apply": cloud_apply(env)},
    }


def render_env_example() -> str:
    """Deterministic ``.env.example`` text: every registry name, values empty or the default."""
    lines = [
        "# Placeholders for langgraph_support_app. Copy to .env and fill in locally; set the same",
        "# NAMES as GitHub secrets or CI variables for hosted runs. Never commit a value. Generated",
        "# from orchestrator/credentials.json by orchestrator.credentials.render_env_example().",
    ]
    for key, group in groups().items():
        lines.append("")
        lines.append(f"# {group.get('label') or key}")
        for item in group.get("env") or []:
            default = item.get("default") if item.get("kind") != "secret" else None
            lines.append(f"{item['name']}={default if default is not None else ''}")
    return "\n".join(lines) + "\n"


def env_example_names(text: str) -> list[str]:
    names = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        names.append(line.split("=", 1)[0].strip())
    return sorted(set(names))
