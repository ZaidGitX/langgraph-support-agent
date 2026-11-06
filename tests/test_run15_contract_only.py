"""Before live run 15: on CodeBuild a generated container, deploy, plan, smoke, or dast stage runs
its contract and nothing beside it that could contradict it (live runs 12 to 14 each failed on a
model-written step next to a contract that already did the job)."""

from __future__ import annotations

import yaml

from orchestrator import generator, llm
from orchestrator.schemas import GeneratedArtifact

ENV = {"tier": "dev", "platform": "aws", "run_id": "r15"}


def _spec(name: str, *commands: str) -> GeneratedArtifact:
    body = "".join(f"      - {command}\n" for command in commands)
    return GeneratedArtifact(path=f"ci/buildspec-{name}.yml", kind="buildspec", purpose="model", content=f"version: 0.2\nenv:\n  shell: bash\nphases:\n  build:\n    commands:\n{body}")


def _commands(artifacts) -> list[str]:
    return yaml.safe_load(next(item for item in artifacts if item.kind == "buildspec").content)["phases"]["build"]["commands"]


def _stage(name: str, family: str, provider: str = "") -> dict:
    return {"name": name, "family": family, "provider": provider, "runner": "aws_codebuild", "environment": ENV}


def test_floor_keys_cover_every_late_wave_stage():
    assert generator.floor_for({"name": "dast_scan", "family": "scan"}) == "dast"
    assert generator.floor_for({"name": "push_image", "family": "container"}) == "container"
    assert generator.floor_for({"name": "post_deploy_smoke", "family": "verify"}) == "smoke"
    assert generator.floor_for({"name": "deploy_dev", "family": "deploy"}) == "deploy"
    assert generator.floor_for({"name": "infra_plan", "family": "deploy"}) == "deploy_plan"
    assert generator.floor_for({"name": "sca", "family": "scan"}) is None


def test_push_image_pushes_through_the_contract_only():
    spec = _spec("push_image", "aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $REGISTRY", "docker tag app:latest $IMAGE", "docker push $IMAGE", "bash ci/push_image.sh", "echo pushed")
    commands = _commands(generator.platform_floor([spec], _stage("push_image", "container")))
    assert "bash ci/container.sh" in commands and "echo pushed" in commands
    assert not any("docker push" in c or "docker tag" in c or "ci/push_image.sh" in c for c in commands)
    assert any("docker login" in c for c in commands)  # harmless steps stay


def test_smoke_and_dast_stages_use_the_contracts_that_know_the_url():
    smoke = _spec("post_deploy_smoke", "python ci/smoke_dev.generated.py", "curl -sf \"$APP_PUBLIC_URL/status\"", "echo smoke")
    commands = _commands(generator.platform_floor([smoke], _stage("post_deploy_smoke", "verify")))
    assert commands == ["echo smoke", "bash ci/smoke.sh"] or set(commands) == {"echo smoke", "bash ci/smoke.sh"}
    dast = _spec("dast_scan", "docker run --rm -t ghcr.io/zaproxy/zaproxy:stable zap-baseline.py -t \"$APP_URL\" -I", "bash ci/dast_scan.sh", "echo scanned")
    dast_commands = _commands(generator.platform_floor([dast], _stage("dast_scan", "scan", "zap")))
    assert "bash ci/dast.sh" in dast_commands and "echo scanned" in dast_commands
    assert not any("zap-baseline" in c or "ci/dast_scan.sh" in c for c in dast_commands)


def test_deploy_stage_drops_its_own_script_and_mutations():
    deploy = _spec("deploy_dev", "python ci/deploy_dev.generated.py --tier dev", "aws cloudformation deploy --stack-name x --template-file deploy/ecs-stack.yaml", "bash ci/deploy.sh")
    commands = _commands(generator.platform_floor([deploy], _stage("deploy_dev", "deploy")))
    assert commands == ["bash ci/deploy.sh"]
    clean = _spec("deploy_dev", "bash ci/deploy.sh")
    assert generator.platform_floor([clean], _stage("deploy_dev", "deploy"))[0].content == clean.content
    local = generator.platform_floor([deploy], {**_stage("deploy_dev", "deploy"), "runner": "local"})
    assert local[0].content == deploy.content


def test_the_deployed_task_never_autostarts_a_run():
    from orchestrator import templates

    stack = templates.render_template(
        "cloudformation", {"name": "pipeline", "family": "custom", "purpose": "stack", "environment": ENV}, ENV, runner="aws_codebuild", run_id="r15",
        placeholders=["AWS_REGION"], extra={"CONTAINER_ENV_LINES": "", "CONTAINER_SECRET_LINES": ""},
    )
    assert '{Name: ORCHESTRATOR_AUTOSTART, Value: "0"}' in stack
    assert yaml.safe_load(stack.replace("!Sub", "").replace("!Ref", "").replace("!If", "").replace("!GetAtt", "").replace("!Equals", "")) is not None


def test_prompt_names_every_late_wave_contract():
    for word in ("build_image and push_image stages run bash ci/container.sh", "smoke stages run bash ci/smoke.sh", "dast stages bash ci/dast.sh"):
        assert word in llm.GENERATE_SYSTEM, word
