#!/usr/bin/env python3
"""Print the environment a push-triggered run takes from orchestrator.yaml (NAME=value lines).

The push workflow appends the output to $GITHUB_ENV before ``docker compose up``, so the
committed profile, not the workflow file, decides where a push goes:

  ORCHESTRATOR_PROFILE_PLATFORM, _TIER   the profile's platform and tier
  ORCHESTRATOR_DISPATCH                  platform for jenkins, aws, gcp, azure; github otherwise
  ORCHESTRATOR_CLOUD_APPLY               1 for those platforms, else 0
  <config names>                         the profile's ``config:`` block: registry names of
                                         kind config only, anything else is skipped with a
                                         warning on stderr (a secret never comes from a file)

Standard library only: the runner has neither PyYAML nor pydantic. It mirrors
``orchestrator.profile.workflow_env``; ``tests/test_profile_env.py`` keeps the two in step.
A forbidden tier (production) fails the step before anything starts.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "orchestrator.yaml"
REGISTRY = ROOT / "orchestrator" / "credentials.json"
CLOUD_PLATFORMS = ("jenkins", "aws", "gcp", "azure")
PLATFORMS = ("local", "docker", "github", "jenkins", "aws", "gcp", "azure", "remote", "kubernetes")
FORBIDDEN_TIERS = ("production", "prod")
NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _scalar(text: str) -> str:
    """A YAML scalar as this file writes them: optional quotes, a trailing comment dropped."""
    text = text.strip()
    if len(text) >= 2 and text[0] in {'"', "'"} and text[-1] == text[0]:
        return text[1:-1]
    kept = []
    for index, char in enumerate(text):
        if char == "#" and (index == 0 or text[index - 1].isspace()):
            break
        kept.append(char)
    return "".join(kept).strip()


def parse_plain(text: str) -> dict:
    """Top-level ``key: value`` scalars and the two-space-indented mappings under a bare key
    (enough for ``platform``, ``tier``, and ``config``). Deeper blocks are ignored."""
    data: dict = {}
    current: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        key, separator, value = raw.strip().partition(":")
        if not separator:
            continue
        key = key.strip()
        if indent == 0:
            scalar = _scalar(value)
            if scalar:
                data[key] = scalar
                current = None
            else:
                current = key
                data[key] = {}
        elif indent == 2 and current and isinstance(data.get(current), dict):
            scalar = _scalar(value)
            if scalar:
                data[current][key] = scalar
    return data


def parse_profile(text: str) -> dict:
    """PyYAML when it is installed, the plain reader otherwise."""
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return parse_plain(text)
    data = yaml.safe_load(text) or {}
    return data if isinstance(data, dict) else {}


def registry_kinds(path: Path = REGISTRY) -> dict[str, str]:
    """name -> kind from orchestrator/credentials.json; empty when it cannot be read."""
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    kinds: dict[str, str] = {}
    for group in (registry.get("groups") or {}).values():
        for item in group.get("env") or []:
            if isinstance(item, dict) and item.get("name"):
                kinds[str(item["name"])] = str(item.get("kind") or "")
    return kinds


def lines_for(profile: dict, kinds: dict[str, str]) -> tuple[list[str], list[str]]:
    """The NAME=value lines and the warnings for a parsed profile."""
    platform = str(profile.get("platform") or "local").strip().lower()
    tier = str(profile.get("tier") or "dev").strip().lower()
    warnings: list[str] = []
    if tier in FORBIDDEN_TIERS:
        raise SystemExit(f"orchestrator.yaml names the forbidden tier {tier!r}; nothing starts")
    if platform not in PLATFORMS:
        warnings.append(f"platform {platform!r} is not one the registry knows; the run will report the invalid profile")
    cloud = platform in CLOUD_PLATFORMS
    output = [
        f"ORCHESTRATOR_PROFILE_PLATFORM={platform}",
        f"ORCHESTRATOR_PROFILE_TIER={tier}",
        f"ORCHESTRATOR_DISPATCH={'platform' if cloud else 'github'}",
        f"ORCHESTRATOR_CLOUD_APPLY={'1' if cloud else '0'}",
    ]
    config = profile.get("config") or {}
    if not isinstance(config, dict):
        warnings.append("config must be a mapping of registry config names to values; ignored")
        config = {}
    for name, value in config.items():
        key = str(name).strip()
        text = "" if value is None else str(value).strip()
        if not NAME.match(key) or kinds.get(key) != "config":
            warnings.append(f"config.{key} skipped: not a registry name of kind config (a secret never comes from a file)")
            continue
        if not text or "\n" in text:
            warnings.append(f"config.{key} skipped: it needs a one-line value")
            continue
        output.append(f"{key}={text}")
    return output, warnings


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    path = Path(args[0]) if args else PROFILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        print(f"no {path.name} at {path.parent}: the run keeps the workflow's own environment", file=sys.stderr)
        return 0
    output, warnings = lines_for(parse_profile(text), registry_kinds())
    for line in output:
        print(line)
    for warning in warnings:
        print(f"profile_env: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
