"""Live run 9 (2026-11-05) stopped at secret injection: generated stages had declared the
orchestrator's own AWS identity in env manifests and a GitHub workflow, and the session token has
no value with static keys. Injection now copies exactly what the platform's own files bind, never
the platform's identity secrets or a name without a value, and autopilot refuses a plan whose
injection plan still has an unresolved bound name."""

from __future__ import annotations

from pathlib import Path

import yaml

from orchestrator import credentials, generator, profile, suite
from orchestrator.graph import _bound_secret_names
from orchestrator.providers.aws import AwsProvider
from orchestrator.providers.local import LocalProvider
from orchestrator.schemas import GeneratedArtifact
from orchestrator.state import initial_state
from tests.test_run9_scan_menu import ANSWERS, STAGES

ROOT = Path(__file__).resolve().parent.parent
PRESENT = {"SNYK_TOKEN", "OPENAI_API_KEY", "TYPESAFE_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}


def _present(name: str, env=None) -> bool:
    return name in PRESENT


def test_identity_secrets_are_never_injectable(monkeypatch):
    monkeypatch.setattr(credentials, "is_present", _present)
    assert credentials.identity_secrets("aws") == ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"]
    assert credentials.identity_secrets("jenkins") == ["JENKINS_API_TOKEN"] and credentials.identity_secrets("nope") == []
    assert suite.injectable_secrets(["AWS_SESSION_TOKEN", "AWS_ACCESS_KEY_ID", "SNYK_TOKEN", "TRIVY_TOKEN", "AWS_REGION", "OPENAI_API_KEY"], "aws") == ["SNYK_TOKEN", "OPENAI_API_KEY"]


def test_bound_names_come_only_from_files_the_platform_resolves():
    records = [
        {"path": ".env.dev.example", "kind": "env_example", "content": "AWS_SESSION_TOKEN=\nSNYK_TOKEN=\n", "secrets_referenced": ["AWS_SESSION_TOKEN", "SNYK_TOKEN"]},
        {"path": ".github/workflows/gen-deploy_dev.yml", "kind": "github_workflow", "content": "env:\n  AWS_SESSION_TOKEN: ${{ secrets.AWS_SESSION_TOKEN }}\n", "secrets_referenced": ["AWS_SESSION_TOKEN", "TYPESAFE_API_KEY"]},
        {"path": "ci/verify-build-image.py", "kind": "script", "content": "NAMES = ('AWS_ACCESS_KEY_ID', 'AWS_SESSION_TOKEN')\n", "secrets_referenced": ["AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN"]},
        {"path": "ci/buildspec-scanners.yml", "kind": "buildspec", "content": 'env:\n  secrets-manager:\n    SNYK_TOKEN: "langgraph-support-app/SNYK_TOKEN"\n    AWS_ACCESS_KEY_ID: "langgraph-support-app/AWS_ACCESS_KEY_ID"\n', "secrets_referenced": []},
        {"path": "deploy/ecs-stack.yaml", "kind": "cloudformation", "content": "Secrets: []\n", "secrets_referenced": ["OPENAI_API_KEY"]},
    ]
    assert suite.bound_secret_names(records, "aws") == ["OPENAI_API_KEY", "SNYK_TOKEN"]
    assert _bound_secret_names(records, "aws") == ["OPENAI_API_KEY", "SNYK_TOKEN"]
    # On GitHub the workflow's declared names are bindings, every explicit binding in content still
    # counts, and the AWS key pair is not GitHub's identity.
    assert suite.bound_secret_names(records, "github") == ["AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN", "SNYK_TOKEN", "TYPESAFE_API_KEY"]
    # No platform given: every declaration counts (the pre-run-9 behaviour), identity excluded by nobody.
    assert "AWS_SESSION_TOKEN" in suite.bound_secret_names(records)


def test_harden_drops_bindings_codebuild_could_not_resolve(monkeypatch):
    monkeypatch.setattr(credentials, "is_present", _present)
    content = (
        "version: 0.2\nenv:\n  variables:\n    TIER: dev\n  secrets-manager:\n"
        '    SNYK_TOKEN: "langgraph-support-app/SNYK_TOKEN"\n'
        '    AWS_SESSION_TOKEN: "langgraph-support-app/AWS_SESSION_TOKEN"\n'
        "    TRIVY_TOKEN: TRIVY_TOKEN\n"
        "phases:\n  build:\n    commands:\n      - bash ci/scanners.sh\n"
    )
    hardened = generator.harden(GeneratedArtifact(path="ci/buildspec-scanners.yml", kind="buildspec", purpose="p", content=content))
    spec = yaml.safe_load(hardened.content)
    assert spec["env"]["secrets-manager"] == {"SNYK_TOKEN": "langgraph-support-app/SNYK_TOKEN"}
    assert spec["env"]["variables"] == {"TIER": "dev"} and spec["phases"]["build"]["commands"] == ["bash ci/scanners.sh"]
    clean = generator.harden(GeneratedArtifact(path="ci/buildspec-build.yml", kind="buildspec", purpose="p", content="version: 0.2\nphases:\n  build:\n    commands:\n      - bash ci/build.sh\n"))
    assert clean.content.endswith("- bash ci/build.sh\n")  # untouched when nothing is bound
    only_bad = generator.harden(GeneratedArtifact(
        path="ci/buildspec-x.yml", kind="buildspec", purpose="p",
        content='version: 0.2\nenv:\n  secrets-manager:\n    AWS_SECRET_ACCESS_KEY: "langgraph-support-app/AWS_SECRET_ACCESS_KEY"\nphases:\n  build:\n    commands:\n      - echo hi\n',
    ))
    assert "env" not in yaml.safe_load(only_bad.content)


def test_platform_stages_never_get_the_platform_identity_as_placeholders(monkeypatch):
    monkeypatch.setattr(credentials, "is_present", _present)
    stage = {"name": "deploy_dev", "family": "deploy", "runner": "aws_codebuild", "environment": {"tier": "dev", "platform": "aws"}}
    names = generator.placeholders_for(stage, AwsProvider())
    assert "AWS_REGION" in names and not {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"} & set(names)
    assert generator.placeholders_for({**stage, "runner": "local"}, LocalProvider()) == list(LocalProvider().placeholders())
    manifest = generator.env_manifest(stage, names)
    assert "AWS_SESSION_TOKEN" not in manifest.content and "AWS_SESSION_TOKEN" not in manifest.secrets_referenced
    cloud = initial_state(str(ROOT), run_id="r10")
    cloud["answers"] = dict(ANSWERS, env_platform="aws", env_target="dev")
    cloud["pipeline"] = {"stages": STAGES, "environment": {"tier": "dev", "platform": "aws"}}
    context = suite.context_for(cloud, AwsProvider())
    assert not {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"} & set(context["placeholders"])
    assert not {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"} & set(context["available_secrets"])
    assert "SNYK_TOKEN" in context["available_secrets"]


def test_autopilot_refuses_a_plan_with_an_unresolved_binding():
    loaded = profile.load(ROOT)
    view = {
        "artifacts": {}, "artifact_review": {}, "jev_decisions": [], "pipeline": {"stages": [{"name": "build"}]},
        "suite": {"status": "ok"},
        "injection": {"status": "planned", "names": ["SNYK_TOKEN", "AWS_SESSION_TOKEN"], "missing": ["AWS_SESSION_TOKEN"]},
    }
    ok, reason = profile.approval(loaded, view)
    assert not ok and "AWS_SESSION_TOKEN" in reason and "unbind" in reason
    view["injection"]["missing"] = ["AWS_ACCESS_KEY_ID"]  # a provider credential missing in plan mode, not a bound name
    assert profile.approval(loaded, view)[0]
    view["injection"]["missing"] = []
    assert profile.approval(loaded, view)[0]
