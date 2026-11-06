"""Live run 23 (2026-11-06): the build contract passed, and the fixed build stage still failed
twice because Astra's buildspec for it checked a summary file the contract never writes in its
post_build phase. The run 17 rule (a floored stage's buildspec is the skeleton calling the contract,
nothing else) had left the fixed stages' buildspecs to the model. Now every CodeBuild buildspec is
the orchestrator's skeleton calling the stage's contract: the fixed stages through the suite's
contract check, and every generated stage with a contract through the floor."""

from __future__ import annotations

import yaml

from orchestrator import generator, llm, suite, templates
from orchestrator.schemas import GeneratedArtifact, PipelineSuite

ENV = {"tier": "dev", "platform": "aws", "run_id": "r23", "region": "us-east-1"}
STAGES = [
    {"name": "build", "family": "build", "purpose": "compile", "depends_on": []},
    {"name": "scanners", "family": "scan", "purpose": "scan", "depends_on": ["build"]},
    {"name": "test", "family": "test", "purpose": "unit tests", "depends_on": ["build"]},
]
ANSWERS = {"scan_tools": "semgrep, pip-audit", "dast_tools": "zap"}
RUN_23_BUILD = (
    "version: 0.2\n\nenv:\n  shell: bash\n  variables:\n    RUN_ID: \"r23\"\n    TIER: \"dev\"\n    STAGE: \"build\"\n    PLATFORM: \"aws\"\n"
    "    APP_NAME: \"langgraph-support-app\"\n\nphases:\n  install:\n    runtime-versions:\n      python: 3.12\n    commands:\n"
    "      - case \"$TIER\" in production|prod) echo \"No path past UAT.\"; exit 2;; esac\n  build:\n    commands:\n      - bash ci/build.sh\n"
    "  post_build:\n    commands:\n      - |\n        if [ -s var/build/summary.json ]; then\n          cat var/build/summary.json\n        else\n"
    "          echo failed\n          exit 1\n        fi\n"
)


def _commands(content: str, phase: str = "build") -> list[str]:
    phases = yaml.safe_load(content)["phases"]
    return list((phases.get(phase) or {}).get("commands") or [])


def test_a_fixed_stage_buildspec_is_the_skeleton_calling_its_contract():
    skeletons = suite.render_skeletons("aws", STAGES, ENV, run_id="r23", placeholders=["AWS_REGION"], answers=ANSWERS)
    model = PipelineSuite(platform="aws", runner="aws_codebuild", notes="model", artifacts=[
        GeneratedArtifact(path="ci/buildspec-build.yml", kind="buildspec", purpose="model", content=RUN_23_BUILD),
        GeneratedArtifact(path="ci/buildspec-scanners.yml", kind="buildspec", purpose="model", content=skeletons["ci/buildspec-scanners.yml"].replace("bash ci/scanners.sh", "bash ci/scanners.sh && echo scanned")),
    ])
    restored = suite.assert_contract(model, "aws", STAGES, ENV, ["AWS_REGION"], ANSWERS, run_id="r23")
    by_path = {item.path: item for item in model.artifacts}
    build = by_path["ci/buildspec-build.yml"].content
    assert _commands(build) == ["bash ci/build.sh"]
    assert "summary.json" not in build and _commands(build, "post_build") == [c for c in _commands(skeletons["ci/buildspec-build.yml"], "post_build")]
    assert _commands(by_path["ci/buildspec-scanners.yml"].content) == ['SCAN_TOOLS="semgrep,pip-audit" SCAN_SEVERITY="high" bash ci/scanners.sh'] or _commands(by_path["ci/buildspec-scanners.yml"].content) == _commands(skeletons["ci/buildspec-scanners.yml"])
    assert "echo scanned" not in by_path["ci/buildspec-scanners.yml"].content
    assert "ci/buildspec-test.yml" in by_path and _commands(by_path["ci/buildspec-test.yml"].content) == ["bash ci/test.sh"]  # a missing stage file is created
    assert any(entry.startswith("ci/buildspec-build.yml: bash ci/build.sh") for entry in restored)
    assert suite.check(model).ok
    again = suite.assert_contract(model, "aws", STAGES, ENV, ["AWS_REGION"], ANSWERS, run_id="r23")
    assert not any(entry.startswith("ci/buildspec-") for entry in again)  # idempotent despite the generated-at header


def test_every_generated_codebuild_stage_with_a_contract_is_floored():
    assert generator.floor_for({"name": "integration_test", "family": "test"}) == "test"
    assert generator.floor_for({"name": "frontend_build", "family": "build"}) == "build"
    assert generator.floor_for({"name": "build_image", "family": "build"}) == "container"  # the image stages keep their contract
    assert generator.floor_for({"name": "sca", "family": "scan", "provider": "pip_audit"}) == "scanners"
    assert generator.floor_for({"name": "secret_scan", "family": "scan", "provider": "gitleaks"}) == "scanners"
    assert generator.floor_for({"name": "sbom", "family": "scan"}) is None  # no provider the contract knows: the model's file stays
    assert generator.floor_for({"name": "release", "family": "release"}) == "release"
    assert generator.floor_for({"name": "bundle", "family": "package"}) == "package"
    spec = GeneratedArtifact(path="ci/buildspec-integration_test.yml", kind="buildspec", purpose="model", content="version: 0.2\nenv:\n  shell: bash\nphases:\n  build:\n    commands:\n      - bash ci/integration_test.sh\n      - echo tested\n")
    script = GeneratedArtifact(path="ci/integration_test.sh", kind="shell", purpose="model", content="#!/usr/bin/env bash\nset -euo pipefail\npytest -m integration\n")
    stage = {"name": "integration_test", "family": "test", "runner": "aws_codebuild", "environment": ENV}
    floored = generator.platform_floor([spec, script], stage, run_id="r23")
    assert _commands(next(item for item in floored if item.kind == "buildspec").content) == ["bash ci/test.sh"]
    assert next(item for item in floored if item.kind == "shell").content == script.content  # the model's script stays as evidence
    sca = {"name": "sca", "family": "scan", "provider": "pip_audit", "runner": "aws_codebuild", "environment": ENV}
    scoped = generator.platform_floor([spec.model_copy(update={"path": "ci/buildspec-sca.yml"})], sca, run_id="r23")
    assert _commands(scoped[0].content) == ['SCAN_TOOLS="pip-audit" bash ci/scanners.sh']
    local = generator.platform_floor([spec], {**stage, "runner": "local"}, run_id="r23")
    assert local[0].content == spec.content


def test_prompts_say_every_codebuild_buildspec_is_the_skeleton():
    assert "every stage's buildspec is the orchestrator's skeleton calling" in llm.GENERATE_SYSTEM
    assert "each buildspec is the orchestrator's skeleton" in llm.SUITE_SYSTEM
    assert templates.contract_path("test") == "ci/test.sh"
