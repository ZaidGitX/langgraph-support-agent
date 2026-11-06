"""Live run 20 (2026-11-06): seven stages passed on CodeBuild at the first attempt and the deploy
stage ran for the first time. The stack rolled back because the redaction had turned
"task-execution" into "ta[redacted]" inside the stored stack (the OpenAI key pattern matched
"sk-execution"), and the retry could not update a ROLLBACK_COMPLETE stack. The key pattern needs a
boundary and a key's length, the orchestrator's own files survive redaction unchanged, and the
deploy contract creates with --on-failure DELETE so a failed create leaves nothing to trip on."""

from __future__ import annotations

import subprocess

from orchestrator import policy, redaction, suite, templates
from orchestrator.schemas import GeneratedArtifact

ENV = {"tier": "dev", "platform": "aws", "run_id": "r20", "region": "us-east-1"}
STAGES = [
    {"name": "build", "family": "build", "purpose": "compile", "depends_on": []},
    {"name": "deploy", "family": "deploy", "purpose": "deploy", "depends_on": ["build"]},
]


def test_the_key_pattern_needs_a_boundary_and_a_keys_length():
    line = 'RoleName: !Sub "${AppName}-${Tier}-task-execution"'
    assert redaction.redact(line) == line
    plain = "disk-encryption task-execution desk-lamp mask-sk-off"
    assert redaction.redact(plain) == plain
    assert redaction.redact("sk-short") == "sk-short"
    legacy = "sk-" + "A1b2" * 12
    project = "sk-proj-" + "Zz9_-" * 20
    assert redaction.redact(f"saw {legacy} here") == f"saw {redaction.MARK} here"
    assert redaction.redact(f"export OPENAI_API_KEY={project}").endswith(redaction.MARK)
    assert redaction.file_has_secret(f"key {legacy}") and not redaction.file_has_secret(line)
    assert redaction.file_has_secret("x " + "sk-" + "Q" * 24) and not redaction.file_has_secret("task-" + "sk-" + "Q" * 24)


def test_the_orchestrators_own_files_survive_redaction():
    stage = {"name": "pipeline", "family": "custom", "purpose": "the whole pipeline", "environment": ENV}
    texts = {key: templates.contract_script(key) for key in templates.CONTRACT_SCRIPTS}
    skeletons = suite.render_skeletons("aws", STAGES, ENV, run_id="r20", placeholders=["AWS_REGION"])
    texts.update(skeletons)
    texts["stack"] = templates.render_template(
        "cloudformation", stage, ENV, runner="aws_codebuild", run_id="r20", placeholders=["AWS_REGION"],
        extra={"CONTAINER_ENV_LINES": suite._container_env_lines(["AWS_REGION"]), "CONTAINER_SECRET_LINES": suite._container_secret_lines("aws")},
    )
    for name, text in texts.items():
        assert redaction.redact(text) == text, name
    assert "task-execution" in texts["stack"] and "task-execution" in skeletons["deploy/ecs-stack.yaml"]


def test_the_deploy_contract_creates_with_on_failure_delete_and_never_deletes(tmp_path):
    text = templates.contract_script("deploy")
    for marker in (
        "--on-failure DELETE", "wait stack-create-complete", "describe-stack-events", "ResourceStatus==`CREATE_FAILED`",
        "ROLLBACK_COMPLETE|ROLLBACK_FAILED|CREATE_FAILED|DELETE_FAILED|DELETE_IN_PROGRESS", "scripts/aws_teardown.sh",
        '"ParameterKey": "SubnetIds", "ParameterValue": "$subnets"', "aws cloudformation deploy --stack-name",
    ):
        assert marker in text, marker
    assert "delete-stack" not in text  # the guard on delete commands stays whole
    assert text.index('if [ "$DEPLOY_MODE" = "plan" ]') < text.index("--on-failure DELETE")  # the plan creates nothing
    assert text.index("ROLLBACK_COMPLETE|ROLLBACK_FAILED") < text.index("--on-failure DELETE") < text.index("aws cloudformation deploy --stack-name")
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci" / "deploy.sh").write_text(text, encoding="utf-8")
    assert subprocess.run(["bash", "-n", "ci/deploy.sh"], cwd=tmp_path, check=False, capture_output=True, text=True).returncode == 0
    result = policy.check_artifact(GeneratedArtifact(path="ci/deploy.sh", kind="shell", purpose="contract", content=text))
    assert result.ok, [f.rule for f in result.findings]
    forbidden = policy.check_artifact(GeneratedArtifact(path="ci/x.sh", kind="shell", purpose="p", content='#!/usr/bin/env bash\nset -euo pipefail\naws cloudformation delete-stack --stack-name "$STACK" --region us-east-1\n'))
    assert "destructive.aws" in {f.rule for f in forbidden.findings}
    assert policy.classify_command("aws cloudformation create-stack --stack-name s --on-failure DELETE")[0] == "ask"
