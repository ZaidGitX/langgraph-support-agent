from __future__ import annotations

import time
from pathlib import Path

import pytest

from orchestrator import profile
from orchestrator.schemas import RankDecision
from orchestrator.session import SessionService
from tests.test_orchestrator import BadArtifactAstra, FakeAstra, FakeJev, _echo

PROFILE = """
version: 1
platform: local
tier: dev
answers:
  by_id:
    env_target: dev
    test_depth: unit
    release_policy: manual
    scan_policy: high
  by_topic:
    runner: local
autopilot:
  answer_questions: true
  approve: true
  min_confidence: 0.6
"""


def _write_profile(root: Path, text: str = PROFILE) -> None:
    (root / "orchestrator.yaml").write_text(text, encoding="utf-8")


def _wait(service: SessionService, phases: set[str], timeout: float = 15) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = service.snapshot()
        if last["phase"] in phases:
            return last
        if last["phase"] == "error":
            raise AssertionError(last["error"])
        time.sleep(0.02)
    raise AssertionError(f"timed out in phase {last and last['phase']}: {last and last.get('autopilot')}")


def test_profile_answers_by_id_defaults_and_topic(tmp_path):
    _write_profile(tmp_path)
    loaded = profile.load(tmp_path)
    assert loaded and loaded.enabled and loaded.path.endswith("orchestrator.yaml")
    assert profile.answer_for(loaded, {"id": "env_target", "prompt": "?"}) == "dev"
    assert profile.answer_for(loaded, {"id": "env_platform", "prompt": "Which platform?"}) == "local"
    assert profile.answer_for(loaded, {"id": "q_runner_choice", "prompt": "Which runner should build?"}) == "local"
    assert profile.answer_for(loaded, {"id": "unknown", "prompt": "Anything else?"}) is None
    answers, unmatched = profile.answers_for(loaded, [{"id": "test_depth", "prompt": ""}, {"id": "colour", "prompt": "Favourite colour?"}])
    assert answers == {"test_depth": "unit"} and unmatched == ["colour"]
    assert profile.load(tmp_path / "nowhere") is None


def test_profile_rejects_a_forbidden_tier_and_unknown_platform(tmp_path):
    _write_profile(tmp_path, "version: 1\ntier: prod" + "uction\n")
    with pytest.raises(profile.ProfileError, match="tier"):
        profile.load(tmp_path)
    _write_profile(tmp_path, "version: 1\nplatform: mainframe\n")
    with pytest.raises(profile.ProfileError, match="platform"):
        profile.load(tmp_path)
    _write_profile(tmp_path, "version: 1\nsurprise: true\n")
    with pytest.raises(profile.ProfileError):
        profile.load(tmp_path)


def test_approval_rules():
    loaded = profile.RunProfile()
    clean = {"artifacts": {}, "artifact_review": {}, "jev_decisions": [], "pipeline": {"stages": [{"name": "build"}]}}
    assert profile.approval(loaded, clean)[0] is True
    dirty = {**clean, "artifacts": {"x": [{"policy": {"ok": False}}]}}
    assert profile.approval(loaded, dirty) == (False, "an artifact has a policy error")
    rejected = {**clean, "artifact_review": {"x": {"verdict": "reject"}}}
    assert "did not accept x" in profile.approval(loaded, rejected)[1]
    low = {**clean, "jev_decisions": [{"kind": "rank", "choice": {"pipeline": {"choice": "a", "confidence": 0.3}}}]}
    assert "below" in profile.approval(loaded, low)[1]
    high = {**clean, "jev_decisions": [{"kind": "rank", "choice": {"pipeline": {"choice": "a", "confidence": 0.9}}}]}
    assert profile.approval(loaded, high)[0] is True
    off = profile.RunProfile(autopilot={"approve": False})
    assert profile.approval(off, clean) == (False, "autopilot.approve is off")


def test_autopilot_answers_questions_and_approves_but_waits_at_uat(tmp_path):
    _write_profile(tmp_path)
    service = SessionService(db_path=str(tmp_path / "a.sqlite"), jev=FakeJev(), astra=FakeAstra(), commands=_echo(), repo_root=str(tmp_path))
    try:
        started = service.start(trigger={"source": "webhook", "sha": "abc123"})
        assert started["autopilot"]["enabled"] is True and started["trigger"]["source"] == "webhook"
        view = _wait(service, {"uat"})
        actions = [(event["gate"], event["action"]) for event in view["autopilot"]["events"]]
        assert ("ecosystem_qa", "answered") in actions and ("pipeline_approval", "approved") in actions
        assert ("uat_signoff", "human") in actions
        assert view["phase"] == "uat" and view["gate"] == "uat_signoff"
        service.submit({"gate": "uat_signoff", "decision": "approved"})
        results = _wait(service, {"results"})
        assert results["report"]["approvals"]["pipeline"] == "approved"
        assert results["report"]["approvals"]["pipeline_by"] == "autopilot"
        assert results["report"]["approvals"]["uat"] == "approved"
    finally:
        service.close()


def test_an_unmatched_question_falls_back_to_the_person(tmp_path):
    _write_profile(tmp_path, "version: 1\nanswers:\n  by_id:\n    env_target: dev\n    test_depth: unit\n")
    service = SessionService(db_path=str(tmp_path / "b.sqlite"), jev=FakeJev(), astra=FakeAstra(), commands=_echo(), repo_root=str(tmp_path))
    try:
        service.start()
        view = _wait(service, {"questions"})
        events = view["autopilot"]["events"]
        assert events and events[-1]["action"] == "human" and "release_policy" in events[-1]["detail"]
    finally:
        service.close()


class LowConfidenceJev(FakeJev):
    def rank(self, payload: dict) -> RankDecision:
        decision = super().rank(payload)
        self.last_call = {"client": "FakeJev", "model": "injected", "choice": {"pipeline": {"choice": decision.chosen_id, "confidence": 0.2}}}
        return decision


def test_low_confidence_keeps_the_approval_gate_human(tmp_path):
    _write_profile(tmp_path)
    service = SessionService(db_path=str(tmp_path / "c.sqlite"), jev=LowConfidenceJev(), astra=FakeAstra(), commands=_echo(), repo_root=str(tmp_path))
    try:
        service.start()
        view = _wait(service, {"approval"})
        last = view["autopilot"]["events"][-1]
        assert last["gate"] == "pipeline_approval" and last["action"] == "human" and "0.20" in last["detail"]
    finally:
        service.close()


def test_a_policy_error_keeps_the_approval_gate_human(tmp_path):
    _write_profile(tmp_path)
    service = SessionService(db_path=str(tmp_path / "d.sqlite"), jev=FakeJev(), astra=BadArtifactAstra(), commands=_echo(), repo_root=str(tmp_path))
    try:
        service.start()
        view = _wait(service, {"approval"})
        # The phase flips before the autopilot event is appended; on a slow CodeBuild host the
        # event arrived after the first snapshot (live run 12's test stage failed once on this).
        deadline = time.time() + 10
        while time.time() < deadline and not any(event.get("gate") == "pipeline_approval" for event in view["autopilot"]["events"]):
            time.sleep(0.05)
            view = service.snapshot()
        last = next(event for event in reversed(view["autopilot"]["events"]) if event.get("gate") == "pipeline_approval")
        assert last["action"] == "human" and ("policy" in last["detail"] or "accept" in last["detail"])
    finally:
        service.close()


def test_a_second_push_waits_in_the_queue_and_starts_after_the_run(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _write_profile(first)
    _write_profile(second)
    service = SessionService(db_path=str(tmp_path / "e.sqlite"), jev=FakeJev(), astra=FakeAstra(), commands=_echo(), repo_root=str(first))
    try:
        service.start(trigger={"source": "webhook", "sha": "one"})
        view = _wait(service, {"uat"})
        first_run = view["run_id"]
        queued = service.start(repo_root=str(second), trigger={"source": "webhook", "sha": "two"})
        assert queued["run_id"] == first_run and queued["queue"] == [{"source": "webhook", "sha": "two"}]
        service.submit({"gate": "uat_signoff", "decision": "approved"})
        deadline = time.time() + 15
        while time.time() < deadline:
            view = service.snapshot()
            if view["run_id"] != first_run and view.get("trigger", {}).get("sha") == "two":
                break
            time.sleep(0.02)
        assert view["trigger"]["sha"] == "two" and view["queue"] == []
        _wait(service, {"uat"})
    finally:
        service.close()
