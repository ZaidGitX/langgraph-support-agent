from __future__ import annotations

import inspect
import sys
from pathlib import Path

from orchestrator import demo
from orchestrator.catalog import commands_for
from orchestrator.dispatch import commands_for_stages, execute_stage, workflows_for
from orchestrator.graph import normalize_stages
from orchestrator.guardrails import apply_guardrails
from orchestrator.local_ops import package, release, secrets, uat
from orchestrator.runner import redact_secrets
from orchestrator.scan import discover, scan_checkout
from orchestrator.schemas import GateDecision, QuestionSet, StageBrief
from scripts.wait_for_session import wait_for

FORBIDDEN_WORKFLOWS = (
    "package.yml",
    "scanners.yml",
    "dast.yml",
    "release.yml",
    "deploy.yml",
    "ci.yml",
)


def test_scan_stage_dispatches_scanners_only_when_designated():
    designated = commands_for_stages(["build", "scanners"], {"env_target": "local"}, "")
    blob = " ".join(" ".join(command) for command in designated)
    assert "scanners.yml" in blob
    assert "build.yml" in blob
    assert workflows_for(["build", "scanners"]) == ["build.yml", "scanners.yml"]
    absent = commands_for_stages(["build", "test"], {}, "")
    absent_blob = " ".join(" ".join(command) for command in absent)
    assert "scanners.yml" not in absent_blob
    assert workflows_for(["build", "test"]) == ["build.yml", "test.yml"]


def test_scanners_workflow_runs_trivy_and_snyk():
    root = Path(__file__).resolve().parents[1]
    scanners = (root / ".github" / "workflows" / "scanners.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch" in scanners
    assert "\n  push:" not in scanners
    assert "aquasecurity/trivy-action@v0.36.0" in scanners
    assert "scan-type: fs" in scanners
    assert "snyk/actions/setup@v1.0.0" in scanners
    assert "snyk test --file=requirements.txt --command=python3 --fail-on=upgradable" in scanners
    assert "snyk test --file=frontend/package.json --fail-on=upgradable" in scanners
    assert "actions/setup-node@v4" in scanners
    assert "working-directory: frontend" in scanners
    assert "npm ci" in scanners
    assert 'python -m venv "$RUNNER_TEMP/semgrep-venv"' in scanners
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    assert "mcp==1.30.0" in requirements
    assert "secrets.SNYK_TOKEN" in scanners
    assert "snyk_uat" not in scanners
    dependabot = (root / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    for ecosystem in ("pip", "npm", "github-actions"):
        assert f"package-ecosystem: {ecosystem}" in dependabot


def test_build_and_test_do_not_dispatch_later_workflows():
    commands = commands_for_stages(["build", "test"], {"env_target": "local"}, "https://example.test")
    blob = " ".join(" ".join(command) for command in commands)
    assert "build.yml" in blob
    assert "test.yml" in blob
    for name in FORBIDDEN_WORKFLOWS:
        assert name not in blob
    assert workflows_for(["build", "test"]) == ["build.yml", "test.yml"]


def test_dast_command_carries_the_app_url_only_when_designated():
    designated = commands_for_stages(["dast"], {}, "https://tunnel.example")
    assert any("app_url=https://tunnel.example" in part for command in designated for part in command)
    absent = " ".join(" ".join(command) for command in commands_for_stages(["build"], {}, "https://tunnel.example"))
    assert "dast.yml" not in absent
    assert "app_url=" not in absent


def test_local_mode_does_not_call_github(monkeypatch, tmp_path):
    def boom(*_args, **_kwargs):
        raise AssertionError("github dispatch")

    monkeypatch.setattr("orchestrator.dispatch.run_workflow", boom)
    result = execute_stage(
        "build",
        [{"argv": [sys.executable, "-c", "print('ok')"], "timeout": 30, "label": "echo"}],
        str(tmp_path),
    )
    assert result["status"] == "pass"


def test_retry_cannot_add_a_stage_jev_did_not_approve():
    decision = GateDecision(
        route="remediate",
        retry=True,
        retry_stages=["build", "package", "dast", "release", "deploy"],
        replan=False,
        rationale="retry more than was approved",
    )
    state = {
        "attempt": {"build": 1},
        "hops": 1,
        "started_at": 10**12,
        "held_seconds": 0,
        "pipeline": {"id": "p", "stages": [{"name": "build"}, {"name": "test"}]},
        "stage_results": [{"stage": "build", "plan_id": "p", "status": "fail", "blocked": False}],
        "env_state": {},
    }
    guarded = apply_guardrails(state, decision)
    assert guarded.retry_stages == ["build"]
    assert "package" not in guarded.retry_stages
    assert "dast" not in guarded.retry_stages


def test_cycle_does_not_add_stages():
    ordered, removed = normalize_stages(
        [
            {"name": "build", "depends_on": ["test"], "purpose": "compile"},
            {"name": "test", "depends_on": ["build"], "purpose": "test"},
            {"name": "not-a-stage", "depends_on": [], "purpose": "ignore"},
        ]
    )
    names = [stage["name"] for stage in ordered]
    assert names == ["build", "test"]
    assert removed
    index = {name: position for position, name in enumerate(names)}
    for stage in ordered:
        for dep in stage["depends_on"]:
            assert index[dep] < index[stage["name"]]


def test_catalog_commands_are_fixed():
    build = commands_for("build", {}, None)
    labels = [item["label"] for item in build]
    assert labels == ["compileall", "ruff", "pip-audit", "secrets"]
    scanners = [item["label"] for item in commands_for("scanners", {}, None)]
    assert "semgrep" in scanners
    assert "codeql" in scanners
    blob = " ".join(" ".join(item["argv"]) for item in build + commands_for("dast", {}, None))
    assert "gpt-6-astra" not in blob
    brief = StageBrief(summary="Do not run this sentence as a shell command.", watch_for="watch")
    assert "argv" not in brief.model_dump()
    assert "answer" not in QuestionSet.model_json_schema()["properties"]


def test_scan_reads_the_checkout_without_writing_questions():
    profile = scan_checkout(str(Path(__file__).resolve().parents[1]))
    assert "Python" in profile["languages"]
    ecosystem = discover(profile["root"])
    assert ecosystem["entry_points"]
    assert "questions" not in ecosystem


def test_local_ops_round_trip(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    assert package(tmp_path) == 0
    assert release(tmp_path) == 0
    assert uat(tmp_path, "uat") == 0
    assert secrets(tmp_path) == 0


def test_redact_hawk_shaped_token():
    sample = "hawk." + ("a" * 8) + "." + ("b" * 8)
    cleaned = redact_secrets(f"token {sample} end")
    assert "a" * 8 not in cleaned
    assert "[redacted]" in cleaned


def test_redact_snyk_shaped_token():
    sample = "snyk_uat." + ("a" * 8) + ".bbb.ccc"
    cleaned = redact_secrets(f"token {sample} end")
    assert "snyk_uat." not in cleaned
    assert "[redacted]" in cleaned


def test_only_the_session_workflow_runs_on_push():
    root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    for path in root.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        if path.name == "orchestrator.yml":
            assert "push:" in text
            assert "pull_request" not in text
            assert "actions: write" in text
            continue
        assert "pull_request" not in text
        assert "\n  push:" not in text
        assert "workflow_dispatch" in text


def test_demo_module_does_not_script_answers():
    source = inspect.getsource(demo)
    assert "demo_resume" not in source
    assert "env_target" not in source


def test_wait_times_out_without_answering(capsys):
    now = {"value": 0.0}

    def clock():
        return now["value"]

    def sleep(seconds):
        now["value"] += seconds

    def opener(_url, timeout=10):
        raise OSError("down")

    code = wait_for(
        "http://127.0.0.1:9",
        30,
        clock=clock,
        sleep=sleep,
        opener=opener,
        grace_seconds=0,
    )
    assert code == 1
    assert "No canned answers were submitted." in capsys.readouterr().out


def test_wait_finishes_when_the_results_screen_was_viewed(capsys):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"phase":"results","results_viewed":true,"status":"passed","run_id":"abc"}'

    code = wait_for(
        "http://127.0.0.1:8000",
        60,
        clock=lambda: 0,
        sleep=lambda _seconds: None,
        opener=lambda _url, timeout=10: Response(),
        grace_seconds=0,
    )
    assert code == 0
    assert "status=passed" in capsys.readouterr().out
