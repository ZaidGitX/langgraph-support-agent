"""Sprint 4 scan and test menu: the answers choose the providers, the catalog says how each runs,
the contract scripts honor the selection on every platform, and Astra sees the same menu."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from orchestrator import generator, llm, profile, scanners, suite, templates
from orchestrator.providers.aws import AwsProvider
from orchestrator.providers.local import LocalProvider
from orchestrator.schemas import GeneratedArtifact, PipelineSuite
from orchestrator.state import initial_state
from tests.test_generator import STAGE, _state

ROOT = Path(__file__).resolve().parent.parent
STAGES = [
    {"name": "build", "family": "build", "purpose": "compile", "depends_on": []},
    {"name": "scanners", "family": "scan", "purpose": "scan", "depends_on": ["build"]},
    {"name": "dast", "family": "verify", "purpose": "dynamic scan", "depends_on": ["build"]},
]
ANSWERS = {"scan_tools": "Semgrep, pip-audit and Trivy please", "dast_tools": "zap", "scan_policy": "fail on high and critical"}
SELECTED_CALL = 'SCAN_TOOLS="semgrep,pip-audit,trivy" SCAN_SEVERITY="HIGH,CRITICAL" bash ci/scanners.sh'


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    env.update({"TIER": "dev", "RUN_ID": "t9", "PLATFORM": "local", "AWS_REGION": "us-east-1"})
    return env


def test_selection_reads_names_aliases_all_and_none():
    chosen = scanners.selection_from_answers({**ANSWERS, "dast_tools": "none", "scan_policy": "fail on medium and above"})
    assert chosen == {"scan_tools": ["semgrep", "pip_audit", "trivy"], "dast_tools": [], "severity": "MEDIUM,HIGH,CRITICAL"}
    assert scanners.selection_env({"scan_tools": "sonarqube and codeql", "dast_tools": "stackhawk"}) == {
        "SCAN_TOOLS": "sonar,codeql", "DAST_TOOLS": "stackhawk", "SCAN_SEVERITY": "HIGH,CRITICAL",
    }
    everything = scanners.selection_from_answers({})
    assert everything["scan_tools"] == list(scanners.SCAN_STAGE_TOOLS) and everything["dast_tools"] == list(scanners.DAST_STAGE_TOOLS)
    assert scanners.selection_from_answers({"scan_tools": "all of them"})["scan_tools"] == list(scanners.SCAN_STAGE_TOOLS)
    assert scanners.selection_env({"scan_tools": "none", "dast_tools": "no"}) == {"SCAN_TOOLS": "none", "DAST_TOOLS": "none", "SCAN_SEVERITY": "HIGH,CRITICAL"}
    assert scanners.severity_from("critical only") == "CRITICAL" and scanners.severity_from("") == "HIGH,CRITICAL"


def test_catalog_rows_for_astra_cover_every_workflow_provider_by_name_only():
    rows = {row["tool"]: row for row in scanners.catalog_for_prompt()}
    for key in ("semgrep", "pip_audit", "snyk", "trivy", "gitleaks", "sonar", "codeql", "zap", "stackhawk", "pytest"):
        assert key in rows, key
    assert rows["sonar"]["skip_without"] == ["SONAR_TOKEN", "SONAR_ORGANIZATION", "SONAR_PROJECT_KEY"]
    assert rows["codeql"]["argv"]["analyze"][0] == "codeql" and "codeql-bundle" in rows["codeql"]["install"]
    assert rows["stackhawk"]["argv"]["dast"][0] == "docker" and rows["stackhawk"]["skip_without"] == ["HAWK_API_KEY", "HAWK_APP_ID"]
    assert rows["zap"]["argv"]["dast"][0] == "docker" and "-J" in rows["zap"]["argv"]["dast"]
    assert rows["trivy"]["argv"]["scanners"][0] == "docker" and "$SCAN_SEVERITY" in rows["trivy"]["argv"]["scanners"]
    dumped = json.dumps(rows)
    assert "fakevalue" not in dumped and "HAWK_API_KEY" in dumped  # names, never a value
    assert "hadolint" not in rows  # lint-only tools are not scan or test providers


def test_contract_scripts_honor_the_selection(tmp_path):
    scan = templates.contract_script("scanners")
    assert 'SCAN_TOOLS:=all' in scan and "not selected" in scan
    for tool in ("semgrep", "pip-audit", "snyk", "trivy", "gitleaks", "sonar", "codeql"):
        assert f"selected {tool}" in scan, tool
    dast = templates.contract_script("dast")
    assert 'DAST_TOOLS:=all' in dast and "selected zap" in dast and "selected stackhawk" in dast
    (tmp_path / "ci").mkdir()
    for key, text in (("scanners", scan), ("dast", dast)):
        (tmp_path / "ci" / f"{key}.sh").write_text(text)
        syntax = subprocess.run(["bash", "-n", f"ci/{key}.sh"], cwd=tmp_path, check=False, capture_output=True, text=True)
        assert syntax.returncode == 0, syntax.stderr
    # Nothing selected: every provider is recorded as skipped, nothing is installed, the stage passes.
    run = subprocess.run(["bash", "ci/scanners.sh"], cwd=tmp_path, env={**_env(), "SCAN_TOOLS": "none"}, check=False, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
    ledger = [json.loads(line) for line in (tmp_path / "var/scanners/status.jsonl").read_text().splitlines() if line.strip()]
    assert len(ledger) == 9 and {row["status"] for row in ledger} == {"skipped"} and {row["reason"] for row in ledger} == {"not selected"}
    summary = json.loads(run.stdout.strip().splitlines()[-1])
    assert summary["status"] == "passed" and summary["failed"] == 0 and summary["skipped"] == 9 and summary["selected"] == "none"
    dast_run = subprocess.run(["bash", "ci/dast.sh"], cwd=tmp_path, env={**_env(), "DAST_TOOLS": "none"}, check=False, capture_output=True, text=True, timeout=60)
    assert dast_run.returncode == 0 and '"zap":"skipped"' in dast_run.stdout and "no dynamic scan selected" in dast_run.stdout


def test_command_hints_carry_the_selection_on_every_platform():
    assert suite.command_hints("scanners", "aws", ANSWERS) == [SELECTED_CALL]
    assert suite.command_hints("dast", "aws", ANSWERS) == ['DAST_TOOLS="zap" bash ci/dast.sh']
    assert suite.command_hints("build", "aws", ANSWERS) == ["bash ci/build.sh"]
    assert suite.command_hints("scanners", "jenkins") == ["bash ci/scanners.sh"]  # unanswered: the script runs every tool
    for platform in ("jenkins", "gcp", "azure", "aws"):
        rendered = suite.render_skeletons(platform, STAGES, {"tier": "dev", "platform": platform}, run_id="r9", placeholders=["AWS_REGION"], answers=ANSWERS)
        text = "\n".join(rendered.values())
        assert SELECTED_CALL in text, platform
    aws = suite.render_skeletons("aws", STAGES, {"tier": "dev", "platform": "aws"}, run_id="r9", placeholders=["AWS_REGION"], answers=ANSWERS)
    assert 'DAST_TOOLS="zap" bash ci/dast.sh' in aws["ci/buildspec-dast.yml"]
    spec = yaml.safe_load(aws["ci/buildspec-scanners.yml"])  # the quoted selection keeps the buildspec valid YAML
    assert SELECTED_CALL in spec["phases"]["build"]["commands"]


def test_assert_contract_restores_the_call_with_the_selection():
    skeletons = suite.render_skeletons("aws", STAGES, {"tier": "dev", "platform": "aws"}, run_id="r9", placeholders=["AWS_REGION"])
    dropped = skeletons["ci/buildspec-scanners.yml"].replace("      - bash ci/scanners.sh\n", "      - echo scanned\n")
    assert "bash ci/scanners.sh" not in dropped
    model = PipelineSuite(
        platform="aws", runner="aws_codebuild", notes="model output",
        artifacts=[GeneratedArtifact(path="ci/buildspec-scanners.yml", kind="buildspec", purpose="model", content=dropped)],
    )
    fixed = suite.with_manifest(model, {"tier": "dev", "platform": "aws"}, ["AWS_REGION"], platform="aws", stages=STAGES, answers=ANSWERS)
    content = {item.path: item.content for item in fixed.artifacts}["ci/buildspec-scanners.yml"]
    assert SELECTED_CALL in content and "echo scanned" in content


def test_astra_contexts_carry_the_menu(tmp_path):
    state = _state(str(tmp_path))
    state["answers"] = {"scan_tools": "codeql and sonarqube", "dast_tools": "stackhawk"}
    context = generator.context_for(state, {**STAGE, "name": "sast", "family": "scan"}, LocalProvider())
    assert context["scan_selection"]["scan_tools"] == ["sonar", "codeql"] and context["scan_selection"]["dast_tools"] == ["stackhawk"]
    assert {row["tool"] for row in context["scanner_catalog"]} >= {"codeql", "sonar", "stackhawk", "zap", "semgrep"}
    build = generator.context_for(state, {**STAGE, "name": "compile", "family": "build"}, LocalProvider())
    assert build["scanner_catalog"] == [] and build["scan_selection"]["scan_tools"] == ["sonar", "codeql"]
    cloud = initial_state(str(ROOT), run_id="r9")
    cloud["answers"] = dict(ANSWERS, env_platform="aws", env_target="dev")
    cloud["pipeline"] = {"stages": STAGES, "environment": {"tier": "dev", "platform": "aws"}}
    suite_context = suite.context_for(cloud, AwsProvider())
    assert suite_context["scan_selection"]["scan_tools"] == ["semgrep", "pip_audit", "trivy"]
    assert {row["tool"] for row in suite_context["scanner_catalog"]} >= {"codeql", "sonar", "stackhawk"}
    assert SELECTED_CALL in suite_context["skeletons"]["ci/buildspec-scanners.yml"]
    for prompt, words in ((llm.QUESTION_SYSTEM, ("scan_tools", "dast_tools")), (llm.GENERATE_SYSTEM, ("scanner_catalog", "scan_selection")), (llm.SUITE_SYSTEM, ("scan_selection", "SCAN_TOOLS", "scanner_catalog"))):
        for word in words:
            assert word in prompt, word


def test_profile_answers_the_scan_menu():
    loaded = profile.load(ROOT)
    assert "semgrep" in loaded.answers.by_id["scan_tools"] and loaded.answers.by_id["dast_tools"] == "zap"
    assert profile.answer_for(loaded, {"id": "scan_tools", "prompt": "Which scanners should run?"}) == loaded.answers.by_id["scan_tools"]
    assert profile.answer_for(loaded, {"id": "q7", "prompt": "Which DAST provider should run?"}) == "zap"
    assert scanners.selection_env(loaded.answers.by_id) == {"SCAN_TOOLS": "semgrep,pip-audit,snyk,trivy,gitleaks", "DAST_TOOLS": "zap", "SCAN_SEVERITY": "HIGH,CRITICAL"}
