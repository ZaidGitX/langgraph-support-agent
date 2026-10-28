from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from orchestrator import credentials, redaction
from orchestrator.llm import scrub
from orchestrator.runner import redact_secrets

ROOT = Path(__file__).resolve().parents[1]
SKILL_ASSET = ROOT / ".claude" / "skills" / "sprint3-agentic-cicd" / "assets" / "credentials.json"

# Credential-shaped samples are assembled at runtime so this file never holds one.
FAKE_AKIA = "AKIA" + "0" * 16
FAKE_JWT = "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20
FAKE_PAT = "github_pat_" + "x" * 30
FAKE_VALUE = "fakevalue-" + "z" * 20
PEM_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"


def test_names_are_unique_upper_snake_case():
    names = credentials.all_names()
    assert names and len(names) == len(set(names))
    assert all(re.fullmatch(r"[A-Z][A-Z0-9_]+", name) for name in names)


def test_registry_groups_kinds_and_runner_mapping():
    groups = credentials.groups()
    assert {"app", "github", "aws", "gcp", "azure", "jenkins", "docker", "ssh", "kubernetes", "scanners", "orchestrator"} <= set(groups)
    assert credentials.kind_of("AWS_SECRET_ACCESS_KEY") == "secret"
    assert credentials.kind_of("AWS_REGION") == "config"
    assert credentials.kind_of("GOOGLE_APPLICATION_CREDENTIALS") == "path"
    assert credentials.kind_of("NOT_A_NAME") == "config"
    assert credentials.group_for_runner("aws_codebuild") == "aws"
    assert credentials.group_for_runner("github_actions") == "github"
    assert credentials.group_for_runner("local") is None
    assert "AWS_REGION" in credentials.names_for("aws")
    assert "AWS_PROFILE" in credentials.names_for("aws")
    assert credentials.placeholders_for("jenkins") == ["JENKINS_URL", "JENKINS_USER", "JENKINS_API_TOKEN", "JENKINS_JOB"]


def test_split_present_honours_alternative_sets():
    present, missing = credentials.split_present("aws", env={})
    assert present == []
    assert {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"} <= set(missing)
    _, missing = credentials.split_present("aws", env={"AWS_PROFILE": "dev", "AWS_REGION": "us-east-1"})
    assert missing == []
    _, missing = credentials.split_present("aws", env={"AWS_ACCESS_KEY_ID": FAKE_AKIA, "AWS_SECRET_ACCESS_KEY": FAKE_VALUE, "AWS_REGION": "us-east-1"})
    assert missing == []
    _, missing = credentials.split_present("github", env={"GH_TOKEN": FAKE_VALUE, "GITHUB_REPOSITORY": "o/r"})
    assert missing == []


def test_readiness_reports_names_only_and_plan_modes():
    env = {"AWS_ACCESS_KEY_ID": FAKE_AKIA, "AWS_SECRET_ACCESS_KEY": FAKE_VALUE}
    report = credentials.readiness(env=env)
    text = json.dumps(report)
    assert FAKE_VALUE not in text and FAKE_AKIA not in text
    aws = report["providers"]["aws"]
    assert "AWS_REGION" in aws["missing"] and aws["mode"] == "plan"
    assert set(aws["present"]) == {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
    assert report["runners"]["local"]["mode"] == "apply"
    assert report["runners"]["aws_codebuild"]["mode"] == "plan"
    assert report["runners"]["gcp_cloudbuild"]["mode"] == "plan"
    assert report["toggles"]["cloud_apply"] is False
    assert report["toggles"]["ORCHESTRATOR_DISPATCH"] == "local"
    assert set(report["scanners"]) >= {"semgrep", "trivy", "gitleaks", "snyk", "stackhawk", "sonar"}
    assert report["scanners"]["sonar"]["env_missing"] == ["SONAR_TOKEN", "SONAR_ORGANIZATION", "SONAR_PROJECT_KEY"]


def test_cloud_apply_only_turns_a_complete_group_into_apply():
    env = {"AWS_PROFILE": "dev", "AWS_REGION": "us-east-1", "ORCHESTRATOR_CLOUD_APPLY": "1"}
    report = credentials.readiness(env=env)
    expected = "apply" if shutil.which("aws") else "plan"
    assert report["providers"]["aws"]["mode"] == expected
    assert report["runners"]["aws_codebuild"]["mode"] == expected
    assert report["runners"]["jenkins"]["mode"] == "plan"
    assert report["toggles"]["cloud_apply"] is True
    github = credentials.readiness(env={"GITHUB_TOKEN": FAKE_VALUE, "GITHUB_REPOSITORY": "o/r", "ORCHESTRATOR_DISPATCH": "github"})
    assert github["runners"]["github_actions"]["mode"] == ("apply" if shutil.which("gh") else "plan")


def test_provider_statuses_override_runner_detection():
    report = credentials.readiness(provider_statuses={"docker": {"cli": False, "missing": [], "mode": "plan"}}, env={})
    assert report["runners"]["docker"] == {"group": "docker", "cli": False, "missing": [], "mode": "plan"}


def test_env_example_is_generated_from_the_registry():
    rendered = credentials.render_env_example()
    assert credentials.env_example_names(rendered) == credentials.all_names()
    assert (ROOT / ".env.example").read_text(encoding="utf-8") == rendered
    assert "OPENAI_MODEL=gpt-6-astra" in rendered
    assert re.search(r"(?m)^OPENAI_API_KEY=$", rendered)


def test_compose_and_push_workflow_carry_every_placeholder():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for name in credentials.all_names():
        if name in {"ORCHESTRATOR_DB"}:
            continue
        # Every name is passed through from the host environment (GH_TOKEN mirrors GITHUB_TOKEN).
        assert f"      {name}: ${{" in compose, name
    workflow = (ROOT / ".github" / "workflows" / "orchestrator.yml").read_text(encoding="utf-8")
    for name in credentials.github_secret_names():
        assert f"{name}: ${{{{ secrets.{name} }}}}" in workflow, name


def test_readiness_never_leaks_a_set_value(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", FAKE_VALUE)
    report = credentials.readiness()
    assert FAKE_VALUE not in json.dumps(report)
    assert "AWS_SECRET_ACCESS_KEY" in report["providers"]["aws"]["present"]


def test_redaction_covers_tokens_values_and_assignments(monkeypatch):
    monkeypatch.setenv("SNYK_TOKEN", FAKE_VALUE)
    text = f"token {FAKE_VALUE} key {FAKE_AKIA} jwt {FAKE_JWT} pat {FAKE_PAT} api_key=abcdef12 {PEM_HEADER}"
    cleaned = redaction.redact(text)
    for sample in (FAKE_VALUE, FAKE_AKIA, FAKE_JWT, FAKE_PAT, "abcdef12", PEM_HEADER):
        assert sample not in cleaned
    assert "api_key=[redacted]" in cleaned
    assert scrub(text) == cleaned
    assert redact_secrets(text) == cleaned
    assert redaction.redact("x" * 50, limit=10).startswith("x" * 10) and "[truncated]" in redaction.redact("x" * 50, limit=10)
    assert redaction.redact("hello world") == "hello world"


def test_file_scan_patterns_are_narrower():
    assert redaction.file_has_secret("key " + FAKE_AKIA) is True
    assert redaction.file_has_secret("hawk." + "a" * 8 + "." + "b" * 8) is True
    assert redaction.file_has_secret(FAKE_JWT) is False
    assert redaction.file_has_secret("plain text") is False


@pytest.mark.skipif(not SKILL_ASSET.is_file(), reason="skill asset not present in this checkout")
def test_package_registry_matches_the_skill_asset():
    assert json.loads(SKILL_ASSET.read_text(encoding="utf-8")) == credentials.registry()


def test_readiness_endpoint_works_before_a_session(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", FAKE_VALUE)
    with TestClient(app) as client:
        response = client.get("/api/orchestrator/readiness")
    assert response.status_code == 200
    body = response.json()
    assert {"app", "providers", "runners", "scanners", "toggles"} <= set(body)
    assert FAKE_VALUE not in response.text
