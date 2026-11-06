"""Live run 15 (2026-11-06): build, test, sca, integration_test, and scanners passed on CodeBuild;
the generated build_image buildspec carried a shell quote escape inside a YAML single-quoted
scalar, CodeBuild rejected it at DOWNLOAD_SOURCE twice, and the run escalated. A YAML artifact
must parse (policy yaml.invalid) and a floored stage with an unparsable buildspec gets the skeleton."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

from orchestrator import generator, policy, templates
from orchestrator.schemas import GeneratedArtifact

ROOT = Path(__file__).resolve().parent.parent
ENV = {"tier": "dev", "platform": "aws", "run_id": "r15"}
BROKEN = (
    "version: 0.2\nenv:\n  shell: bash\nphases:\n  build:\n    commands:\n"
    "      - set -euo pipefail\n"
    "      - 'trap '\\''status=$?; if [ \"$status\" -ne 0 ]; then echo \"SUMMARY stage=build_image status=failed\"; fi'\\'' EXIT'\n"
    "      - bash ci/container.sh\n"
)


def _mirror():
    spec = importlib.util.spec_from_file_location("policy_rules", ROOT / ".claude/skills/sprint3-agentic-cicd/scripts/policy_rules.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_yaml_artifact_must_parse():
    result = policy.check_artifact(GeneratedArtifact(path="ci/buildspec-build_image.yml", kind="buildspec", purpose="p", content=BROKEN))
    rules = {f.rule: f for f in result.findings}
    assert not result.ok and "yaml.invalid" in rules and rules["yaml.invalid"].line == 8
    good = BROKEN.replace("'trap '\\''status=$?;", "'trap ''status=$?;").replace("fi'\\'' EXIT'", "fi'' EXIT'")
    assert yaml.safe_load(good)["phases"]["build"]["commands"][1].startswith("trap 'status=$?")
    assert "yaml.invalid" not in {f.rule for f in policy.check_artifact(GeneratedArtifact(path="ci/buildspec-build_image.yml", kind="buildspec", purpose="p", content=good)).findings}
    assert not policy.yaml_findings("not: [valid", "shell")  # only YAML kinds are parsed
    stack = templates.render_template(
        "cloudformation", {"name": "pipeline", "family": "custom", "purpose": "stack", "environment": ENV}, ENV, runner="aws_codebuild", run_id="r15",
        placeholders=["AWS_REGION"], extra={"CONTAINER_ENV_LINES": "", "CONTAINER_SECRET_LINES": ""},
    )
    assert not policy.yaml_findings(stack, "cloudformation")  # CloudFormation's own tags parse
    assert "yaml.invalid" in policy.rule_ids() and "yaml.invalid" in _mirror().rule_ids()
    assert any(line.startswith("yaml.invalid") for line in policy.rules_summary())


def test_a_floored_stage_with_an_unparsable_buildspec_gets_the_skeleton():
    stage = {"name": "build_image", "family": "container", "runner": "aws_codebuild", "environment": ENV}
    floored = generator.platform_floor([GeneratedArtifact(path="ci/buildspec-build_image.yml", kind="buildspec", purpose="model", content=BROKEN)], stage)
    spec = next(item for item in floored if item.kind == "buildspec")
    document = yaml.safe_load(spec.content)
    assert "bash ci/container.sh" in document["phases"]["build"]["commands"] and document["env"]["shell"] == "bash"
    assert "invalid YAML replaced" in spec.purpose and not policy.placeholder_findings(spec)
    assert policy.check_artifact(spec).ok
