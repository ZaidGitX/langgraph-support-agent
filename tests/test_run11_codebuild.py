"""Live run 11 (2026-11-05): the first CodeBuild wave passed build and test; sca died under sh
("Illegal option -o pipefail"), integration_test treated pytest's "no tests collected" (exit 5)
as a failure, and scanners failed on trivy misconfigs in two Astra-written IaC files and one
synthetic test string. Every buildspec now runs bash, the catalog and prompt name the exit code,
a wildcard principal is a policy error, and the scanners script lists what failed."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import yaml

from orchestrator import generator, llm, policy, scanners, templates
from orchestrator.schemas import GeneratedArtifact

ROOT = Path(__file__).resolve().parent.parent
ENV = {"tier": "dev", "platform": "aws", "run_id": "r11"}


def _mirror():
    spec = importlib.util.spec_from_file_location("policy_rules", ROOT / ".claude/skills/sprint3-agentic-cicd/scripts/policy_rules.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_buildspec_runs_bash():
    for key in ("buildspec",):
        rendered = templates.render_template(key, {"name": "build", "family": "build", "purpose": "compile"}, ENV, runner="aws_codebuild", run_id="r11", placeholders=[])
        assert "env:\n  shell: bash\n" in rendered
    stage_spec = templates.render_template(
        "buildspec", {"name": "scanners", "family": "scan", "purpose": "scan"}, ENV, runner="aws_codebuild", run_id="r11", placeholders=[],
        extra={"STAGE_COMMANDS": "      - bash ci/scanners.sh", "WORKFLOW_FILE": "scanners.yml", "SECRETS_MANAGER_LINES": ""}, template=templates.SUITE_TEMPLATES["buildspec"],
    )
    assert yaml.safe_load(stage_spec)["env"]["shell"] == "bash"
    cases = {
        "no env": "version: 0.2\nphases:\n  build:\n    commands:\n      - bash ci/build.sh\n",
        "env without shell": "version: 0.2\nenv:\n  variables:\n    TIER: dev\nphases:\n  build:\n    commands:\n      - bash ci/build.sh\n",
        "shell sh": "version: 0.2\nenv:\n  shell: sh\n  variables:\n    TIER: dev\nphases:\n  build:\n    commands:\n      - set -euo pipefail\n",
    }
    for label, text in cases.items():
        hardened = generator.harden(GeneratedArtifact(path="ci/buildspec-sca.yml", kind="buildspec", purpose="p", content=text)).content
        document = yaml.safe_load(hardened)
        assert document["env"]["shell"] == "bash", label
        assert document["phases"]["build"]["commands"] == yaml.safe_load(text)["phases"]["build"]["commands"], label
    already = "version: 0.2\nenv:\n  shell: bash\nphases:\n  build:\n    commands:\n      - bash ci/build.sh\n"
    assert generator.harden(GeneratedArtifact(path="ci/buildspec-x.yml", kind="buildspec", purpose="p", content=already)).content == already


def test_a_test_level_with_no_tests_is_skipped_by_prompt_and_catalog():
    rows = {row["tool"]: row for row in scanners.catalog_for_prompt()}
    assert rows["pytest"]["exit_codes"]["5"].startswith("no tests collected")
    for word in ("exit code 5", "env.shell: bash", "non-root USER", "wildcard Principal", "validate-template"):
        assert word in llm.GENERATE_SYSTEM, word


def test_a_wildcard_principal_is_a_policy_error_in_both_copies():
    ecr_json = '{"Statement": [{"Effect": "Allow", "Principal": "*", "Action": "ecr:GetDownloadUrlForLayer"}]}\n'
    ecr_yaml = "RepositoryPolicyText:\n  Statement:\n    - Effect: Allow\n      Principal:\n        AWS: '*'\n      Action: ecr:BatchGetImage\n"
    for text, kind in ((ecr_json, "doc"), (ecr_yaml, "cloudformation")):
        rules = {f.rule for f in policy.check_text(text, kind=kind).findings}
        assert "iac.public_principal" in rules, text
        assert "iac.public_principal" in {f["rule"] for f in _mirror().check_text(text, kind=kind)}, text
    named = "Principal:\n  Service: ecs-tasks.amazonaws.com\nAWS: arn:aws:iam::123456789012:role/deployer\n"
    assert not [f for f in policy.check_text(named, kind="cloudformation").findings if f.rule == "iac.public_principal"]
    stack = templates.render_template(
        "cloudformation", {"name": "pipeline", "family": "custom", "purpose": "stack", "environment": ENV}, ENV, runner="aws_codebuild", run_id="r11",
        placeholders=["AWS_REGION"], extra={"CONTAINER_ENV_LINES": "", "CONTAINER_SECRET_LINES": ""},
    )
    result = policy.check_artifact(GeneratedArtifact(path="deploy/ecs-stack.yaml", kind="cloudformation", purpose="stack", content=stack))
    assert result.ok, [f.message for f in result.findings if f.severity == "error"]


def test_scanners_contract_lists_what_failed(tmp_path):
    text = templates.contract_script("scanners")
    for marker in ("trivy finding:", "gitleaks finding:", "semgrep finding:", "listed above"):
        assert marker in text, marker
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci" / "scanners.sh").write_text(text)
    assert subprocess.run(["bash", "-n", "ci/scanners.sh"], cwd=tmp_path, check=False, capture_output=True, text=True).returncode == 0
    assert policy.check_artifact(GeneratedArtifact(path="ci/scanners.sh", kind="shell", purpose="contract", content=text)).ok
