#!/usr/bin/env python3
"""Scaffold a provider module and its test for Phase 4.

  python3 scaffold_provider.py aws_codebuild --platform aws --cli aws --group aws
  python3 scaffold_provider.py jenkins --platform jenkins --group jenkins

Writes orchestrator/providers/<runner>.py and tests/test_provider_<runner>.py from the
Provider protocol in references/06-providers-runners.md. Refuses to overwrite unless --force.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
ROOT = SKILL_DIR.parents[2]

MODULE = '''"""{runner} provider.

Runner `{runner}` on platform `{platform}`. Detects the CLI and the credential names from
orchestrator/credentials.json group `{group}`; renders the artifact skeleton from the skill
templates; executes in plan mode (records the exact argv it would run) or apply mode (runs
it). Never reads or prints a credential value.
"""

from __future__ import annotations

import os
import shutil

from .base import Provider, ProviderStatus, StageRun, plan_result, run_argv
from .. import credentials

RUNNER = "{runner}"
PLATFORM = "{platform}"
CLI = {cli!r}
GROUP = "{group}"


class {class_name}(Provider):
    runner = RUNNER
    platform = PLATFORM
    cli = CLI
    credential_keys = [GROUP]
    artifact_kinds = {kinds!r}

    def detect(self) -> ProviderStatus:
        present, missing = credentials.split_present(GROUP)
        cli_path = shutil.which(CLI) if CLI else ""
        cli_ok = bool(cli_path) if CLI else True
        mode = "apply" if cli_ok and not missing and (os.getenv("ORCHESTRATOR_CLOUD_APPLY") == "1") else "plan"
        return ProviderStatus(runner=RUNNER, cli=cli_ok, cli_path=cli_path or "", present=present, missing=missing, mode=mode)

    def render_skeleton(self, stage: dict, environment: dict) -> dict[str, str]:
        from ..generator import render_template

        return {{kind: render_template(kind, stage, environment, runner=RUNNER) for kind in self.artifact_kinds}}

    def commands_for(self, stage: dict, records: list[dict]) -> list[dict]:
        return []  # remote runner: execution happens in execute()

    def would_run(self, stage: dict, records: list[dict]) -> list[list[str]]:
        # TODO(phase 4): build the exact argv from the runner matrix in 06-providers-runners.md
        return [[CLI or "echo", "--version"]]

    def execute(self, stage: dict, records: list[dict], cwd: str, *, mode: str, answers: dict) -> StageRun:
        status = self.detect()
        argvs = self.would_run(stage, records)
        if mode != "apply" or status.mode != "apply":
            return plan_result(RUNNER, status.missing, argvs, records)
        commands = [run_argv(argv, cwd, timeout=1800, label=f"{{RUNNER}} {{stage['name']}}") for argv in argvs]
        failed = [c for c in commands if c["exit_code"] != 0]
        evidence = "\\n".join(f"[{{c['label']}}] exit={{c['exit_code']}}\\n{{c['evidence']}}" for c in commands)
        return StageRun(
            status="fail" if failed else "pass",
            exit_code=failed[0]["exit_code"] if failed else 0,
            blocked=False,
            commands=commands,
            checks=[{{"name": c["label"], "status": "pass" if c["exit_code"] == 0 else "fail", "job": ""}} for c in commands],
            evidence=evidence,
            plan={{}},
        )
'''

TEST = '''from __future__ import annotations

import subprocess

import pytest

from orchestrator.providers import registry
from orchestrator.providers.{runner} import {class_name}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("{runner} must not run a subprocess in plan mode")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.delenv("ORCHESTRATOR_CLOUD_APPLY", raising=False)


def test_{runner}_is_registered():
    assert registry.for_runner("{runner}").runner == "{runner}"


def test_{runner}_plans_without_credentials(tmp_path):
    provider = {class_name}()
    stage = {{"name": "deploy_dev", "family": "deploy", "runner": "{runner}", "provider": "", "environment": {{"tier": "dev", "platform": "{platform}"}}}}
    run = provider.execute(stage, [], str(tmp_path), mode="plan", answers={{}})
    assert run.status == "planned"
    assert run.plan["runner"] == "{runner}"
    assert run.plan["would_run"]
    assert "production" not in str(run.plan)
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runner")
    parser.add_argument("--platform", required=True)
    parser.add_argument("--group", required=True, help="credential registry group key")
    parser.add_argument("--cli", default="", help="CLI binary; empty for REST-only providers")
    parser.add_argument("--kinds", default="shell", help="comma-separated artifact kinds")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    runner = args.runner.strip().lower()
    class_name = "".join(part.capitalize() for part in runner.split("_")) + "Provider"
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    module_path = ROOT / "orchestrator" / "providers" / f"{runner}.py"
    test_path = ROOT / "tests" / f"test_provider_{runner}.py"
    for path in (module_path, test_path):
        if path.exists() and not args.force:
            print(f"refusing to overwrite {path} (use --force)")
            return 1
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_path.write_text(MODULE.format(runner=runner, platform=args.platform, cli=args.cli or None, group=args.group, class_name=class_name, kinds=kinds), encoding="utf-8")
    test_path.write_text(TEST.format(runner=runner, platform=args.platform, class_name=class_name), encoding="utf-8")
    print(f"wrote {module_path}\nwrote {test_path}")
    print("next: register it in orchestrator/providers/registry.py:")
    print(f'    from .{runner} import {class_name}\n    PROVIDERS["{runner}"] = {class_name}()')
    print("then fill would_run() from references/06-providers-runners.md and run the tests.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
