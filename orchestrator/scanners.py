"""Scanner and test-tool catalog (Sprint 3, phase 4).

``scanners.json`` (a copy of the skill asset) describes every tool a scan or test stage may
use: CLI name, Docker image, install hint, argv templates per stage, the CI action that runs
it on GitHub, the credential names it reads, and how to extract findings from its output.
Tools are data; adding one is a catalog entry, not a code path.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name("scanners.json")
_CATALOG: dict | None = None


def catalog(*, reload: bool = False) -> dict[str, dict]:
    global _CATALOG
    if _CATALOG is None or reload:
        _CATALOG = dict(json.loads(CATALOG_PATH.read_text(encoding="utf-8")).get("tools") or {})
    return _CATALOG


def entry(key: str) -> dict | None:
    return catalog().get(str(key or "").strip().lower())


def keys() -> list[str]:
    return sorted(catalog())


def tools_for_stage(stage: str) -> list[str]:
    """Catalog keys whose ``stages`` list names this stage, in catalog order."""
    return [key for key, item in catalog().items() if stage in (item.get("stages") or [])]


def aliases() -> dict[str, str]:
    """Lower-case alias -> catalog key, for matching GitHub step names and labels."""
    mapping: dict[str, str] = {}
    for key, item in catalog().items():
        for alias in item.get("aliases") or [key]:
            mapping[str(alias).lower()] = key
    return mapping


def cli_present(key: str) -> bool:
    item = entry(key) or {}
    cli = item.get("cli")
    return bool(cli) and shutil.which(cli) is not None


def docker_image(key: str) -> str:
    return str((entry(key) or {}).get("docker_image") or "")


def env_names(key: str) -> list[str]:
    return [str(name) for name in (entry(key) or {}).get("env") or []]


# --- Selection from the person's answers (Sprint 4) -------------------------------------------
# The workflows' providers are a menu: `scan_tools` and `dast_tools` (or the profile) say which
# run; `scan_policy` says the severity that fails a run. The orchestrator's contract scripts honor
# the selection (SCAN_TOOLS, DAST_TOOLS, SCAN_SEVERITY) and Astra's generated scan stages take
# their invocations from the catalog. Unanswered means every catalog tool of that stage, with a
# tool whose credential is not provided recorded as skipped.
SCAN_STAGE_TOOLS = ("semgrep", "pip_audit", "snyk", "trivy", "gitleaks", "sonar", "codeql")
DAST_STAGE_TOOLS = ("zap", "stackhawk")
_ALL_WORDS = {"all", "every", "everything", "default", "defaults", "full", "yes"}
_NONE_WORDS = {"none", "no", "skip", "nothing"}


def _resolve(text: str, allowed: tuple[str, ...]) -> list[str] | None:
    """Catalog keys named in free text, in catalog order; None for "all"/empty, [] for "none"."""
    lowered = " " + re.sub(r"[^a-z0-9_ -]+", " ", str(text or "").lower()) + " "
    words = set(lowered.split())
    if not words or words & _ALL_WORDS and not any(key in lowered for key in allowed):
        return None
    if words <= _NONE_WORDS:
        return []
    found: list[str] = []
    alias_map = aliases()
    for key in allowed:
        names = {key, key.replace("_", "-"), *[alias for alias, target in alias_map.items() if target == key]}
        if any(f" {name} " in lowered or f" {name}," in lowered for name in names):
            found.append(key)
    return found or None


def severity_from(text: str) -> str:
    """The severities that fail a run, from the scan_policy answer (default HIGH,CRITICAL)."""
    lowered = str(text or "").lower()
    if "medium" in lowered or "moderate" in lowered:
        return "MEDIUM,HIGH,CRITICAL"
    if "critical" in lowered and "high" not in lowered:
        return "CRITICAL"
    if "low" in lowered and "high" not in lowered and "critical" not in lowered:
        return "LOW,MEDIUM,HIGH,CRITICAL"
    return "HIGH,CRITICAL"


def selection_from_answers(answers: dict | None) -> dict:
    """``{"scan_tools": [...], "dast_tools": [...], "severity": "HIGH,CRITICAL"}``.

    Lists hold catalog keys (``pip_audit`` style); an unanswered or "all" answer is the stage's
    whole catalog list; "none" is an empty list.
    """
    answers = answers or {}
    scan = _resolve(str(answers.get("scan_tools") or ""), SCAN_STAGE_TOOLS)
    dast = _resolve(str(answers.get("dast_tools") or ""), DAST_STAGE_TOOLS)
    return {
        "scan_tools": list(SCAN_STAGE_TOOLS) if scan is None else scan,
        "dast_tools": list(DAST_STAGE_TOOLS) if dast is None else dast,
        "severity": severity_from(str(answers.get("scan_policy") or "")),
    }


def selection_env(answers: dict | None) -> dict[str, str]:
    """The three environment values a stage script reads for the selection."""
    chosen = selection_from_answers(answers)
    return {
        "SCAN_TOOLS": ",".join(key.replace("_", "-") for key in chosen["scan_tools"]) or "none",
        "DAST_TOOLS": ",".join(chosen["dast_tools"]) or "none",
        "SCAN_SEVERITY": chosen["severity"],
    }


def catalog_for_prompt(stage_names: tuple[str, ...] = ("scanners", "dast", "sast", "sca", "secrets_scan", "container_scan", "iac_scan", "sbom", "dast_scan", "test", "unit_test", "integration_test", "perf_test", "contract_test")) -> list[dict]:
    """What Astra needs to write a scan stage for any provider: key, aliases, image, env names,
    argv per stage, how to install it, and when to skip it. Names only, never a value."""
    rows: list[dict] = []
    for key, item in catalog().items():
        argv = {stage: list(lines) for stage, lines in (item.get("argv") or {}).items() if stage in stage_names or stage in {"analyze", "gate", "frontend"}}
        if not argv and not set(item.get("stages") or []) & set(stage_names):
            continue
        rows.append({
            "tool": key,
            "aliases": list(item.get("aliases") or []),
            "docker_image": item.get("docker_image"),
            "cli": item.get("cli"),
            "install": item.get("install"),
            "env_names": list(item.get("env") or []),
            "skip_without": list(item.get("skip_without") or []),
            "stages": list(item.get("stages") or []),
            "argv": argv,
            "severity_from": item.get("severity_from"),
            "exit_codes": dict(item.get("exit_codes") or {}),
        })
    return rows
