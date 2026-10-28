"""Scanner and test-tool catalog (Sprint 3, phase 4).

``scanners.json`` (a copy of the skill asset) describes every tool a scan or test stage may
use: CLI name, Docker image, install hint, argv templates per stage, the CI action that runs
it on GitHub, the credential names it reads, and how to extract findings from its output.
Tools are data; adding one is a catalog entry, not a code path.
"""

from __future__ import annotations

import json
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
