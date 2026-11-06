"""Live run 19 (2026-11-06): the project document restored after run 18 was rendered with run id
"local", so the project's source pointed at orchestrator/local/source.zip while the upload went to
the run's folder, and both build attempts failed at DOWNLOAD_SOURCE (NoSuchKey). The contract
check renders with the run's id, and provisioning pins the document's name, source key, and
environment values from the run and the host whatever the stored file says."""

from __future__ import annotations

import json

from orchestrator import credentials, suite, templates
from orchestrator.providers.aws import AwsProvider
from orchestrator.schemas import GeneratedArtifact, PipelineSuite

ENV_WITHOUT_RUN_ID = {"tier": "dev", "platform": "aws", "region": "us-east-1"}
STAGES = [{"name": "build", "family": "build", "purpose": "compile", "depends_on": []}]
STALE = json.dumps({
    "name": "$AWS_CODEBUILD_PROJECT",
    "description": "Approved build stages, run local.",
    "source": {"type": "S3", "location": "$AWS_S3_BUCKET/orchestrator/local/source.zip", "buildspec": "ci/buildspec-build.yml"},
    "artifacts": {"type": "NO_ARTIFACTS"},
    "environment": {
        "type": "LINUX_CONTAINER", "image": "aws/codebuild/standard:7.0", "computeType": "BUILD_GENERAL1_MEDIUM", "privilegedMode": True,
        "environmentVariables": [{"name": "APP_NAME", "value": "app", "type": "PLAINTEXT"}, {"name": "AWS_REGION", "value": "", "type": "PLAINTEXT"}],
    },
    "serviceRole": "$AWS_CODEBUILD_ROLE_ARN",
    "timeoutInMinutes": 30,
    "logsConfig": {"cloudWatchLogs": {"status": "ENABLED"}},
}, indent=2)


def _model() -> PipelineSuite:
    return PipelineSuite(platform="aws", runner="aws_codebuild", artifacts=[GeneratedArtifact(path="deploy/codebuild-project.json", kind="codebuild_project", purpose="project", content=STALE)], notes="")


def _document(suite_obj: PipelineSuite) -> dict:
    return json.loads(next(item for item in suite_obj.artifacts if item.path == "deploy/codebuild-project.json").content)


def test_the_contract_check_renders_the_project_document_with_the_runs_id():
    fixed = suite.with_manifest(_model(), ENV_WITHOUT_RUN_ID, ["AWS_REGION"], platform="aws", stages=STAGES, run_id="r19")
    assert _document(fixed)["source"]["location"] == "$AWS_S3_BUCKET/orchestrator/r19/source.zip"
    assert "run r19" in _document(fixed)["description"]
    assert "run r19" in next(item for item in fixed.artifacts if item.path == "deploy/ecs-stack.yaml").content
    from_environment = _model()
    suite.assert_contract(from_environment, "aws", STAGES, {**ENV_WITHOUT_RUN_ID, "run_id": "r19b"}, [])
    assert _document(from_environment)["source"]["location"].endswith("/orchestrator/r19b/source.zip")
    from_answers = _model()
    suite.assert_contract(from_answers, "aws", STAGES, ENV_WITHOUT_RUN_ID, [], {"run_id": "r19c"})
    assert _document(from_answers)["source"]["location"].endswith("/orchestrator/r19c/source.zip")
    explicit = _model()
    suite.assert_contract(explicit, "aws", STAGES, {**ENV_WITHOUT_RUN_ID, "run_id": "other"}, [], {"run_id": "other"}, run_id="r19d")
    assert _document(explicit)["source"]["location"].endswith("/orchestrator/r19d/source.zip")  # the caller's id wins
    assert "/orchestrator/local/" not in _document(fixed)["source"]["location"]


def test_provisioning_pins_the_project_document_to_the_upload(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    (checkout / "app").mkdir(parents=True)
    (checkout / "app" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    stored = tmp_path / "stored"
    stored.mkdir()
    (stored / "project.json").write_text(STALE, encoding="utf-8")
    records = [{"path": "deploy/codebuild-project.json", "kind": "codebuild_project", "stored_at": str(stored / "project.json")}]
    for name in (*templates.CODEBUILD_ENV_NAMES, "AWS_CODEBUILD_PROJECT", "AWS_CODEBUILD_ROLE_ARN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_S3_BUCKET", "bucket-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ECR_REPOSITORY", "repo")
    monkeypatch.setenv("AWS_SUBNET_IDS", "subnet-a,subnet-b")
    provider = AwsProvider()
    provider.prepare_provision(records, str(checkout), {"run_id": "r19", "env_target": "dev"}, bundle=records)
    document = json.loads((stored / "project.rendered.json").read_text(encoding="utf-8"))
    assert document["name"] == f"{templates.APP_NAME}-orchestrator"
    assert document["source"] == {"type": "S3", "location": "bucket-1/orchestrator/r19/source.zip", "buildspec": "ci/buildspec-build.yml"}
    upload = provider.provision_commands(records, str(checkout), {"run_id": "r19"})[0]
    key = next(part for part in upload if part.startswith("s3://"))
    assert key.replace("s3://$AWS_S3_BUCKET/", "bucket-1/") == document["source"]["location"]  # the upload and the project agree
    variables = {item["name"]: item["value"] for item in document["environment"]["environmentVariables"]}
    assert variables == {"APP_NAME": templates.APP_NAME, "TIER": "dev", "AWS_REGION": "us-east-1", "AWS_S3_BUCKET": "bucket-1", "AWS_ECR_REPOSITORY": "repo", "AWS_SUBNET_IDS": "subnet-a,subnet-b"}
    assert all(item["type"] == "PLAINTEXT" for item in document["environment"]["environmentVariables"])
    assert document["environment"]["privilegedMode"] is True and document["timeoutInMinutes"] == 30 and document["serviceRole"] == ""
    assert not any(credentials.kind_of(name) == "secret" for name in variables)
    assert AwsProvider.pin_project_document("not json", "r19") == "not json"
    assert AwsProvider.pin_project_document("[1, 2]", "r19") == "[1, 2]"


def test_a_full_aws_run_stores_the_project_document_under_its_own_run_id(tmp_path, monkeypatch):
    from pathlib import Path

    from tests.test_suite import _run

    values = _run(tmp_path, "aws", monkeypatch)
    run_id = str(values["run_id"])
    assert run_id and run_id != "local"
    record = next(item for item in values["generated_artifacts"]["pipeline"] if item["path"] == "deploy/codebuild-project.json")
    document = json.loads(Path(record["stored_at"]).read_text(encoding="utf-8"))
    assert document["source"]["location"] == f"$AWS_S3_BUCKET/orchestrator/{run_id}/source.zip"
    assert [item["name"] for item in document["environment"]["environmentVariables"]] == list(templates.CODEBUILD_ENV_NAMES)
