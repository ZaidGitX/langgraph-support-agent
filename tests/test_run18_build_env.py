"""Live run 18 (2026-11-06): build, sca, test, scanners, and integration_test passed at the first
attempt; build_image failed twice with "AWS_ECR_REPOSITORY is required". The model's project
document had kept nine environment names and dropped AWS_ECR_REPOSITORY, AWS_CFN_STACK,
AWS_ECS_CLUSTER, and AWS_ECS_SERVICE, and the skeleton-only buildspec declares none of them. The
project document is the orchestrator's now, and every start-build repeats the names from the host."""

from __future__ import annotations

import json

from orchestrator import credentials, llm, suite, templates
from orchestrator.providers import aws as aws_module
from orchestrator.providers.aws import (
    AwsProvider,
    build_env_override,
    resolve_build_env,
)
from orchestrator.schemas import GeneratedArtifact, PipelineSuite

ENV = {"tier": "dev", "platform": "aws", "run_id": "r18", "region": "us-east-1"}
STAGES = [
    {"name": "build", "family": "build", "purpose": "compile", "depends_on": []},
    {"name": "build_image", "family": "container", "purpose": "image", "depends_on": ["build"]},
]
MODEL_NAMES = ("APP_NAME", "RUN_ID", "PLATFORM", "TIER", "AWS_REGION", "AWS_ACCOUNT_ID", "AWS_S3_BUCKET", "AWS_VPC_ID", "AWS_SUBNET_IDS")
MODEL_PROJECT = json.dumps({
    "name": "$AWS_CODEBUILD_PROJECT",
    "description": "Approved build stages for the app, run r18. Privileged mode supports scanner containers.",
    "source": {"type": "S3", "location": "$AWS_S3_BUCKET/orchestrator/r18/source.zip", "buildspec": "ci/buildspec-build.yml"},
    "artifacts": {"type": "NO_ARTIFACTS"},
    "environment": {
        "type": "LINUX_CONTAINER", "image": "aws/codebuild/standard:7.0", "computeType": "BUILD_GENERAL1_MEDIUM", "privilegedMode": True,
        "environmentVariables": [{"name": name, "value": "r18" if name == "RUN_ID" else f"${name}", "type": "PLAINTEXT"} for name in MODEL_NAMES],
    },
    "serviceRole": "$AWS_CODEBUILD_ROLE_ARN",
    "timeoutInMinutes": 30,
}, indent=2)


def _names(content: str) -> list[str]:
    return [item["name"] for item in json.loads(content)["environment"]["environmentVariables"]]


def test_the_template_lists_every_build_name_and_no_secret():
    rendered = templates.render_template("codebuild_project", {"name": "pipeline"}, ENV, runner="aws_codebuild", run_id="r18")
    assert _names(rendered) == list(templates.CODEBUILD_ENV_NAMES)
    assert {"AWS_ECR_REPOSITORY", "AWS_CFN_STACK", "AWS_ECS_CLUSTER", "AWS_ECS_SERVICE", "AWS_VPC_ID", "AWS_SUBNET_IDS"} <= set(templates.CODEBUILD_ENV_NAMES)
    assert not any(credentials.kind_of(name) == "secret" for name in templates.CODEBUILD_ENV_NAMES)


def test_the_project_document_is_the_orchestrators():
    skeletons = suite.render_skeletons("aws", STAGES, ENV, run_id="r18", placeholders=["AWS_REGION"])
    artifacts = [
        GeneratedArtifact(path="ci/buildspec-build.yml", kind="buildspec", purpose="build", content=skeletons["ci/buildspec-build.yml"].replace('echo "stage build placeholder"', "bash ci/build.sh")),
        GeneratedArtifact(path="ci/buildspec-build_image.yml", kind="buildspec", purpose="image", content=skeletons["ci/buildspec-build_image.yml"].replace('echo "stage build_image placeholder"', "bash ci/container.sh")),
        GeneratedArtifact(path="deploy/codebuild-project.json", kind="codebuild_project", purpose="project", content=MODEL_PROJECT),
    ]
    suite_obj = PipelineSuite(platform="aws", runner="aws_codebuild", artifacts=artifacts, notes="model suite")
    assert _names(MODEL_PROJECT) == list(MODEL_NAMES)  # the model's list is missing four names
    restored = suite.assert_contract(suite_obj, "aws", STAGES, ENV, ["AWS_REGION"])
    assert "deploy/codebuild-project.json" in restored and "deploy/ecs-stack.yaml" in restored
    project = next(item for item in suite_obj.artifacts if item.path == "deploy/codebuild-project.json")
    assert project.kind == "codebuild_project" and _names(project.content) == list(templates.CODEBUILD_ENV_NAMES)
    assert json.loads(project.content)["environment"]["privilegedMode"] is True
    assert suite.assert_contract(suite_obj, "aws", STAGES, ENV, ["AWS_REGION"]) == []  # idempotent
    hardened = suite.with_manifest(PipelineSuite(platform="aws", runner="aws_codebuild", artifacts=list(artifacts), notes="model suite"), ENV, ["AWS_REGION"], platform="aws", stages=STAGES)
    assert _names(next(item for item in hardened.artifacts if item.path == "deploy/codebuild-project.json").content) == list(templates.CODEBUILD_ENV_NAMES)
    assert suite.check(hardened).ok


def test_every_start_build_repeats_the_names_from_the_host(tmp_path, monkeypatch):
    provider = AwsProvider()
    spec = tmp_path / "ci" / "buildspec-build_image.yml"
    spec.parent.mkdir()
    spec.write_text("version: 0.2\n", encoding="utf-8")
    records = [{"path": "ci/buildspec-build_image.yml", "kind": "buildspec", "stored_at": str(spec)}]
    stage = {"name": "build_image", "family": "container", "runner": "aws_codebuild", "environment": ENV}
    argv = provider.would_run(stage, records, str(tmp_path), {"run_id": "r18", "env_target": "dev"})[0]
    assert argv[:3] == ["aws", "codebuild", "start-build"] and argv[argv.index("--buildspec-override") + 1] == f"file://{spec}"
    position = argv.index("--environment-variables-override") + 1
    planned = {item["name"]: item["value"] for item in json.loads(argv[position])}
    assert set(planned) == {"RUN_ID", "PLATFORM", *templates.CODEBUILD_ENV_NAMES}
    assert planned["RUN_ID"] == "r18" and planned["TIER"] == "dev" and planned["PLATFORM"] == "aws" and planned["APP_NAME"] == templates.APP_NAME
    assert all(planned[name] == f"${name}" for name in templates.CODEBUILD_ENV_NAMES if name.startswith("AWS_"))  # never a value in the plan
    assert all(item["type"] == "PLAINTEXT" for item in json.loads(argv[position]))
    # The stage without a buildspec carries the same override.
    bare = provider.would_run({**stage, "provider": "aws_codebuild"}, [], str(tmp_path), {"run_id": "r18", "env_target": "dev"})[0]
    assert json.loads(bare[bare.index("--environment-variables-override") + 1]) == json.loads(argv[position])
    # At apply time the host values go in, commas intact; an unset name is dropped, a secret never appears.
    for name in templates.CODEBUILD_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCOUNT_ID", "1" * 12)
    monkeypatch.setenv("AWS_ECR_REPOSITORY", "langgraph-support-app")
    monkeypatch.setenv("AWS_CFN_STACK", "langgraph-support-app-dev")
    monkeypatch.setenv("AWS_SUBNET_IDS", "subnet-a,subnet-b,subnet-c")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "not-a-real-key-id")
    seen: list[list[str]] = []
    monkeypatch.setattr(provider, "_codebuild", lambda stage, argv, cwd: seen.append(list(argv)) or aws_module.StageRun(status="pass", exit_code=0, blocked=False, commands=[], checks=[], evidence="", plan={}))
    provider.apply(stage, [argv], str(tmp_path), records, {"run_id": "r18"})
    final = {item["name"]: item["value"] for item in json.loads(seen[0][position])}
    assert final["AWS_ECR_REPOSITORY"] == "langgraph-support-app" and final["AWS_CFN_STACK"] == "langgraph-support-app-dev"
    assert final["AWS_SUBNET_IDS"] == "subnet-a,subnet-b,subnet-c" and final["AWS_ACCOUNT_ID"] == "1" * 12 and final["RUN_ID"] == "r18"
    assert "AWS_VPC_ID" not in final and "AWS_ECS_CLUSTER" not in final and "AWS_ACCESS_KEY_ID" not in final
    assert not any(str(value).startswith("$") or not str(value).strip() for value in final.values())
    assert seen[0][:3] == ["aws", "codebuild", "start-build"] and seen[0][argv.index("--buildspec-override") + 1] == f"file://{spec}"


def test_resolve_build_env_leaves_other_argvs_alone():
    assert resolve_build_env(["aws", "ecs", "update-service"]) == ["aws", "ecs", "update-service"]
    assert resolve_build_env(["aws", "codebuild", "start-build", "--environment-variables-override"]) == ["aws", "codebuild", "start-build", "--environment-variables-override"]
    assert resolve_build_env(["x", "--environment-variables-override", "name=RUN_ID,value=r,type=PLAINTEXT"])[2] == "name=RUN_ID,value=r,type=PLAINTEXT"
    entries = json.loads(build_env_override("r18", "dev"))
    assert entries[0] == {"name": "RUN_ID", "value": "r18", "type": "PLAINTEXT"} and len(entries) == 2 + len(templates.CODEBUILD_ENV_NAMES)


def test_prompt_says_the_project_document_is_the_orchestrators():
    assert "deploy/codebuild-project.json is the" in llm.SUITE_SYSTEM and "environmentVariables list" in llm.SUITE_SYSTEM
