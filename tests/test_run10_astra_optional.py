"""Live run 10 (2026-11-05) reached AWS, injected the app secrets, and then died on an OpenAI 429
("no credits remaining") raised by the first stage's brief. After approval Astra is advisory:
briefs, advice, the diagnosis, and the deploy document never abort a run whose stages can run."""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver

from orchestrator.graph import build_graph
from orchestrator.llm import AstraQuotaError
from orchestrator.schemas import StageSpec
from orchestrator.state import initial_state
from tests.test_orchestrator import (
    FakeAstra,
    FakeJev,
    _echo,
    _fail,
    _resume_for_test,
    drive,
)

QUOTA = "gpt-6.1-sol: Error code: 429 - insufficient_quota credit_balance_exhausted"


class BrokeAstra(FakeAstra):
    """Plans and generates like the fake, then runs out of credits at the first stage."""

    def brief(self, payload: dict):
        raise AstraQuotaError(QUOTA)

    def advise(self, payload: dict):
        raise AstraQuotaError(QUOTA)

    def diagnose(self, payload: dict):
        raise AstraQuotaError(QUOTA)

    def write_docs(self, payload: dict):
        raise AstraQuotaError(QUOTA)


def test_a_run_finishes_when_astra_runs_out_of_credits_after_approval(tmp_path):
    graph = build_graph(MemorySaver(), jev=FakeJev(), astra=BrokeAstra(), commands=_echo())
    values = drive(graph, initial_state(str(tmp_path)), _resume_for_test, echo=False)
    assert values["status"] == "passed"
    assert values["approvals"]["uat"] == "approved" and values["env_state"]["past_uat"] is False
    briefs = {row["stage"]: row["brief"] for row in values["stage_results"]}
    assert {"build", "test", "uat"} <= set(briefs)
    assert all("Astra was unavailable for this brief" in text and "429" in text for text in briefs.values())


def test_a_failing_stage_is_still_gated_without_a_diagnosis(tmp_path):
    stages = [StageSpec(name="build", depends_on=[], purpose="fails")]
    graph = build_graph(MemorySaver(), jev=FakeJev(), astra=BrokeAstra(stages), commands=_fail())
    values = drive(graph, initial_state(str(tmp_path)), _resume_for_test, echo=False)
    assert values["status"] == "escalated" and values["gate"]["route"] == "escalate"
    assert "Astra was unavailable for the diagnosis" in values["diagnosis"] and "429" in values["diagnosis"]
