"""Live run 12 (2026-11-06): build, test, integration_test, and scanners passed on CodeBuild; the
generated infra_plan stage ran its own `cloudformation deploy --no-execute-changeset` without the
stack's parameters and failed twice. The deploy contract now has a plan mode, infra stages on
CodeBuild use it, and a model-written stack mutation never runs beside the contract."""

from __future__ import annotations

import subprocess

import yaml

from orchestrator import generator, llm, policy, templates
from orchestrator.schemas import GeneratedArtifact

ENV = {"tier": "dev", "platform": "aws", "run_id": "r12"}
MODEL_PLAN = (
    "version: 0.2\nenv:\n  shell: bash\nphases:\n  build:\n    commands:\n"
    "      - aws cloudformation validate-template --template-body file://deploy/ecs-stack.yaml --region \"$AWS_REGION\"\n"
    "      - aws cloudformation deploy --region \"$AWS_REGION\" --template-file deploy/ecs-stack.yaml --stack-name \"$AWS_CFN_STACK\" --capabilities CAPABILITY_NAMED_IAM --no-execute-changeset\n"
    "      - echo planned\n"
)


def test_deploy_contract_has_a_plan_mode(tmp_path):
    text = templates.contract_script("deploy")
    for marker in (': "${DEPLOY_MODE:=apply}"', 'if [ "$DEPLOY_MODE" = "plan" ]', "validate-template", "image_in_ecr", '"mode\\":\\"plan\\"'):
        assert marker in text, marker
    assert text.index('if [ "$DEPLOY_MODE" = "plan" ]') < text.index("aws cloudformation deploy --stack-name")  # the plan returns before any mutation
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci" / "deploy.sh").write_text(text)
    assert subprocess.run(["bash", "-n", "ci/deploy.sh"], cwd=tmp_path, check=False, capture_output=True, text=True).returncode == 0
    assert policy.check_artifact(GeneratedArtifact(path="ci/deploy.sh", kind="shell", purpose="contract", content=text)).ok


def test_infra_stages_floor_to_the_deploy_contract():
    assert generator.floor_for({"name": "infra_plan", "family": "deploy"}) == "deploy_plan"
    assert generator.floor_for({"name": "infra_apply", "family": "deploy"}) == "deploy"
    assert generator.floor_for({"name": "deploy_dev", "family": "deploy"}) == "deploy"
    assert generator.floor_for({"name": "post_deploy_smoke", "family": "verify"}) == "smoke"
    assert generator.floor_for({"name": "sca", "family": "scan"}) is None
    stage = {"name": "infra_plan", "family": "deploy", "runner": "aws_codebuild", "environment": ENV}
    floored = generator.platform_floor([GeneratedArtifact(path="ci/buildspec-infra_plan.yml", kind="buildspec", purpose="plan", content=MODEL_PLAN)], stage)
    commands = yaml.safe_load(floored[0].content)["phases"]["build"]["commands"]
    assert "DEPLOY_MODE=plan bash ci/deploy.sh" in commands
    assert not any("cloudformation deploy" in command for command in commands)  # the model's change set is gone
    assert any("validate-template" in command for command in commands) and "echo planned" in commands  # harmless steps stay
    apply_stage = {"name": "infra_apply", "family": "deploy", "runner": "aws_codebuild", "environment": ENV}
    applied = generator.platform_floor([GeneratedArtifact(path="ci/buildspec-infra_apply.yml", kind="buildspec", purpose="apply", content=MODEL_PLAN)], apply_stage)
    apply_commands = yaml.safe_load(applied[0].content)["phases"]["build"]["commands"]
    assert apply_commands.count("bash ci/deploy.sh") == 1 and not any("cloudformation deploy" in c for c in apply_commands)
    clean = "version: 0.2\nenv:\n  shell: bash\nphases:\n  build:\n    commands:\n      - bash ci/deploy.sh\n"
    untouched = generator.platform_floor([GeneratedArtifact(path="ci/buildspec-deploy_dev.yml", kind="buildspec", purpose="deploy", content=clean)], {**apply_stage, "name": "deploy_dev"})
    assert untouched[0].content == clean
    local = generator.platform_floor([GeneratedArtifact(path="ci/buildspec-infra_plan.yml", kind="buildspec", purpose="plan", content=MODEL_PLAN)], {**stage, "runner": "local"})
    assert local[0].content == MODEL_PLAN  # the floor is CodeBuild's


def test_prompt_names_the_plan_mode():
    for word in ("DEPLOY_MODE=plan bash ci/deploy.sh", "infra_apply and deploy stages run bash ci/deploy.sh", "no cloudformation deploy"):
        assert word in llm.GENERATE_SYSTEM, word
