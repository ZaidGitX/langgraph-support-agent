"""Provider protocol, result shapes, and the shared base implementation."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from .. import credentials, templates
from ..runner import run_commands

LOCAL_RUNNERS = ("local", "docker")
_PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}|\$([A-Z][A-Z0-9_]*)")


@dataclass
class ProviderStatus:
    runner: str
    cli: bool
    cli_path: str
    present: list[str]
    missing: list[str]
    mode: str  # apply | plan
    notes: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class StageRun:
    """The same keys ``runner.run_commands`` returns, plus ``plan`` for a planned stage."""

    status: str  # pass | fail | skipped | planned | blocked
    exit_code: int
    blocked: bool
    commands: list[dict]
    checks: list[dict]
    evidence: str
    plan: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


class Provider(Protocol):
    runner: str
    platform: str
    cli: str | None
    credential_group: str | None
    artifact_kinds: tuple[str, ...]

    def detect(self) -> ProviderStatus: ...

    def render_skeleton(self, stage: dict, environment: dict | None, *, run_id: str = "", placeholders: list[str] | None = None) -> dict[str, str]: ...

    def commands_for(self, stage: dict, records: list[dict]) -> list[dict]: ...

    def would_run(self, stage: dict, records: list[dict], cwd: str, answers: dict) -> list[list[str]]: ...

    def execute(self, stage: dict, records: list[dict], cwd: str, *, mode: str, answers: dict) -> StageRun: ...


def expand_env(argv: list[str], env: Mapping[str, str] | None = None) -> list[str]:
    """Replace ``$NAME``/``${NAME}`` with the host value for config and path names only.

    Secret names are never expanded: a CLI reads them from the environment itself, so a
    secret value never lands on an argv that is recorded as evidence. A name that is unset
    falls back to its registry default, else stays as written so the gap is visible.
    """
    source = env if env is not None else os.environ

    def replace(match: re.Match) -> str:
        name = match.group(1) or match.group(2)
        if credentials.kind_of(name) == "secret":
            return match.group(0)
        value = str(source.get(name, "") or "").strip() or credentials.default_of(name)
        return value or match.group(0)

    return [_PLACEHOLDER.sub(replace, str(part)) for part in argv]


def run_id_of(stage: dict, answers: Mapping | None = None) -> str:
    return str(stage.get("run_id") or (answers or {}).get("run_id") or os.getenv("RUN_ID") or "local")


def tier_of(stage: dict, answers: Mapping | None = None) -> str:
    environment = stage.get("environment") or {}
    tier = str(environment.get("tier") or (answers or {}).get("env_target") or "local").lower()
    return tier if tier in {"local", "dev", "staging", "uat"} else "local"


def stored_path(record: dict, cwd: str | None = None) -> str:
    """The path of a generated artifact on disk: ``stored_at`` when written, else its repo path."""
    stored = str(record.get("stored_at") or record.get("path") or "")
    if cwd and stored and not Path(stored).is_absolute():
        candidate = Path(cwd) / stored
        if candidate.exists():
            return str(candidate)
    return stored


def artifact_of_kind(records: list[dict] | None, *kinds: str, cwd: str | None = None) -> str | None:
    for record in records or []:
        if record.get("kind") in kinds:
            return stored_path(record, cwd)
    return None


def stored_script(records: list[dict] | None, cwd: str | None = None) -> str | None:
    return artifact_of_kind(records, "shell", "script", cwd=cwd)


def plan_result(runner: str, missing: list[str], would_run: list[list[str]], records: list[dict] | None, note: str = "") -> StageRun:
    """What every provider returns when it cannot or may not apply."""
    would = [[str(part) for part in argv] for argv in would_run]
    shown = " ".join(would[0]) if would else "(nothing)"
    if not note:
        reason = f"missing {', '.join(missing)}; " if missing else "cloud apply is off; "
        note = f"planned: {reason}would run: {shown}"
    next_step = (
        f"Set {', '.join(missing)} and run again to apply."
        if missing
        else "Set ORCHESTRATOR_CLOUD_APPLY=1 (and the provider's names) to apply this stage."
    )
    return StageRun(
        status="planned",
        exit_code=0,
        blocked=False,
        commands=[{"label": f"{runner} plan", "argv": would[0] if would else [], "exit_code": 0, "evidence": note}],
        checks=[{"name": f"{runner} plan", "status": "planned", "job": "", "findings": note, "next_step": next_step, "needs_astra": False}],
        evidence=note,
        plan={
            "runner": runner,
            "mode": "plan",
            "missing": list(missing),
            "would_run": would,
            "artifacts": [str(record.get("path") or "") for record in records or []],
        },
    )


def from_run_commands(ran: dict) -> StageRun:
    return StageRun(
        status=str(ran.get("status") or "fail"),
        exit_code=int(ran.get("exit_code") or 0),
        blocked=bool(ran.get("blocked")),
        commands=list(ran.get("commands") or []),
        checks=list(ran.get("checks") or []),
        evidence=str(ran.get("evidence") or ""),
        plan={},
    )


class BaseProvider:
    """Shared behaviour: detect from the credential registry, render skeletons from the
    templates, plan unless everything is ready and allowed, apply through ``run_commands``."""

    runner = "local"
    platform = "local"
    cli: str | None = None
    credential_group: str | None = None
    artifact_kinds: tuple[str, ...] = ("shell",)
    timeout = 1800

    def detect(self) -> ProviderStatus:
        cli_path = shutil.which(self.cli) if self.cli else ""
        if self.credential_group:
            present, missing = credentials.split_present(self.credential_group)
        else:
            present, missing = [], []
        cli_ok = bool(cli_path) if self.cli else True
        ready = cli_ok and not missing
        return ProviderStatus(
            runner=self.runner,
            cli=cli_ok,
            cli_path=cli_path or "",
            present=present,
            missing=missing,
            mode=credentials.runner_mode(self.runner, ready),
        )

    def placeholders(self) -> list[str]:
        return credentials.placeholders_for(self.credential_group) if self.credential_group else []

    def render_skeleton(self, stage: dict, environment: dict | None = None, *, run_id: str = "", placeholders: list[str] | None = None) -> dict[str, str]:
        names = list(placeholders) if placeholders is not None else self.placeholders()
        return {
            kind: templates.render_template(kind, stage, environment, runner=self.runner, run_id=run_id, placeholders=names)
            for kind in self.artifact_kinds
        }

    def commands_for(self, stage: dict, records: list[dict]) -> list[dict]:
        """Argv for runners that execute on this host; remote runners return nothing here."""
        return []

    def would_run(self, stage: dict, records: list[dict], cwd: str, answers: dict) -> list[list[str]]:
        raise NotImplementedError

    def execute(self, stage: dict, records: list[dict], cwd: str, *, mode: str, answers: dict) -> StageRun:
        status = self.detect()
        argvs = self.would_run(stage, records, cwd, answers or {})
        if mode != "apply" or status.mode != "apply":
            return plan_result(self.runner, status.missing, argvs, records)
        return self.apply(stage, argvs, cwd, records, answers or {})

    def apply(self, stage: dict, argvs: list[list[str]], cwd: str, records: list[dict], answers: dict) -> StageRun:
        commands = [
            {"argv": expand_env(argv), "timeout": self.timeout, "label": f"{self.runner}:{stage.get('name')}:{position + 1}"}
            for position, argv in enumerate(argvs)
        ]
        return from_run_commands(run_commands(commands, cwd))
