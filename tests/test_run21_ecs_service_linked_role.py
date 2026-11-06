"""Live run 21 (2026-11-06): seven stages passed at the first attempt and the deploy contract's new
create path did its job (a failed create removed itself and printed the reason): the stack's ECS
service could not be created because the account had no ECS service-linked role yet, and the
CodeBuild role was not allowed to create it. Provisioning creates that role once with the person's
credentials, and the CodeBuild role may create it as well."""

from __future__ import annotations

import pytest

from orchestrator.providers import aws as aws_module
from orchestrator.providers.base import ProviderStatus
from tests.test_aws_bootstrap import ACCOUNT, NAMES, FakeAws, _records


@pytest.fixture
def provider(monkeypatch, tmp_path):
    """The bootstrap tests' provider: no names set, apply mode, no sleeps, uploads faked."""
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    instance = aws_module.AwsProvider()
    monkeypatch.setattr(instance, "detect", lambda: ProviderStatus(runner="aws_codebuild", cli=True, cli_path="/usr/bin/aws", present=["AWS_REGION"], missing=[], mode="apply"))
    monkeypatch.setattr(aws_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(aws_module, "run_commands", lambda commands, cwd: {"status": "pass", "evidence": "uploaded", "exit_code": 0, "commands": commands})
    (tmp_path / "README.md").write_text("checkout\n", encoding="utf-8")
    return instance


class SlrAws(FakeAws):
    """The scripted account, with the ECS service-linked role tracked apart from the app roles."""

    def __init__(self, *, linked=False, linked_error="", **kwargs) -> None:
        super().__init__(**kwargs)
        self.linked = linked
        self.linked_error = linked_error

    def __call__(self, argv, cwd, timeout=120):
        verb = argv[1:3]
        if verb == ["iam", "get-role"] and argv[4] == aws_module.ECS_SERVICE_LINKED_ROLE:
            self.calls.append(list(argv))
            return (0, {"Role": {"RoleName": argv[4]}}, "") if self.linked else (1, None, "NoSuchEntity")
        if verb == ["iam", "create-service-linked-role"]:
            self.calls.append(list(argv))
            if self.linked_error:
                return 1, None, self.linked_error
            self.linked = True
            return 0, {"Role": {"RoleName": aws_module.ECS_SERVICE_LINKED_ROLE}}, ""
        return super().__call__(argv, cwd, timeout)


def _linked_calls(fake: SlrAws) -> list[list[str]]:
    return [call for call in fake.calls if call[1:3] == ["iam", "create-service-linked-role"]]


def test_provisioning_creates_the_ecs_service_linked_role_once(provider, tmp_path):
    fake = SlrAws(bucket=True, repository=True, role=True, project=True)
    provider._aws_json = fake
    result = provider.provision(_records(tmp_path), str(tmp_path), mode="apply", answers={"run_id": "r21"}, bundle=None)
    assert result.status == "pass", result.evidence
    assert _linked_calls(fake) == [["aws", "iam", "create-service-linked-role", "--aws-service-name", "ecs.amazonaws.com"]]
    assert f"created: service-linked role {aws_module.ECS_SERVICE_LINKED_ROLE}" in result.evidence
    verbs = [tuple(call[1:3]) for call in fake.calls]
    assert verbs.index(("iam", "create-service-linked-role")) < verbs.index(("codebuild", "update-project"))  # before the project, so the first build sees it
    again = SlrAws(bucket=True, repository=True, role=True, project=True, linked=True)
    provider._aws_json = again
    result = provider.provision(_records(tmp_path), str(tmp_path), mode="apply", answers={"run_id": "r21"}, bundle=None)
    assert result.status == "pass" and not _linked_calls(again) and "service-linked role" not in result.evidence


def test_a_role_that_cannot_be_created_is_a_note_not_a_failure(provider, tmp_path):
    fake = SlrAws(bucket=True, repository=True, role=True, project=True, linked_error="AccessDenied: iam:CreateServiceLinkedRole")
    provider._aws_json = fake
    result = provider.provision(_records(tmp_path), str(tmp_path), mode="apply", answers={"run_id": "r21"}, bundle=None)
    assert result.status == "pass", result.evidence
    assert f"service-linked role {aws_module.ECS_SERVICE_LINKED_ROLE} is missing and could not be created" in result.evidence
    assert "created: service-linked role" not in result.evidence


def test_the_codebuild_role_may_create_the_ecs_service_linked_role():
    policy = aws_module.role_policy(ACCOUNT, "us-east-1", bucket="b", repository="r", project="p")
    statements = [item for item in policy["Statement"] if "iam:CreateServiceLinkedRole" in item["Action"]]
    assert len(statements) == 1
    statement = statements[0]
    assert statement["Action"] == ["iam:CreateServiceLinkedRole"]
    assert statement["Resource"] == f"arn:aws:iam::{ACCOUNT}:role/aws-service-role/ecs.amazonaws.com/{aws_module.ECS_SERVICE_LINKED_ROLE}*"
    assert statement["Condition"] == {"StringLike": {"iam:AWSServiceName": "ecs.amazonaws.com"}}
    app_roles = next(item for item in policy["Statement"] if "iam:CreateRole" in item["Action"])
    assert {"iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:PassRole"} <= set(app_roles["Action"])
    assert app_roles["Resource"].endswith(":role/langgraph-support-app-*")
