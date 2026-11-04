"""The profile's ``config`` block (Sprint 4, phase 12): non-secret names a push needs."""

from __future__ import annotations

import pytest

from orchestrator import profile
from orchestrator.session import SessionService

BASE = """
version: 1
platform: aws
tier: dev
config:
  AWS_REGION: eu-west-1
"""


def test_config_names_are_registry_config_names_only(tmp_path):
    (tmp_path / "orchestrator.yaml").write_text(BASE, encoding="utf-8")
    loaded = profile.load(tmp_path)
    assert loaded and loaded.config == {"AWS_REGION": "eu-west-1"}
    assert profile.workflow_env(loaded) == {
        "ORCHESTRATOR_PROFILE_PLATFORM": "aws",
        "ORCHESTRATOR_PROFILE_TIER": "dev",
        "ORCHESTRATOR_DISPATCH": "platform",
        "ORCHESTRATOR_CLOUD_APPLY": "1",
        "AWS_REGION": "eu-west-1",
    }
    for bad, reason in (
        ("config:\n  AWS_SECRET_ACCESS_KEY: x\n", "never a credential"),
        ("config:\n  NOT_A_NAME: x\n", "not a name in the credential registry"),
        ("config:\n  AWS_REGION: ''\n", "needs a value"),
    ):
        (tmp_path / "orchestrator.yaml").write_text("platform: aws\n" + bad, encoding="utf-8")
        with pytest.raises(profile.ProfileError, match=reason):
            profile.load(tmp_path)


def test_dispatch_follows_the_platform():
    assert profile.dispatch_for("aws") == profile.dispatch_for("jenkins") == "platform"
    assert profile.dispatch_for("local") == profile.dispatch_for("github") == "github"


def test_apply_config_fills_only_empty_names(tmp_path):
    (tmp_path / "orchestrator.yaml").write_text(BASE + "  AWS_S3_BUCKET: profile-bucket\n", encoding="utf-8")
    loaded = profile.load(tmp_path)
    env = {"AWS_REGION": "", "AWS_S3_BUCKET": "host-bucket"}
    assert profile.apply_config(loaded, env) == ["AWS_REGION"]
    assert env == {"AWS_REGION": "eu-west-1", "AWS_S3_BUCKET": "host-bucket"}


def test_session_exports_config_even_with_autopilot_off(tmp_path, monkeypatch):
    (tmp_path / "orchestrator.yaml").write_text(BASE, encoding="utf-8")
    monkeypatch.setenv("AWS_REGION", "")
    monkeypatch.setenv("ORCHESTRATOR_AUTOPILOT", "0")
    service = SessionService(repo_root=str(tmp_path), db_path=str(tmp_path / "db.sqlite"))
    service._run_root = str(tmp_path)
    view = service._load_profile()
    assert view["enabled"] is False and view["disabled_by"] == "ORCHESTRATOR_AUTOPILOT" and view["config"] == ["AWS_REGION"]
    assert service.profile is None
    assert __import__("os").environ["AWS_REGION"] == "eu-west-1"
    monkeypatch.setenv("ORCHESTRATOR_AUTOPILOT", "1")
    view = service._load_profile()
    assert view["enabled"] is True and view["platform"] == "aws" and view["config"] == ["AWS_REGION"]
    assert service.profile is not None
