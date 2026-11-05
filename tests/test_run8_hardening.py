"""Live run 8 (2026-11-05), the first run that executed on CodeBuild: no artifacts block in a
buildspec (the project has no artifacts destination), a stage with a buildspec always runs
inside CodeBuild, a failed build brings its CloudWatch log tail, and the deploy names have
defaults the project environment carries."""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator import generator, templates
from orchestrator.providers.aws import AwsProvider
from orchestrator.schemas import GeneratedArtifact

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "orchestrator" / "templates"

BUILDSPEC = (
    "version: 0.2\n"
    "phases:\n"
    "  build:\n"
    "    commands:\n"
    "      - pytest -q\n"
    "artifacts:\n"
    "  files:\n"
    "    - var/**/*\n"
    "  discard-paths: no\n"
    "cache:\n"
    "  paths:\n"
    "    - /root/.cache/pip/**/*\n"
)


def test_an_artifacts_block_is_dropped_from_a_buildspec_and_the_templates_have_none():
    hardened = generator.harden(GeneratedArtifact(path="ci/buildspec-test.yml", kind="buildspec", purpose="t", content=BUILDSPEC))
    assert "artifacts:" not in hardened.content and "discard-paths" not in hardened.content
    assert "      - pytest -q\n" in hardened.content and "cache:\n  paths:\n    - /root/.cache/pip/**/*\n" in hardened.content
    untouched = GeneratedArtifact(path="ci/x.sh", kind="shell", purpose="s", content="artifacts:\n  files: []\n")
    assert generator.harden(untouched) is untouched
    for name in ("buildspec.yml.tpl", "buildspec.stage.yml.tpl"):
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "\nartifacts:" not in text, name


def test_a_stage_with_a_buildspec_runs_inside_codebuild_whatever_its_provider(tmp_path):
    provider = AwsProvider()
    stage = {"name": "infra_plan", "provider": "aws_cfn", "runner": "aws_codebuild"}
    answers = {"run_id": "r1"}
    spec = tmp_path / "ci" / "buildspec-infra_plan.yml"
    spec.parent.mkdir()
    spec.write_text("version: 0.2\n", encoding="utf-8")
    records = [
        {"path": "deploy/dev-ecs.generated.yml", "kind": "cloudformation", "stored_at": str(tmp_path / "deploy" / "dev-ecs.generated.yml")},
        {"path": "ci/buildspec-infra_plan.yml", "kind": "buildspec", "stored_at": str(spec)},
    ]
    argvs = provider.would_run(stage, records, str(tmp_path), answers)
    assert argvs[0][:3] == ["aws", "codebuild", "start-build"] and "--buildspec-override" in argvs[0]
    assert argvs[0][argvs[0].index("--buildspec-override") + 1] == f"file://{spec}"
    without = provider.would_run(stage, records[:1], str(tmp_path), answers)
    assert without[0][:3] == ["aws", "cloudformation", "deploy"]


def test_a_failed_build_brings_its_log_tail(tmp_path, monkeypatch):
    provider = AwsProvider()
    seen: list[list[str]] = []

    def fake_json(argv, cwd, timeout=120):
        seen.append(list(argv))
        return 0, {"events": [{"message": "Running command pytest -q"}, {"message": "FAILED tests/test_x.py::test_y"}, {"message": ""}]}, ""

    monkeypatch.setattr(provider, "_aws_json", fake_json)
    tail = provider._log_tail({"groupName": "/aws/codebuild/p", "streamName": "abc"}, "us-east-1", str(tmp_path))
    assert tail == "Running command pytest -q\nFAILED tests/test_x.py::test_y"
    assert seen[0][:3] == ["aws", "logs", "get-log-events"] and "--no-start-from-head" in seen[0] and seen[0][-2:] == ["--region", "us-east-1"]
    assert provider._log_tail({}, "us-east-1", str(tmp_path)) == ""


def test_deploy_defaults_fill_only_unset_names_and_reach_the_project_environment(monkeypatch):
    for name in ("AWS_CFN_STACK", "AWS_ECS_CLUSTER", "AWS_ECS_SERVICE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ECS_SERVICE", "my-service")
    defaults = AwsProvider.deploy_defaults("dev")
    assert defaults == {"AWS_CFN_STACK": "langgraph-support-app-dev", "AWS_ECS_CLUSTER": "langgraph-support-app-dev", "AWS_ECS_SERVICE": "langgraph-support-app-dev"}
    import os

    assert os.environ["AWS_CFN_STACK"] == "langgraph-support-app-dev" and os.environ["AWS_ECS_SERVICE"] == "my-service"
    rendered = templates.render_template("codebuild_project", {"name": "pipeline"}, {"tier": "dev", "platform": "aws", "region": "us-east-1"}, runner="aws_codebuild", run_id="r1")
    document = json.loads(rendered)
    names = {item["name"] for item in document["environment"]["environmentVariables"]}
    assert {"AWS_CFN_STACK", "AWS_ECS_CLUSTER", "AWS_ECS_SERVICE", "AWS_S3_BUCKET", "AWS_ECR_REPOSITORY", "AWS_ACCOUNT_ID"} <= names
    assert document["artifacts"] == {"type": "NO_ARTIFACTS"}
