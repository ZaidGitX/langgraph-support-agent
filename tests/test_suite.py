from __future__ import annotations

import json
import zipfile
from pathlib import Path

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
from orchestrator.schemas import CandidatePipeline, PlanProposal, StageSpec
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


class PlatformStageAstra(FakeAstra):
    """A stochastic candidate whose generated sbom stage is bound to the platform runner."""

    def propose(self, payload: dict) -> PlanProposal:
        stochastic = CandidatePipeline(
            id="platform-route",
            summary="build, test, sbom on CodeBuild, uat",
            mode="stochastic",
            stages=[
                StageSpec(name="build", purpose="compile"),
                StageSpec(name="test", depends_on=["build"], purpose="unit tests"),
                StageSpec(name="sbom", depends_on=["build"], purpose="software bill of materials", runner="aws_codebuild", provider="syft"),
                StageSpec(name="uat", depends_on=["test", "sbom"], purpose="sign-off"),
            ],
        )
        plain = CandidatePipeline(
            id="plain",
            summary="build, test, uat",
            stages=[
                StageSpec(name="build", purpose="compile"),
                StageSpec(name="test", depends_on=["build"], purpose="unit tests"),
                StageSpec(name="uat", depends_on=["test"], purpose="sign-off"),
            ],
        )
        return PlanProposal(candidates=[stochastic, plain])


class BadSuitePlatformStageAstra(PlatformStageAstra):
    """The same candidate, with a suite the policy rejects (a target past UAT, built at runtime)."""

    def generate_suite(self, payload: dict):
        built = super().generate_suite(payload)
        built.artifacts[0].content += f"\npython -m orchestrator.local_ops deploy --target {FORBIDDEN_TARGET}\n"
        return built


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
    # Every suite carries the orchestrator's contract scripts for the fixed stages (Sprint 4,
    # after live run 8): the platform-neutral ones everywhere, the AWS ones on AWS.
    neutral = {"ci/build.sh", "ci/test.sh", "ci/scanners.sh", "ci/package.sh"}
    aws_only = {"ci/container.sh", "ci/release.sh", "ci/deploy.sh", "ci/smoke.sh", "ci/dast.sh", "ci/image_scan.sh"}
    expected = {
        "jenkins": {"ci/pipeline.Jenkinsfile", "deploy/jenkins-job.xml", *neutral},
        "aws": {"ci/buildspec-build.yml", "ci/buildspec-test.yml", "ci/buildspec-scanners.yml", "ci/buildspec-deploy.yml", "deploy/codebuild-project.json", "deploy/ecs-stack.yaml", *neutral, *aws_only},
        "gcp": {"ci/cloudbuild.yaml", *neutral},
        "azure": {"ci/azure-pipelines.yml", *neutral},
    }
    for platform, paths in expected.items():
        rendered = suite.render_skeletons(platform, stages, {"tier": "dev", "platform": platform}, run_id="run-1", placeholders=["AWS_REGION", "SNYK_TOKEN"])
        assert set(rendered) == paths, platform
        for path, text in rendered.items():
            assert _policy_clean(path, text), path
            assert "$STAGE_BLOCKS" not in text and "$STAGE_COMMANDS" not in text
            if path in {"ci/pipeline.Jenkinsfile", "ci/cloudbuild.yaml", "ci/azure-pipelines.yml"}:
                assert "build" in text and "test" in text and "scanners" in text and "deploy" in text
    rendered = suite.render_skeletons("jenkins", stages, {}, run_id="r", placeholders=[])
    jenkinsfile = rendered["ci/pipeline.Jenkinsfile"]
    # The stage runs the orchestrator's contract script; the scanners live inside it.
    assert "params.STAGE == 'scanners'" in jenkinsfile and "bash ci/scanners.sh" in jenkinsfile
    assert "docker run --rm" in rendered["ci/scanners.sh"] and "semgrep" in rendered["ci/scanners.sh"] and "gitleaks" in rendered["ci/scanners.sh"]
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
    stored = next(record for record in values["generated_artifacts"]["pipeline"] if record["path"] == "ci/pipeline.Jenkinsfile")
    text = Path(stored["stored_at"]).read_text(encoding="utf-8")
    assert "credentials('" in text and "[redacted]" not in text  # by-reference bindings survive storage


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
        (GithubActionsProvider(), ["gh", "secret", "set", "SNYK_TOKEN"], "GitHub repository secrets"),
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


def test_provision_bundle_packs_every_generated_file_once_suite_first():
    generated = {
        "sbom": [{"path": "ci/sbom.sh", "kind": "shell"}, {"path": ".env.dev.example", "kind": "env_example", "note": "stage copy"}],
        "pipeline": [{"path": "ci/buildspec-build.yml", "kind": "buildspec"}, {"path": ".env.dev.example", "kind": "env_example", "note": "suite copy"}],
        "build_image": [{"path": "ci/build_image.sh", "kind": "shell"}, {"path": "", "kind": "shell"}],
    }
    bundle = suite.provision_bundle(generated)
    assert [item["path"] for item in bundle] == ["ci/buildspec-build.yml", ".env.dev.example", "ci/build_image.sh", "ci/sbom.sh"]
    assert next(item for item in bundle if item["path"] == ".env.dev.example")["note"] == "suite copy"


def test_prepare_provision_packs_the_bundle_into_source_zip(tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / "app").mkdir(parents=True)
    (checkout / "app" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (checkout / "node_modules").mkdir()
    (checkout / "node_modules" / "x.js").write_text("", encoding="utf-8")
    stored = tmp_path / "stored"
    stored.mkdir()
    (stored / "buildspec-build.yml").write_text("version: 0.2\n", encoding="utf-8")
    (stored / "sbom.sh").write_text("#!/usr/bin/env bash\necho sbom\n", encoding="utf-8")
    (stored / "project.json").write_text('{"name": "demo"}', encoding="utf-8")
    suite_records = [
        {"path": "ci/buildspec-build.yml", "kind": "buildspec", "stored_at": str(stored / "buildspec-build.yml")},
        {"path": "deploy/codebuild-project.json", "kind": "codebuild_project", "stored_at": str(stored / "project.json")},
    ]
    stage_record = {"path": "ci/sbom.sh", "kind": "shell", "stored_at": str(stored / "sbom.sh")}
    note = AwsProvider().prepare_provision(suite_records, str(checkout), {"run_id": "r1"}, bundle=[*suite_records, stage_record, dict(stage_record)])
    with zipfile.ZipFile(checkout / "var" / "generated" / "r1" / "pipeline" / "source.zip") as archive:
        names = set(archive.namelist())
    assert {"app/main.py", "ci/buildspec-build.yml", "deploy/codebuild-project.json", "ci/sbom.sh"} <= names
    assert not any(name.startswith("node_modules") for name in names)
    assert "3 generated file(s)" in note
    assert (stored / "project.rendered.json").is_file()


def test_generated_platform_stage_runs_on_the_platform_with_the_suite(tmp_path, monkeypatch):
    values = _run(tmp_path, "aws", monkeypatch, astra=PlatformStageAstra())
    assert values["status"] == "passed"
    runners = {stage["name"]: stage["runner"] for stage in values["pipeline"]["stages"]}
    assert runners["sbom"] == "aws_codebuild" and runners["build"] == "aws_codebuild" and runners["uat"] == "local"
    assert values["pipeline"]["suite"]["status"] == "ok"
    sbom = next(row for row in values["stage_results"] if row["stage"] == "sbom")
    assert sbom["status"] == "planned" and sbom["kind"] == "generated" and sbom["runner"] == "aws_codebuild"
    assert values["provision"]["status"] == "planned"
    assert "generated file(s) travel with the source" in values["provision"]["evidence"]
    paths = [item["path"] for item in suite.provision_bundle(values["generated_artifacts"])]
    assert "ci/sbom.sh" in paths and paths[0] in {record["path"] for record in values["generated_artifacts"]["pipeline"]}


def test_generated_platform_stage_falls_back_with_the_fixed_stages_when_the_suite_fails(tmp_path, monkeypatch):
    values = _run(tmp_path, "aws", monkeypatch, astra=BadSuitePlatformStageAstra())
    status = values["pipeline"]["suite"]["status"]
    assert status.startswith("suite rejected") and "on the host instead:" in status and "sbom" in status
    assert all(stage["runner"] == "local" for stage in values["pipeline"]["stages"])
    sbom = next(row for row in values["stage_results"] if row["stage"] == "sbom")
    assert sbom["status"] == "pass" and sbom["runner"] == "local" and "generated-ok" in sbom["evidence"]
    assert values["status"] == "passed"
    assert not (values.get("provision") or {}).get("commands")


def test_buildspec_binds_only_injectable_secrets_that_have_values(monkeypatch):
    names = [*AwsProvider().placeholders(), "SNYK_TOKEN"]
    stages = [{"name": "build"}, {"name": "scanners"}]
    environment = {"tier": "dev", "platform": "aws"}
    for name in names:
        monkeypatch.delenv(name, raising=False)
    rendered = suite.render_skeletons("aws", stages, environment, run_id="r1", placeholders=names)
    assert all("secrets-manager:" not in text for path, text in rendered.items() if path.startswith("ci/buildspec-"))
    assert suite.injectable_secrets(names, "aws") == []
    monkeypatch.setenv("SNYK_TOKEN", "fake-" + "snyk-value")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fake-" + "key-id")
    rendered = suite.render_skeletons("aws", stages, environment, run_id="r1", placeholders=names)
    scanners_spec = rendered["ci/buildspec-scanners.yml"]
    assert f'SNYK_TOKEN: "{templates.APP_NAME}/SNYK_TOKEN"' in scanners_spec
    assert "secrets-manager:" not in rendered["ci/buildspec-build.yml"]  # build runs no scanner
    assert f"{templates.APP_NAME}/AWS_ACCESS_KEY_ID" not in scanners_spec  # the platform's own identity is never bound
    assert suite.injectable_secrets(names, "aws") == ["SNYK_TOKEN"]


def test_referenced_secrets_reads_each_platform_binding_form():
    items = [
        {"content": f'    SNYK_TOKEN: "{templates.APP_NAME}/SNYK_TOKEN"\n'},
        {"content": "    HAWK_API_KEY = credentials('HAWK_API_KEY')\n"},
        {"content": "  - versionName: projects/p/secrets/SONAR_TOKEN/versions/latest\n"},
        {"content": "  value: $(GITLEAKS_LICENSE)\n"},
        {"content": "AWS_REGION=\nSNYK_TOKEN=\n"},
        {"content": f"{templates.APP_NAME}/AWS_REGION"},
    ]
    assert suite.referenced_secrets(items) == ["GITLEAKS_LICENSE", "HAWK_API_KEY", "SNYK_TOKEN", "SONAR_TOKEN"]


def test_suite_injection_covers_exactly_the_secrets_the_stage_files_bind(tmp_path, monkeypatch):
    values = _run(tmp_path, "aws", monkeypatch)
    records = values["generated_artifacts"]["pipeline"]
    # The stage files bind nothing; only the ECS task's Secrets block may name the app's own
    # keys, and only those present on this host (then injection carries exactly them).
    stack_names = {name for record in records if record["path"] == "deploy/ecs-stack.yaml" for name in record["secrets_referenced"]}
    assert records and all(record["secrets_referenced"] == [] for record in records if record["path"] != "deploy/ecs-stack.yaml")
    assert stack_names <= set(suite.APP_TASK_SECRETS)
    if stack_names:
        assert values["injection"]["status"] == "planned" and set(values["injection"]["names"]) == stack_names
    else:
        assert values["injection"]["status"] == "skipped" and values["injection"]["names"] == []


def test_aws_injection_creates_a_missing_secret_then_updates_it(tmp_path, monkeypatch):
    calls: list[tuple[list[str], str | None]] = []

    class Done:
        def __init__(self, code: int) -> None:
            self.returncode = code
            self.stdout = "{}"
            self.stderr = ""

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs.get("input")))
        if argv[:3] == ["aws", "secretsmanager", "describe-secret"]:
            seen = sum(1 for item, _ in calls if item[:3] == ["aws", "secretsmanager", "describe-secret"])
            return Done(255 if seen == 1 else 0)
        return Done(0)

    monkeypatch.setattr("orchestrator.providers.aws.subprocess.run", fake_run)
    monkeypatch.setattr("orchestrator.providers.base.subprocess.run", fake_run)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    secret_value = "fake-" + "snyk-value"
    monkeypatch.setenv("SNYK_TOKEN", secret_value)
    aws = AwsProvider()
    put = aws.inject_commands(["SNYK_TOKEN"], {})[0]
    assert aws.run_injection("SNYK_TOKEN", put, str(tmp_path)) is True
    assert aws.run_injection("SNYK_TOKEN", put, str(tmp_path)) is True
    executed = [argv for argv, _ in calls if argv[:2] == ["aws", "secretsmanager"] and argv[2] != "describe-secret"]
    assert executed[0][2] == "create-secret" and executed[0][executed[0].index("--name") + 1] == f"{templates.APP_NAME}/SNYK_TOKEN"
    assert executed[1][2] == "put-secret-value"
    assert all("file:///dev/stdin" in argv and "us-east-1" in argv for argv in executed)
    assert all(value == secret_value for argv, value in calls if argv[2] != "describe-secret")
    assert all(secret_value not in " ".join(argv) for argv, _ in calls)


def test_platform_setup_stops_the_run_when_injection_fails(tmp_path, monkeypatch):
    from orchestrator.providers import registry as provider_registry
    from orchestrator.providers.base import ProviderStatus, ProvisionResult

    class StubAws(AwsProvider):
        def detect(self):
            return ProviderStatus(runner=self.runner, cli=True, cli_path="/usr/bin/aws", present=["AWS_REGION"], missing=[], mode="apply")

        def provision(self, records, cwd, *, mode, answers, bundle=None):
            return ProvisionResult(self.runner, "provision", "pass" if mode == "apply" else "planned", mode, evidence="stub")

        def inject_secrets(self, names, *, mode, answers, cwd="."):
            status = "fail" if mode == "apply" else "planned"
            return ProvisionResult(self.runner, "inject", status, mode, names=["SNYK_TOKEN"], missing=["SNYK_TOKEN"], evidence="no value for SNYK_TOKEN")

        def execute(self, stage, records, cwd, *, mode, answers):
            raise AssertionError("no stage may start after a failed injection")

    # The stored readiness would say plan (no names on this host); the stub is the live answer.
    monkeypatch.setitem(provider_registry.PROVIDERS, "aws_codebuild", StubAws())
    monkeypatch.setattr(provider_registry, "status_from_readiness", lambda readiness, runner: None)
    monkeypatch.setattr("orchestrator.providers.status_from_readiness", lambda readiness, runner: None, raising=False)
    values = _run(tmp_path, "aws", monkeypatch)
    assert values["status"] == "escalated"
    assert "secret injection failed" in str((values.get("gate") or {}).get("rationale") or "")
    assert values["injection"]["status"] == "fail" and values["provision"]["status"] == "pass"
