"""The push workflow takes where a run goes from orchestrator.yaml (Sprint 4, phase 12).

``scripts/profile_env.py`` prints NAME=value lines for $GITHUB_ENV with the standard library;
``orchestrator.profile.workflow_env`` is the in-process twin. Both must agree, config names
are registry names of kind config only, and a forbidden tier stops the step.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator import profile
from scripts import profile_env

ROOT = Path(__file__).resolve().parent.parent

AWS_PROFILE = """
version: 1
platform: aws   # the live platform
tier: dev
config:
  AWS_REGION: eu-west-1
  AWS_S3_BUCKET: "my-bucket"
answers:
  by_id:
    env_platform: aws
  by_topic:
    platform: aws
autopilot:
  approve: true
"""


def _lines(text: str, parser=profile_env.parse_profile) -> tuple[list[str], list[str]]:
    return profile_env.lines_for(parser(text), profile_env.registry_kinds())


def test_cloud_platform_exports_platform_dispatch_cloud_apply_and_config():
    output, warnings = _lines(AWS_PROFILE)
    assert output == [
        "ORCHESTRATOR_PROFILE_PLATFORM=aws",
        "ORCHESTRATOR_PROFILE_TIER=dev",
        "ORCHESTRATOR_DISPATCH=platform",
        "ORCHESTRATOR_CLOUD_APPLY=1",
        "AWS_REGION=eu-west-1",
        "AWS_S3_BUCKET=my-bucket",
    ]
    assert warnings == []


def test_plain_reader_matches_pyyaml_for_the_profile_shape():
    assert _lines(AWS_PROFILE, profile_env.parse_plain) == _lines(AWS_PROFILE)


def test_local_platform_keeps_github_dispatch_and_no_cloud_apply():
    output, _ = _lines("platform: local\ntier: dev\n")
    assert "ORCHESTRATOR_DISPATCH=github" in output and "ORCHESTRATOR_CLOUD_APPLY=0" in output


def test_a_secret_or_unknown_name_in_config_is_skipped_with_a_warning():
    output, warnings = _lines("platform: aws\nconfig:\n  AWS_SECRET_ACCESS_KEY: nope\n  NOT_A_NAME: x\n  AWS_REGION: ''\n")
    assert not any(line.startswith(("AWS_SECRET_ACCESS_KEY", "NOT_A_NAME", "AWS_REGION")) for line in output)
    assert len(warnings) == 3 and "secret never comes from a file" in warnings[0]


def test_a_forbidden_tier_stops_the_step():
    with pytest.raises(SystemExit):
        _lines("platform: aws\ntier: production\n")


def test_script_and_module_agree_on_the_committed_profile(capsys):
    assert profile_env.main([str(ROOT / "orchestrator.yaml")]) == 0
    printed = [line for line in capsys.readouterr().out.splitlines() if line]
    loaded = profile.load(ROOT)
    assert loaded is not None
    assert printed == [f"{name}={value}" for name, value in profile.workflow_env(loaded).items()]
    assert loaded.platform == "aws" and "AWS_REGION" in loaded.config


def test_missing_profile_keeps_the_workflow_environment(tmp_path, capsys):
    assert profile_env.main([str(tmp_path / "orchestrator.yaml")]) == 0
    assert capsys.readouterr().out == ""
