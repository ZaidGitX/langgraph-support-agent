"""The environment a person answered in an earlier run is reused when Astra skips the question.

Live run 2 (2026-11-03) stalled at the questions gate: Astra recalled run 1 and asked three
questions without env_platform or env_target, the plan would have routed to the local host,
and the answer endpoint refused the extra ids. These tests pin the fix end to end.
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver

from orchestrator.demo import drive
from orchestrator.graph import build_graph, recalled_environment
from orchestrator.schemas import EcosystemQuestion, QuestionSet
from orchestrator.session import SessionService
from orchestrator.state import initial_state
from tests.test_orchestrator import FakeAstra, FakeJev, _echo


def test_recalled_environment_prefers_what_was_answered_then_what_ran():
    episodes = [
        {"platform": "local", "tier": "dev", "answered": {"env_platform": "aws"}},
        {"platform": "jenkins", "tier": "uat", "answered": {}},
    ]
    assert recalled_environment({}, [], episodes) == {"env_platform": "aws", "env_target": "dev"}
    assert recalled_environment({"env_platform": "gcp"}, [], episodes) == {"env_target": "dev"}
    assert recalled_environment({}, [{"id": "env_platform"}, {"id": "env_target"}], episodes) == {}
    bad = [{"platform": "mars", "tier": "prod" + "uction", "answered": {"env_platform": "nowhere"}}]
    assert recalled_environment({}, [], bad) == {}
    assert recalled_environment({}, [], [{"platform": "", "tier": ""}, {"platform": "aws", "tier": "staging"}]) == {"env_platform": "aws", "env_target": "staging"}


class RecallingAstra(FakeAstra):
    """Asks the environment questions only when no episode answers them, as the real Astra does."""

    def write_questions(self, payload: dict) -> QuestionSet:
        if payload.get("episodes"):
            return QuestionSet(questions=[EcosystemQuestion(id="test_depth", prompt="How deep?", reason="an earlier run answered the rest")])
        return QuestionSet(
            questions=[
                EcosystemQuestion(id="env_target", prompt="Which tier?", reason="first run"),
                EcosystemQuestion(id="env_platform", prompt="Which platform?", reason="first run"),
                EcosystemQuestion(id="test_depth", prompt="How deep?", reason="first run"),
            ]
        )


def _answer_only_what_was_asked(payload: dict) -> dict:
    if payload.get("gate") in {"ecosystem_qa", "follow_up_qa"}:
        values = {"env_target": "dev", "env_platform": "aws"}
        return {question["id"]: values.get(question["id"], f"answer for {question['id']}") for question in payload.get("questions") or []}
    return {"decision": "approved"}


def test_second_run_reuses_the_recalled_platform_when_astra_skips_it(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_DISPATCH", "platform")
    db = str(tmp_path / "episodes.sqlite")
    first = build_graph(MemorySaver(), jev=FakeJev(), astra=RecallingAstra(), commands=_echo(), episodes_db=db)
    one = drive(first, initial_state(str(tmp_path), run_id="run-one"), _answer_only_what_was_asked, echo=False)
    assert one["answers"]["env_platform"] == "aws" and one["recalled_answers"] == {}
    second = build_graph(MemorySaver(), jev=FakeJev(), astra=RecallingAstra(), commands=_echo(), episodes_db=db)
    two = drive(second, initial_state(str(tmp_path), run_id="run-two"), _answer_only_what_was_asked, echo=False)
    assert two["recalled_answers"] == {"env_platform": "aws", "env_target": "dev"}
    assert two["answers"]["env_platform"] == "aws" and two["answers"]["env_target"] == "dev"
    runners = {stage["name"]: stage["runner"] for stage in two["pipeline"]["stages"]}
    assert runners["build"] == "aws_codebuild" and runners["uat"] == "local"
    assert two["pipeline"]["suite"]["platform"] == "aws" and two["pipeline"]["suite"]["status"] == "ok"
    assert two["status"] == "passed"


def test_answer_endpoint_accepts_the_environment_ids_when_they_were_not_asked():
    service = SessionService.__new__(SessionService)
    service.view = {"questions": [{"id": "test_depth"}]}
    cleaned = service._resume_for("questions", {"answers": {"test_depth": "unit", "env_platform": "AWS", "env_target": "dev"}})
    assert cleaned == {"test_depth": "unit", "env_platform": "aws", "env_target": "dev"}
    with pytest.raises(ValueError):
        service._resume_for("questions", {"answers": {"test_depth": "unit", "env_platform": "mars"}})
    with pytest.raises(ValueError):
        service._resume_for("questions", {"answers": {"test_depth": "unit", "runner_preference": "docker"}})
    with pytest.raises(ValueError):
        service._resume_for("questions", {"answers": {"env_platform": "aws"}})
