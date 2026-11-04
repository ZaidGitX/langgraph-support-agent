"""Provider protocol, result shapes, and the shared base implementation."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
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


def artifact_for_stage(records: list[dict] | None, stage_name: str, *kinds: str, cwd: str | None = None) -> str | None:
    """The stored path of the suite file that runs ``stage_name`` (``buildspec-<stage>.yml``)."""
    needles = (f"-{stage_name}.", f"/{stage_name}.", f"_{stage_name}.")
    for record in records or []:
        if kinds and record.get("kind") not in kinds:
            continue
        path = str(record.get("path") or "")
        if any(needle in path for needle in needles):
            return stored_path(record, cwd)
    return None


@dataclass
class ProvisionResult:
    """What provisioning or secret injection did, or would do. Names only, never a value."""

    runner: str
    action: str  # provision | inject
    status: str  # planned | pass | fail | skipped
    mode: str  # plan | apply
    commands: list[list[str]] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    evidence: str = ""
    target: str = ""

    def as_dict(self) -> dict:
        return {
            "runner": self.runner,
            "action": self.action,
            "status": self.status,
            "mode": self.mode,
            "commands": [list(argv) for argv in self.commands],
            "names": list(self.names),
            "missing": list(self.missing),
            "evidence": self.evidence,
            "target": self.target,
        }


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
        # A CLI that proved it is signed in (device-code login, `aws login`, a named profile) is
        # ready even when the static key names are unset; see credentials.group_status.
        verified = False
        if self.credential_group:
            from .. import connect

            checked = connect.verified(self.credential_group) or {}
            verified = bool(checked.get("ok"))
        ready = cli_ok and (not missing or verified)
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

    # --- Sprint 4 phase 9: provisioning and secret injection ---------------------------------

    injection_target = ""  # human-readable store name, for example "GitHub repository secrets"

    def provision_commands(self, records: list[dict], cwd: str, answers: dict) -> list[list[str]]:
        """Commands that create the platform objects a suite needs. Placeholders by name."""
        return []

    def prepare_provision(self, records: list[dict], cwd: str, answers: dict, bundle: list[dict] | None = None) -> str:
        """Hook run before an apply (for example zip the checkout for S3). Returns a note.

        ``bundle`` is every generated record of the run (suite first); a provider that uploads
        the source packs it so platform builds find the generated stages' scripts.
        """
        return ""

    def provision(self, records: list[dict], cwd: str, *, mode: str, answers: dict, bundle: list[dict] | None = None) -> ProvisionResult:
        argvs = self.provision_commands(records, cwd, answers or {})
        status = self.detect()
        if not argvs:
            return ProvisionResult(self.runner, "provision", "skipped", mode, evidence="nothing to provision for this runner")
        if mode != "apply" or status.mode != "apply":
            note = "planned: " + ("missing " + ", ".join(status.missing) if status.missing else "cloud apply is off")
            if bundle:
                note += f"; {len(bundle)} generated file(s) travel with the source"
            return ProvisionResult(self.runner, "provision", "planned", "plan", argvs, missing=list(status.missing), evidence=note)
        note = self.prepare_provision(records, cwd, answers or {}, bundle)
        commands = [
            {"argv": expand_env(argv), "timeout": 600, "label": f"{self.runner}:provision:{position + 1}"}
            for position, argv in enumerate(argvs)
        ]
        ran = run_commands(commands, cwd)
        evidence = (note + "\n" if note else "") + str(ran.get("evidence") or "")
        return ProvisionResult(self.runner, "provision", "pass" if ran.get("status") == "pass" else "fail", "apply", argvs, evidence=evidence)

    def inject_commands(self, names: list[str], answers: dict) -> list[list[str]]:
        """Commands that copy vault values into the platform's secret store, one per name.

        A value never appears on an argv: the command reads it from stdin at apply time.
        """
        return []

    def inject_secrets(self, names: list[str], *, mode: str, answers: dict, cwd: str = ".") -> ProvisionResult:
        wanted = [name for name in dict.fromkeys(names or []) if name in credentials.all_names()]
        argvs = self.inject_commands(wanted, answers or {})
        status = self.detect()
        target = self.injection_target or self.runner
        if not wanted or not argvs:
            return ProvisionResult(self.runner, "inject", "skipped", mode, names=wanted, target=target, evidence="nothing to inject")
        missing_values = [name for name in wanted if not credentials.is_present(name)]
        if mode != "apply" or status.mode != "apply":
            note = "planned: " + ("missing " + ", ".join(status.missing) if status.missing else "cloud apply is off")
            return ProvisionResult(self.runner, "inject", "planned", "plan", argvs, names=wanted, missing=list(status.missing) + missing_values, target=target, evidence=note)
        if missing_values:
            return ProvisionResult(self.runner, "inject", "fail", "apply", argvs, names=wanted, missing=missing_values, target=target,
                                   evidence="no value for " + ", ".join(missing_values))
        outcomes = []
        ok = True
        for name, argv in zip(wanted, argvs, strict=False):
            result = self.run_injection(name, argv, cwd)
            outcomes.append(f"{name}: {'ok' if result else 'failed'}")
            ok = ok and result
        return ProvisionResult(self.runner, "inject", "pass" if ok else "fail", "apply", argvs, names=wanted, target=target, evidence="; ".join(outcomes))

    def run_injection(self, name: str, argv: list[str], cwd: str) -> bool:
        """Run one injection command with the value on stdin. Returns whether it succeeded."""
        value = os.getenv(name, "")
        try:
            completed = subprocess.run(
                expand_env(argv), cwd=cwd, input=value, capture_output=True, text=True, timeout=120, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0
