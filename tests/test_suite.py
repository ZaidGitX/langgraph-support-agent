from __future__ import annotations

import json

import pytest
from langgraph.checkpoint.memory import MemorySaver

from orchestrator import policy, suite, templates
from orchestrator.demo import drive
from orchestrator.graph import build_graph, machine_report
from orchestrator.providers.aws import AwsProvider
from orchestrator.providers.azure import AzureProvider
from orchestrator.providers.gcp import GcpProvider
from orchestrator.providers.github_actions import GithubActionsProvider
from orchestrator.providers.jenkins import JenkinsProvider
from orchestrator.state import initial_state
from tests.test_orchestrator import FakeAstra, FakeJev, _echo

FORBIDDEN_TARGET = "prod" + "uction"  # the word itself must not sit in a generated file as a target


def _answers_for(platform: str):
    def resume(payload: dict) -> dict:
        gate = payload.get("gate")
        if gate in {"ecosystem_qa", "follow_up_qa"}:
            answers = {question["id"]: f"test-answer-{question['id']}" for question in payload.get("questions") or []}
            answers["env_platform"] = platform
            answers["env_target"] = "dev"
            return answers
        if gate in {"pipeline_approval", "uat_signoff"}:
            return {"decision": "approved"}
        raise AssertionError(f"unexpected gate {gate}")

    return resume


def _run(tmp_path, platform: str, monkeypatch, astra=None) -> dict:
    monkeypatch.setenv("ORCHESTRATOR_DISPATCH", "platform")
    graph = build_graph(MemorySaver(), jev=FakeJev(), astra=astra or FakeAstra(), commands=_echo())
    return drive(graph, initial_state(str(tmp_path)), _answers_for(platform), echo=False)


def _policy_clean(path: str, text: str) -> bool:
    """The real guarantee: every skeleton passes the policy (no target past UAT, no literal secret)."""
    verdict = policy.check_text(text, kind=policy.kind_for_path(path), path=path)
    return verdict.ok and policy.check_path(path).ok


def test_skeletons_cover_every_platform_and_stay_policy_clean():
    stages = [
        {"name": "build", "family": "build", "purpose": "compile", "depends_on": []},
        {"name": "test", "family": "test", "purpose": "unit tests", "depends_on": ["build"]},
        {"name": "scanners", "family": "scan", "purpose": "sast", "depends_on": ["build"]},
        {"name": "deploy", "family": "deploy", "purpose": "deploy", "depends_on": ["test"]},
    ]
    expected = {
        "jenkins": {"ci/pipeline.Jenkinsfile", "deploy/jenkins-job.xml"},
        "aws": {"ci/buildspec-build.yml", "ci/buildspec-test.yml", "ci/buildspec-scanners.yml", "ci/buildspec-deploy.yml", "deploy/codebuild-project.json", "deploy/ecs-stack.yaml"},
        "gcp": {"ci/cloudbuild.yaml"},
        "azure": {"ci/azure-pipelines.yml"},
    }
    for platform, paths in expected.items():
        rendered = suite.render_skeletons(platform, stages, {"tier": "dev", "platform": platform}, run_id="run-1", placeholders=["AWS_REGION", "SNYK_TOKEN"])
        assert set(rendered) == paths, platform
        for path, text in rendered.items():
            assert _policy_clean(path, text), path
            assert "$STAGE_BLOCKS" not in text and "$STAGE_COMMANDS" not in text
            if path in {"ci/pipeline.Jenkinsfile", "ci/cloudbuild.yaml", "ci/azure-pipelines.yml"}:
                assert "build" in text and "test" in text and "scanners" in text and "deploy" in text
    jenkinsfile = suite.render_skeletons("jenkins", stages, {}, run_id="r", placeholders=[])["ci/pipeline.Jenkinsfile"]
    assert "params.STAGE == 'scanners'" in jenkinsfile and "docker run --rm" in jenkinsfile
    raw = suite.render_skeletons("aws", stages, {}, run_id="r", placeholders=[])["deploy/codebuild-project.json"]
    project = json.loads(raw.replace("$AWS_CODEBUILD_PROJECT", "p").replace("$AWS_S3_BUCKET", "b").replace("$AWS_CODEBUILD_ROLE_ARN", "arn"))
    assert project["source"]["type"] == "S3" and project["serviceRole"] == "arn"
    cloudbuild = suite.render_skeletons("gcp", stages, {}, run_id="r", placeholders=[])["ci/cloudbuild.yaml"]
    assert "$${STAGE}" in cloudbuild and 'waitFor: ["build"]' in cloudbuild


def test_aws_platform_run_uses_the_suite_and_plans_without_credentials(tmp_path, monkeypatch):
    values = _run(tmp_path, "aws", monkeypatch)
    assert values["status"] == "passed"
    pipeline = values["pipeline"]
    runners = {stage["name"]: stage["runner"] for stage in pipeline["stages"]}
    assert runners["build"] == "aws_codebuild" and runners["test"] == "aws_codebuild" and runners["uat"] == "local"
    assert pipeline["suite"]["platform"] == "aws" and pipeline["suite"]["status"] == "ok"
    assert pipeline["suite"]["stage_files"]["build"] == "ci/buildspec-build.yml"
    records = values["generated_artifacts"]["pipeline"]
    paths = {record["path"] for record in records}
    assert {"ci/buildspec-build.yml", "ci/buildspec-test.yml", "deploy/codebuild-project.json", ".env.dev.example"} <= paths
    assert all(record["policy"]["ok"] for record in records)
    assert values["artifact_review"]["pipeline"]["verdict"] == "accept"
    build = next(row for row in values["stage_results"] if row["stage"] == "build")
    assert build["status"] == "planned" and build["runner"] == "aws_codebuild" and build["kind"] == "fixed"
    argv = build["plan"]["would_run"][0]
    assert argv[:3] == ["aws", "codebuild", "start-build"]
    assert "--buildspec-override" in argv and argv[argv.index("--buildspec-override") + 1].endswith("buildspec-build.yml")
    assert {"AWS_REGION", "AWS_ACCESS_KEY_ID"} <= set(build["plan"]["missing"])
    assert values["provision"]["status"] == "planned" and values["provision"]["action"] == "provision"
    commands = values["provision"]["commands"]
    assert commands[0][:3] == ["aws", "s3", "cp"] and commands[-1][:3] == ["aws", "codebuild", "create-project"]
    assert values["injection"]["status"] in {"planned", "skipped"}
    assert values["gate"]["route"] == "pass"
    report = machine_report(values)
    assert report["suite"]["platform"] == "aws" and report["provision"]["status"] == "planned"
    assert "build" in report["planned_stages"]


def test_jenkins_platform_run_triggers_one_job_per_stage(tmp_path, monkeypatch):
    values = _run(tmp_path, "jenkins", monkeypatch)
    assert values["status"] == "passed"
    assert values["pipeline"]["suite"]["stage_files"]["test"] == "ci/pipeline.Jenkinsfile"
    test = next(row for row in values["stage_results"] if row["stage"] == "test")
    assert test["runner"] == "jenkins" and test["status"] == "planned"
    argv = test["plan"]["would_run"][0]
    assert argv[0] == "curl" and "STAGE=test" in argv and "$JENKINS_URL/job/$JENKINS_JOB/buildWithParameters" in argv
    assert values["provision"]["commands"][0][-1] == "$JENKINS_URL/createItem?name=$JENKINS_JOB"


def test_local_dispatch_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_DISPATCH", "local")
    graph = build_graph(MemorySaver(), jev=FakeJev(), astra=FakeAstra(), commands=_echo())
    values = drive(graph, initial_state(str(tmp_path)), _answers_for("aws"), echo=False)
    assert all(stage["runner"] == "local" for stage in values["pipeline"]["stages"])
    assert "pipeline" not in values["generated_artifacts"] and not values.get("provision")
    assert values["status"] == "passed"


def test_suite_rejection_falls_back_to_the_local_host(tmp_path, monkeypatch):
    class BadSuiteAstra(FakeAstra):
        def generate_suite(self, payload: dict):
            built = super().generate_suite(payload)
            # A deploy line that names the forbidden target, built at runtime so the word never
            # sits in this file as a target.
            built.artifacts[0].content += f"\npython -m orchestrator.local_ops deploy --target {FORBIDDEN_TARGET}\n"
            return built

    values = _run(tmp_path, "aws", monkeypatch, astra=BadSuiteAstra())
    assert values["pipeline"]["suite"]["status"].startswith("suite rejected")
    assert all(stage["runner"] == "local" for stage in values["pipeline"]["stages"])
    assert values["artifact_review"]["pipeline"]["verdict"] in {"reject", "revise"}
    assert values["status"] == "passed"


@pytest.mark.parametrize(
    ("provider", "first", "target"),
    [
        (GithubActionsProvider(), ["gh", "secret", "set", "SNYK_TOKEN", "--body-file", "-"], "GitHub repository secrets"),
        (AwsProvider(), ["aws", "secretsmanager", "put-secret-value", "--secret-id", f"{templates.APP_NAME}/SNYK_TOKEN", "--secret-string", "file:///dev/stdin"], "AWS Secrets Manager"),
        (GcpProvider(), ["gcloud", "secrets", "versions", "add", "SNYK_TOKEN", "--data-file=-"], "Google Secret Manager"),
        (JenkinsProvider(), ["curl", "-sS", "-f", "-u", "$JENKINS_USER:$JENKINS_API_TOKEN"], "Jenkins credentials store"),
        (AzureProvider(), ["az", "pipelines", "variable", "create", "--name", "SNYK_TOKEN", "--secret", "true", "--value", "@-"], "Azure Pipelines variable group"),
    ],
)
def test_injection_plans_never_put_a_value_on_the_argv(provider, first, target, monkeypatch):
    monkeypatch.setenv("SNYK_TOKEN", "snyk-test-value-" + "0123456789")
    result = provider.inject_secrets(["SNYK_TOKEN", "NOT_A_NAME"], mode="plan", answers={}, cwd=".")
    assert result.status == "planned" and result.names == ["SNYK_TOKEN"]
    assert result.commands[0][: len(first)] == first
    assert result.target.startswith(target)
    assert "snyk-test-value" not in json.dumps(result.as_dict())


def test_provision_is_skipped_without_a_document_and_planned_with_one(tmp_path):
    aws = AwsProvider()
    assert aws.provision([], str(tmp_path), mode="plan", answers={}).status == "skipped"
    stored = tmp_path / "codebuild-project.json"
    stored.write_text("{}", encoding="utf-8")
    records = [{"path": "deploy/codebuild-project.json", "kind": "codebuild_project", "stored_at": str(stored)}]
    planned = aws.provision(records, str(tmp_path), mode="plan", answers={"run_id": "r1"})
    assert planned.status == "planned" and planned.commands[-1][:3] == ["aws", "codebuild", "create-project"]
    assert "r1/source.zip" in planned.commands[0][-3]
    assert not (tmp_path / "var").exists()  # plan mode zips nothing
