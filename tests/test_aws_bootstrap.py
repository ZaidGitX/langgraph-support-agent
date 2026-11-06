"""The AWS provider creates what a platform run needs and is missing, with the vault credentials:
the person runs no bootstrap script (Sprint 4, after live run 2)."""

from __future__ import annotations

import json
import os

import pytest

from orchestrator.providers import aws as aws_module
from orchestrator.providers.base import ProviderStatus
from orchestrator.templates import APP_NAME

ACCOUNT = "123456789012"
NAMES = ("AWS_ACCOUNT_ID", "AWS_S3_BUCKET", "AWS_ECR_REPOSITORY", "AWS_CODEBUILD_PROJECT", "AWS_CODEBUILD_ROLE_ARN")


class FakeAws:
    """A scripted account: what exists, what gets created, every argv seen (no network)."""

    def __init__(self, *, bucket=False, repository=False, role=False, project=False, slow_role=False) -> None:
        self.exists = {"bucket": bucket, "repository": repository, "role": role, "project": project}
        self.slow_role = slow_role
        self.calls: list[list[str]] = []
        self.policy: dict = {}
        self.create_attempts = 0

    def __call__(self, argv, cwd, timeout=120):
        self.calls.append(list(argv))
        verb = argv[1:3]
        if verb == ["sts", "get-caller-identity"]:
            return 0, {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/tester"}, ""
        if verb == ["s3api", "head-bucket"]:
            return (0, {}, "") if self.exists["bucket"] else (1, None, "404 Not Found")
        if verb == ["s3api", "create-bucket"]:
            self.exists["bucket"] = True
            return 0, {"Location": "/" + argv[4]}, ""
        if verb == ["s3api", "put-public-access-block"]:
            return 0, {}, ""
        if verb == ["ecr", "describe-repositories"]:
            return (0, {"repositories": [{}]}, "") if self.exists["repository"] else (1, None, "RepositoryNotFoundException")
        if verb == ["ecr", "create-repository"]:
            self.exists["repository"] = True
            return 0, {"repository": {}}, ""
        if verb == ["iam", "get-role"]:
            if self.exists["role"]:
                return 0, {"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role/{argv[4]}"}}, ""
            return 1, None, "NoSuchEntity"
        if verb == ["iam", "create-role"]:
            self.exists["role"] = True
            return 0, {"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role/{argv[4]}"}}, ""
        if verb == ["iam", "put-role-policy"]:
            with open(argv[argv.index("--policy-document") + 1].removeprefix("file://"), encoding="utf-8") as handle:
                self.policy = json.load(handle)
            return 0, {}, ""
        if verb == ["codebuild", "batch-get-projects"]:
            return 0, {"projects": [{"name": argv[4]}] if self.exists["project"] else []}, ""
        if verb == ["codebuild", "create-project"]:
            self.create_attempts += 1
            if self.slow_role and self.create_attempts == 1:
                return 1, None, "An error occurred (InvalidInputException): CodeBuild is not authorized to perform: sts:AssumeRole on arn:aws:iam::123456789012:role/x"
            self.exists["project"] = True
            return 0, {"project": {"name": argv[4]}}, ""
        if verb == ["codebuild", "update-project"]:
            return 0, {"project": {}}, ""
        return 0, {}, ""


def _records(tmp_path) -> list[dict]:
    doc = tmp_path / "var" / "generated" / "r1" / "pipeline" / "deploy" / "codebuild-project.json"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text('{"name": "$AWS_CODEBUILD_PROJECT", "serviceRole": "$AWS_CODEBUILD_ROLE_ARN", "source": {"location": "$AWS_S3_BUCKET/orchestrator/r1/source.zip"}}', encoding="utf-8")
    return [{"path": "deploy/codebuild-project.json", "kind": "codebuild_project", "stored_at": str(doc), "secrets_referenced": []}]


@pytest.fixture
def provider(monkeypatch, tmp_path):
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    instance = aws_module.AwsProvider()
    monkeypatch.setattr(instance, "detect", lambda: ProviderStatus(runner="aws_codebuild", cli=True, cli_path="/usr/bin/aws", present=["AWS_REGION"], missing=[], mode="apply"))
    monkeypatch.setattr(aws_module.time, "sleep", lambda seconds: None)
    uploads: list[list[dict]] = []
    monkeypatch.setattr(aws_module, "run_commands", lambda commands, cwd: (uploads.append(commands) or {"status": "pass", "evidence": "uploaded", "exit_code": 0, "commands": commands}))
    instance.uploads = uploads
    (tmp_path / "README.md").write_text("checkout\n", encoding="utf-8")
    return instance


def test_a_fresh_account_gets_the_bucket_the_repository_the_role_and_the_project(provider, tmp_path):
    fake = FakeAws(slow_role=True)
    provider._aws_json = fake
    result = provider.provision(_records(tmp_path), str(tmp_path), mode="apply", answers={"run_id": "r1"}, bundle=None)
    assert result.status == "pass", result.evidence
    assert os.environ["AWS_ACCOUNT_ID"] == ACCOUNT
    assert os.environ["AWS_S3_BUCKET"] == f"langgraph-orchestrator-{ACCOUNT}"
    assert os.environ["AWS_ECR_REPOSITORY"] == APP_NAME
    assert os.environ["AWS_CODEBUILD_PROJECT"] == f"{APP_NAME}-orchestrator"
    assert os.environ["AWS_CODEBUILD_ROLE_ARN"] == f"arn:aws:iam::{ACCOUNT}:role/{APP_NAME}-orchestrator-codebuild-role"
    verbs = [tuple(call[1:3]) for call in fake.calls]
    for expected in (("s3api", "create-bucket"), ("s3api", "put-public-access-block"), ("ecr", "create-repository"), ("iam", "create-role"), ("iam", "put-role-policy"), ("codebuild", "create-project")):
        assert expected in verbs, expected
    assert ("codebuild", "update-project") not in verbs
    assert fake.create_attempts == 2, "the new role is retried until CodeBuild may assume it"
    assert "created: bucket langgraph-orchestrator-123456789012, repository langgraph-support-app, role langgraph-support-app-orchestrator-codebuild-role" in result.evidence
    assert "create-project langgraph-support-app-orchestrator: ok" in result.evidence
    rendered = json.loads((tmp_path / "var" / "generated" / "r1" / "pipeline" / "deploy" / "codebuild-project.rendered.json").read_text(encoding="utf-8"))
    assert rendered["serviceRole"] == os.environ["AWS_CODEBUILD_ROLE_ARN"] and rendered["name"] == f"{APP_NAME}-orchestrator"
    upload = provider.uploads[0]
    assert upload[0]["argv"][:3] == ["aws", "s3", "cp"] and upload[0]["argv"][4] == f"s3://langgraph-orchestrator-{ACCOUNT}/orchestrator/r1/source.zip"
    assert all("create-project" not in command["argv"] for command in upload)


def test_an_existing_platform_is_left_in_place_and_the_project_is_updated(provider, tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_S3_BUCKET", "my-bucket")
    monkeypatch.setenv("AWS_CODEBUILD_PROJECT", "my-project")
    fake = FakeAws(bucket=True, repository=True, role=True, project=True)
    provider._aws_json = fake
    result = provider.provision(_records(tmp_path), str(tmp_path), mode="apply", answers={"run_id": "r1"}, bundle=None)
    assert result.status == "pass", result.evidence
    verbs = [tuple(call[1:3]) for call in fake.calls]
    assert not any(verb[1].startswith("create") for verb in verbs)
    assert ("codebuild", "update-project") in verbs and ("iam", "put-role-policy") in verbs
    assert os.environ["AWS_CODEBUILD_ROLE_ARN"].endswith("role/my-project-codebuild-role")
    assert "bucket my-bucket exists" in result.evidence and "update-project my-project: ok" in result.evidence


def test_a_role_the_person_configured_is_not_touched(provider, tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_CODEBUILD_ROLE_ARN", f"arn:aws:iam::{ACCOUNT}:role/their-own-role")
    fake = FakeAws(bucket=True, repository=True)
    provider._aws_json = fake
    result = provider.provision(_records(tmp_path), str(tmp_path), mode="apply", answers={"run_id": "r1"}, bundle=None)
    assert result.status == "pass", result.evidence
    # The ECS service-linked role check (live run 21) is the one IAM call left: never the person's role.
    assert not any(call[1] == "iam" and call[2] != "create-service-linked-role" and aws_module.ECS_SERVICE_LINKED_ROLE not in call for call in fake.calls)
    assert os.environ["AWS_CODEBUILD_ROLE_ARN"].endswith("their-own-role")
    assert "their-own-role was configured by the person" in result.evidence


def test_a_failed_bootstrap_step_fails_provisioning_before_any_upload(provider, tmp_path):
    fake = FakeAws()

    def refusing(argv, cwd, timeout=120):
        if argv[1:3] == ["s3api", "create-bucket"]:
            return 1, None, "AccessDenied: not allowed to create buckets"
        return fake(argv, cwd, timeout)

    provider._aws_json = refusing
    result = provider.provision(_records(tmp_path), str(tmp_path), mode="apply", answers={"run_id": "r1"}, bundle=None)
    assert result.status == "fail" and "create-bucket" in result.evidence and "AccessDenied" in result.evidence
    assert not provider.uploads


def test_plan_mode_lists_the_commands_and_says_what_the_apply_would_create(provider, tmp_path, monkeypatch):
    monkeypatch.setattr(provider, "detect", lambda: ProviderStatus(runner="aws_codebuild", cli=True, cli_path="/usr/bin/aws", present=[], missing=["AWS_ACCESS_KEY_ID"], mode="plan"))
    fake = FakeAws()
    provider._aws_json = fake
    result = provider.provision(_records(tmp_path), str(tmp_path), mode="plan", answers={"run_id": "r1"}, bundle=None)
    assert result.status == "planned" and not fake.calls
    assert "created when absent" in result.evidence
    assert result.commands[-1][:3] == ["aws", "codebuild", "create-project"]


def test_the_role_policy_is_scoped_to_the_app(provider):
    policy = aws_module.role_policy(ACCOUNT, "us-east-1", bucket="b", repository="r", project="p")
    statements = policy["Statement"]
    iam = next(item for item in statements if any(action.startswith("iam:") for action in item["Action"]))
    assert iam["Resource"] == f"arn:aws:iam::{ACCOUNT}:role/{APP_NAME}-*"
    assert "iam:PassRole" in iam["Action"]
    wildcard_actions = {action for item in statements if item["Resource"] == "*" for action in item["Action"]}
    assert wildcard_actions <= {
        "ecr:GetAuthorizationToken", "cloudformation:ValidateTemplate", "cloudformation:ListStacks", "cloudformation:DescribeStacks",
        "ecs:RegisterTaskDefinition", "ecs:DeregisterTaskDefinition", "ecs:DescribeTaskDefinition", "ecs:ListTaskDefinitions", "ecs:ListClusters",
        "ecs:DescribeClusters", "ecs:DescribeServices", "ecs:ListServices", "ecs:ListTasks", "ecs:DescribeTasks", "ecs:TagResource",
        "ec2:Describe*", "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup", "ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress", "ec2:CreateTags",
    }
    assert any(item["Resource"] == ["arn:aws:s3:::b", "arn:aws:s3:::b/*"] for item in statements)
    assert not any("production" in json.dumps(item) for item in statements)
