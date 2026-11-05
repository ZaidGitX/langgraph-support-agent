"""Live run 6 (2026-11-05) fixes: a clean policy result is never rejected; a cloud profile
does not approve a plan whose suite fell back to the host (it rejects once, with the reason);
a Python stage script runs with python3 on the host."""

from __future__ import annotations

from orchestrator import profile
from orchestrator.llm import guard_review
from orchestrator.providers.local import LocalProvider
from orchestrator.schemas import ArtifactReview
from orchestrator.session import SessionService

AWS_PROFILE = "version: 1\nplatform: aws\ntier: dev\nautopilot:\n  approve: true\n  min_confidence: 0.6\n"
CLEAN_VIEW = {
    "artifacts": {"build_image": [{"path": "ci/x.sh", "policy": {"ok": True}}]},
    "artifact_review": {"build_image": {"verdict": "accept"}},
    "jev_decisions": [{"kind": "rank", "choice": {"pipeline": {"confidence": 0.97}}}],
    "pipeline": {"stages": [{"name": "build"}, {"name": "uat"}]},
}


def test_a_clean_policy_result_is_never_rejected():
    rejected = ArtifactReview(verdict="reject", rationale="Jev: revisions exhausted")
    assert guard_review(rejected, True, 0).verdict == "accept"
    assert guard_review(rejected, True, 1).verdict == "accept"
    assert guard_review(rejected, False, 0).verdict == "reject"
    assert guard_review(ArtifactReview(verdict="revise", rationale="r"), True, 1).verdict == "revise"


def test_a_cloud_profile_needs_the_suite_in_use(tmp_path):
    (tmp_path / "orchestrator.yaml").write_text(AWS_PROFILE, encoding="utf-8")
    loaded = profile.load(tmp_path)
    ok, reason = profile.approval(loaded, {**CLEAN_VIEW, "suite": {"status": "suite rejected (reject); on the host instead: build"}})
    assert not ok and reason.startswith("suite:") and "aws suite is not in use" in reason
    ok, reason = profile.approval(loaded, {**CLEAN_VIEW, "suite": {"platform": "aws", "runner": "aws_codebuild", "status": "", "stage_files": {}}})
    assert not ok and "no suite was generated" in reason
    ok, _ = profile.approval(loaded, {**CLEAN_VIEW, "suite": {"status": "ok"}})
    assert ok
    # A local-dispatch run (a laptop) never wants a suite: nothing to require.
    ok, _ = profile.approval(loaded, {**CLEAN_VIEW, "suite": {}})
    assert ok
    (tmp_path / "orchestrator.yaml").write_text(AWS_PROFILE.replace("platform: aws", "platform: local"), encoding="utf-8")
    ok, _ = profile.approval(profile.load(tmp_path), {**CLEAN_VIEW, "suite": {}})
    assert ok


def test_autopilot_rejects_a_host_fallback_once_then_leaves_it_to_a_person(tmp_path):
    (tmp_path / "orchestrator.yaml").write_text(AWS_PROFILE, encoding="utf-8")
    service = SessionService(repo_root=str(tmp_path), db_path=str(tmp_path / "db.sqlite"))
    service._run_root = str(tmp_path)
    service.view["autopilot"] = service._load_profile()
    service._auto_rejected = False
    service.view.update({**CLEAN_VIEW, "phase": "approval", "status": "waiting", "suite": {"status": "suite rejected (reject); on the host instead: build"}})
    resume = service._autopilot({"gate": "pipeline_approval"})
    assert resume["decision"] == "rejected" and resume["by"] == "autopilot"
    assert "aws suite is not in use" in resume["feedback"] and "ci/, deploy/, docker/, or docs/deploy/" in resume["feedback"]
    events = [(item["gate"], item["action"]) for item in service.view["autopilot"]["events"]]
    assert ("pipeline_approval", "rejected") in events
    service.view.update({"phase": "approval", "status": "waiting"})
    assert service._autopilot({"gate": "pipeline_approval"}) is None
    assert service.view["autopilot"]["events"][-1]["action"] == "human"
    service.view.update({"phase": "approval", "status": "waiting", "suite": {"status": "ok"}})
    assert service._autopilot({"gate": "pipeline_approval"}) == {"decision": "approved", "by": "autopilot"}


def test_a_python_stage_script_runs_with_python3(tmp_path):
    script = tmp_path / "ci" / "smoke.generated.py"
    script.parent.mkdir()
    script.write_text("print('ok')\n", encoding="utf-8")
    shell = tmp_path / "ci" / "build.generated.sh"
    shell.write_text("echo ok\n", encoding="utf-8")
    provider = LocalProvider()
    python_cmd = provider.commands_for({"name": "smoke"}, [{"path": "ci/smoke.generated.py", "kind": "shell", "stored_at": str(script)}])
    assert python_cmd[0]["argv"][:1] == ["python3"] and python_cmd[0]["argv"][1].endswith("smoke.generated.py")
    bash_cmd = provider.commands_for({"name": "build"}, [{"path": "ci/build.generated.sh", "kind": "shell", "stored_at": str(shell)}])
    assert bash_cmd[0]["argv"][:1] == ["bash"]
