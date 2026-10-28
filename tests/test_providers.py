from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from orchestrator import catalog, scanners, templates
from orchestrator.credentials import runner_mode
from orchestrator.dispatch import execute_stage_v2, workflow_command
from orchestrator.findings import extract_result, tool_key
from orchestrator.providers import (
    LOCAL_RUNNERS,
    PROVIDERS,
    detect_all,
    expand_env,
    for_runner,
    status_from_readiness,
)
from orchestrator.providers.aws import AwsProvider
from orchestrator.providers.docker import DockerProvider
from orchestrator.providers.github_actions import GithubActionsProvider
from orchestrator.providers.jenkins import JenkinsProvider
from orchestrator.providers.kubernetes import KubernetesProvider
from orchestrator.providers.local import LocalProvider
from orchestrator.providers.ssh import SshProvider
from orchestrator.registry import RUNNERS

CLOUD_RUNNERS = [runner for runner in RUNNERS if runner not in LOCAL_RUNNERS]


def _stage(name: str, runner: str, provider: str = "", tier: str = "dev", platform: str = "aws") -> dict:
    return {
        "name": name,
        "family": "deploy",
        "kind": "generated",
        "runner": runner,
        "provider": provider,
        "environment": {"tier": tier, "platform": platform, "region": ""},
        "run_id": "run-42",
        "commands": [],
    }


def _script(tmp_path: Path, body: str = "echo generated-ok\n") -> list[dict]:
    path = tmp_path / "ci" / "stage.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    return [{"path": "ci/stage.sh", "kind": "shell", "stored_at": str(path)}]


@pytest.fixture
def no_subprocess(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("a plan must not start a subprocess")

    monkeypatch.setattr(subprocess, "run", boom)


def test_every_runner_has_a_provider_and_plans_without_credentials():
    assert set(PROVIDERS) == set(RUNNERS)
    statuses = detect_all()
    assert set(statuses) == set(RUNNERS)
    assert statuses["local"]["mode"] == "apply"
    for runner in CLOUD_RUNNERS:
        assert statuses[runner]["mode"] == "plan", runner
        assert isinstance(statuses[runner]["missing"], list)
    with pytest.raises(KeyError):
        for_runner("mainframe")


def test_runner_mode_rules():
    assert runner_mode("local", True) == "apply"
    assert runner_mode("docker", True) == "apply"
    assert runner_mode("aws_codebuild", True, env={}) == "plan"
    assert runner_mode("aws_codebuild", True, env={"ORCHESTRATOR_CLOUD_APPLY": "1"}) == "apply"
    assert runner_mode("github_actions", True, env={"ORCHESTRATOR_DISPATCH": "github"}) == "apply"
    assert runner_mode("github_actions", True, env={}) == "plan"
    assert runner_mode("jenkins", False, env={"ORCHESTRATOR_CLOUD_APPLY": "1"}) == "plan"


def test_aws_plans_with_missing_names_and_the_exact_command(tmp_path, no_subprocess):
    run = AwsProvider().execute(_stage("deploy_dev", "aws_codebuild"), [], str(tmp_path), mode="plan", answers={})
    assert run.status == "planned" and run.exit_code == 0 and run.blocked is False
    assert run.plan["runner"] == "aws_codebuild" and run.plan["mode"] == "plan"
    assert {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"} <= set(run.plan["missing"])
    assert run.plan["would_run"][0][:3] == ["aws", "codebuild", "start-build"]
    assert "$AWS_CODEBUILD_PROJECT" in run.plan["would_run"][0]
    assert run.checks[0]["status"] == "planned" and "Set " in run.checks[0]["next_step"]
    assert "production" not in str(run.as_dict())
    ecs = AwsProvider().would_run(_stage("deploy_dev", "aws_codebuild", provider="aws_ecs"), [], str(tmp_path), {})
    assert ecs[0][:3] == ["aws", "ecs", "update-service"]
    ecr = AwsProvider().would_run(_stage("push_image", "aws_codebuild", provider="ecr"), [], str(tmp_path), {})
    assert ecr[-1][:2] == ["docker", "push"] and "get-login-password" in " ".join(ecr[1])


def test_apply_is_refused_without_cloud_apply_even_when_asked(tmp_path, no_subprocess):
    run = AwsProvider().execute(_stage("deploy_dev", "aws_codebuild"), [], str(tmp_path), mode="apply", answers={})
    assert run.status == "planned"


def test_local_runs_the_stored_script(tmp_path):
    records = _script(tmp_path)
    run = LocalProvider().execute(_stage("container_scan", "local"), records, str(tmp_path), mode="plan", answers={})
    assert run.status == "pass" and "generated-ok" in run.evidence
    assert run.commands[0]["argv"][0] == "bash"
    empty = LocalProvider().execute(_stage("container_scan", "local"), [], str(tmp_path), mode="apply", answers={})
    assert empty.status == "fail" and "no generated script" in empty.evidence


def test_docker_plan_mounts_the_checkout(tmp_path, no_subprocess, monkeypatch):
    monkeypatch.setattr("orchestrator.providers.docker.daemon_reachable", lambda timeout=5.0: False)
    records = _script(tmp_path)
    provider = DockerProvider()
    argvs = provider.would_run(_stage("container_scan", "docker", platform="docker"), records, str(tmp_path), {})
    joined = " ".join(argvs[0])
    assert argvs[0][:3] == ["docker", "run", "--rm"] and f"{tmp_path.resolve()}:/work" in joined and joined.endswith("bash ci/stage.sh")
    run = provider.execute(_stage("container_scan", "docker", platform="docker"), records, str(tmp_path), mode="apply", answers={})
    assert run.status == "planned" and run.plan["would_run"][0][0] == "docker"
    scan = provider.would_run(_stage("container_scan", "docker", provider="trivy", platform="docker"), [], str(tmp_path), {})
    assert "aquasec/trivy" in " ".join(scan[0]) and "image" in scan[0] and "trivy" not in scan[0][scan[0].index("--rm") + 1:]


def test_kubernetes_dry_runs_in_plan_mode(tmp_path, monkeypatch):
    manifest = tmp_path / "deploy" / "k8s" / "deploy_dev.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("apiVersion: v1\nkind: Namespace\nmetadata:\n  name: demo\n", encoding="utf-8")
    calls: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = "namespace/demo created (dry run)\n"
        stderr = ""

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return Completed()

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/kubectl" if name == "kubectl" else None)
    monkeypatch.setattr(subprocess, "run", fake_run)
    records = [{"path": "deploy/k8s/deploy_dev.yaml", "kind": "kubernetes_manifest", "stored_at": str(manifest)}]
    run = KubernetesProvider().execute(_stage("deploy_dev", "kubernetes", platform="kubernetes"), records, str(tmp_path), mode="apply", answers={})
    assert run.status == "planned"
    assert calls and "--dry-run=client" in calls[0] and calls[0][:2] == ["kubectl", "apply"]
    assert run.plan["dry_run"]["exit_code"] == 0
    assert run.plan["would_run"][0][-2:] == ["-n", "$K8S_NAMESPACE"]


def test_github_actions_plan_names_the_branch_and_file(tmp_path, no_subprocess):
    provider = GithubActionsProvider()
    stage = _stage("container_scan", "github_actions", platform="github")
    assert provider.workflow_file(stage) == "gen-container_scan.yml"
    run = provider.execute(stage, [], str(tmp_path), mode="plan", answers={})
    assert run.status == "planned"
    assert "orchestrator/generated/run-42" in run.evidence and "gen-container_scan.yml" in run.evidence
    assert run.plan["would_run"][0][:4] == ["gh", "workflow", "run", "gen-container_scan.yml"]
    generated = workflow_command("container_scan", {}, "", workflow_file="gen-container_scan.yml", ref="orchestrator/generated/run-42", inputs={"run_id": "run-42"})
    assert generated[3] == "gen-container_scan.yml" and "--ref" in generated and "run_id=run-42" in generated
    fixed = workflow_command("deploy", {"env_target": "uat"}, "")
    assert "environment=uat" in fixed and fixed[3] == "deploy.yml"


def test_jenkins_and_ssh_plans_use_placeholder_names_only(tmp_path, no_subprocess):
    jenkins = JenkinsProvider().execute(_stage("unit_test", "jenkins", platform="jenkins"), [], str(tmp_path), mode="plan", answers={})
    assert jenkins.status == "planned"
    flat = " ".join(jenkins.plan["would_run"][0])
    assert "$JENKINS_URL/job/$JENKINS_JOB/buildWithParameters" in flat and "RUN_ID=run-42" in flat
    assert {"JENKINS_URL", "JENKINS_USER", "JENKINS_API_TOKEN", "JENKINS_JOB"} <= set(jenkins.plan["missing"])
    ssh = SshProvider().would_run(_stage("deploy_dev", "ssh_remote", platform="remote"), _script(tmp_path), str(tmp_path), {})
    assert ssh[0][0] == "scp" and ssh[1][0] == "ssh" and "$SSH_USER@$SSH_HOST" in ssh[1]
    compose = SshProvider().would_run(_stage("deploy_dev", "ssh_remote", provider="ssh_compose", platform="remote"), [], str(tmp_path), {})
    assert "docker compose" in compose[1][-1]


def test_expand_env_substitutes_config_names_only():
    env = {"AWS_REGION": "us-east-1", "AWS_SECRET_ACCESS_KEY": "never-on-argv", "SSH_PORT": ""}
    argv = expand_env(["aws", "--region", "$AWS_REGION", "key=$AWS_SECRET_ACCESS_KEY", "${AWS_CODEBUILD_PROJECT}", "-p", "$SSH_PORT"], env)
    assert argv == ["aws", "--region", "us-east-1", "key=$AWS_SECRET_ACCESS_KEY", "${AWS_CODEBUILD_PROJECT}", "-p", "22"]


def test_scanner_catalog_commands_and_skips(monkeypatch):
    argv = catalog.scanner_argv("trivy", "container_scan", image="app:x")
    assert argv[:2] == ["trivy", "image"] and argv[-1] == "app:x"
    monkeypatch.setattr(scanners, "cli_present", lambda key: False)
    skip = catalog.scanner_commands("trivy", "container_scan", image="app:x")
    assert skip["label"] == "trivy" and "skip-tool" in skip["argv"]
    monkeypatch.setattr(scanners, "cli_present", lambda key: True)
    live = catalog.scanner_commands("trivy", "sca", root="/repo")
    assert live["argv"][:2] == ["trivy", "fs"] and live["argv"][-1] == "/repo"
    docker = catalog.scanner_commands("trivy", "container_scan", runner="docker", image="app:x")
    assert docker["argv"][0] == "trivy"
    with pytest.raises(ValueError):
        catalog.scanner_argv("no_such_tool", "sast")
    assert "trivy" in scanners.tools_for_stage("container_scan")
    assert scanners.aliases()["hawkscan"] == "stackhawk"


def test_catalog_driven_and_generic_findings():
    findings, step, needs = extract_result("grype", ["NAME  INSTALLED  FIXED", "libx  1.0  1.1  High"], failed=True)
    assert "High" in findings and needs is True and "Grype" in step
    findings, step, needs = extract_result("gitleaks", ["3 leaks found"])
    assert needs is True and "rotate" in step
    findings, step, needs = extract_result("gitleaks", ["no leaks found"])
    assert needs is False
    findings, step, needs = extract_result("mystery_tool", ["all good"])
    assert needs is False and findings
    findings, step, needs = extract_result("mystery_tool", ["ERROR: boom"], failed=True)
    assert needs is True and "ERROR" in findings
    assert tool_key("Vulnerabilities (Grype)") == "grype"
    assert tool_key("Install Snyk CLI") == ""
    assert tool_key("SAST (Semgrep)") == "semgrep"


def test_execute_stage_v2_blocks_on_policy_and_plans_cloud(tmp_path, no_subprocess):
    blocked = execute_stage_v2(_stage("deploy_dev", "local"), [{"argv": ["bash", "-c", "rm -rf /"]}], str(tmp_path))
    assert blocked["status"] == "fail" and blocked["blocked"] is True and "policy blocked" in blocked["evidence"]
    planned = execute_stage_v2(_stage("deploy_dev", "aws_codebuild"), [], str(tmp_path), readiness={})
    assert planned["status"] == "planned" and planned["plan"]["runner"] == "aws_codebuild"
    unknown = execute_stage_v2({"name": "x", "runner": "mainframe"}, [], str(tmp_path))
    assert unknown["status"] == "fail" and "no provider" in unknown["evidence"]


def test_execute_stage_v2_runs_local_and_extracts_findings(tmp_path):
    records = _script(tmp_path, "echo 'Total: 2 (HIGH: 1, CRITICAL: 1)'\n")
    result = execute_stage_v2(_stage("container_scan", "local", provider="trivy"), [], str(tmp_path), records=records)
    assert result["status"] == "pass"
    check = result["checks"][0]
    assert check["tool"] == "trivy" and "Total: 2" in check["findings"] and check["needs_astra"] is True


def test_status_from_readiness_round_trip():
    readiness = {
        "providers": {"aws": {"cli_path": "/opt/homebrew/bin/aws", "present": ["AWS_REGION"]}},
        "runners": {"aws_codebuild": {"group": "aws", "cli": True, "missing": ["AWS_ACCESS_KEY_ID"], "mode": "plan"}},
    }
    status = status_from_readiness(readiness, "aws_codebuild")
    assert status.mode == "plan" and status.missing == ["AWS_ACCESS_KEY_ID"] and status.present == ["AWS_REGION"]
    assert status_from_readiness(readiness, "jenkins") is None


@pytest.mark.parametrize("kind", sorted(templates.KIND_FILES))
def test_templates_render_for_every_kind(kind):
    stage = {"name": "container_scan", "family": "scan", "purpose": "scan the image", "provider": "trivy"}
    text = templates.render_template(kind, stage, {"tier": "dev", "platform": "aws"}, runner="aws_codebuild", run_id="run-42", placeholders=["AWS_REGION", "TRIVY_TOKEN"])
    assert "dev" in text
    if kind not in {"doc", "env_example", "dockerfile", "compose", "kubernetes_manifest"}:
        assert "container_scan" in text
    # Pure template variables are substituted; $TIER / $RUN_ID may remain as shell variables.
    for leftover in ("$FAMILY", "$PURPOSE", "$GENERATED_AT", "$PLACEHOLDER_NAMES", "$ENV_LINES", "$SECRETS_LINES", "$APP_NAME"):
        assert leftover not in text, (kind, leftover)
    assert "$$" not in text
    if kind == "github_workflow":
        assert "${{ secrets.AWS_REGION }}" in text and "${{ inputs.run_id }}" in text and "workflow_dispatch" in text
    if kind in {"shell", "script"}:
        assert "set -euo pipefail" in text and "${RUN_ID:-run-42}" in text and '"${AWS_REGION:=}"' in text
    if kind == "compose":
        assert "AWS_REGION: ${AWS_REGION:-}" in text
    assert "production|prod)" in text or kind in {"doc", "env_example", "dockerfile", "compose", "kubernetes_manifest"}
    assert templates.artifact_path(kind, "container_scan", {"tier": "dev", "platform": "aws"})


def test_provider_skeletons_use_their_registry_placeholders():
    skeleton = AwsProvider().render_skeleton(_stage("deploy_dev", "aws_codebuild"), {"tier": "dev", "platform": "aws"}, run_id="run-42")
    assert set(skeleton) == {"buildspec", "shell"}
    assert "AWS_REGION" in skeleton["shell"] and "RUN_ID" in skeleton["buildspec"]
    assert os.environ.get("AWS_SECRET_ACCESS_KEY", "") == ""  # the conftest scrub holds
