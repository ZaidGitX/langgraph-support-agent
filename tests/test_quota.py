"""A model account out of credits stops the run at once (the user's rule; live run 2)."""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver
from openai import OpenAIError

from app import config
from orchestrator import llm
from orchestrator.demo import drive
from orchestrator.graph import build_graph
from orchestrator.schemas import StageBrief
from orchestrator.state import initial_state
from tests.test_orchestrator import FakeJev, GeneratedAstra, _echo, _resume_for_test

QUOTA_TEXT = "Error code: 429 - {'error': {'message': 'You have no credits remaining. Add credits to continue.', 'type': 'insufficient_quota', 'code': 'credit_balance_exhausted'}}"


class _Failing:
    def __init__(self, exc: Exception, calls: list) -> None:
        self.exc, self.calls = exc, calls

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        self.calls.append(messages)
        raise self.exc


def test_a_quota_error_is_raised_at_once_without_a_second_attempt(monkeypatch):
    calls: list = []
    monkeypatch.setattr(llm, "_client", lambda model_name, temperature=None, reasoning=None: _Failing(OpenAIError(QUOTA_TEXT), calls))
    with pytest.raises(llm.AstraQuotaError) as caught:
        llm.invoke_structured("gpt-test", StageBrief, "system", {"stage": "build"})
    assert len(calls) == 1 and "insufficient_quota" in str(caught.value)
    assert isinstance(caught.value, RuntimeError)


def test_other_errors_still_get_the_second_attempt(monkeypatch):
    calls: list = []
    monkeypatch.setattr(llm, "_client", lambda model_name, temperature=None, reasoning=None: _Failing(OpenAIError("Error code: 500 - server error"), calls))
    with pytest.raises(RuntimeError) as caught:
        llm.invoke_structured("gpt-test", StageBrief, "system", {"stage": "build"})
    assert len(calls) == 2 and not isinstance(caught.value, llm.AstraQuotaError)


def test_is_quota_error_matches_the_known_shapes():
    assert llm.is_quota_error(QUOTA_TEXT)
    assert llm.is_quota_error("You exceeded your current quota, please check your plan")
    assert not llm.is_quota_error("Error code: 429 - rate limit reached, retry after 2s")


def test_the_client_retries_once(monkeypatch):
    monkeypatch.setattr(config, "get_openai_api_key", lambda: "unit-placeholder-" * 3)
    client = llm._client("gpt-test")
    assert client.max_retries == 1


class QuotaAstra(GeneratedAstra):
    """Proposes a generated stage, then runs out of credits while writing its files."""

    def generate_artifacts(self, payload: dict):
        raise llm.AstraQuotaError("gpt-test: " + QUOTA_TEXT)


def test_generation_stops_the_run_on_a_quota_error(tmp_path):
    graph = build_graph(MemorySaver(), jev=FakeJev(), astra=QuotaAstra(), commands=_echo())
    with pytest.raises(llm.AstraQuotaError):
        drive(graph, initial_state(str(tmp_path)), _resume_for_test, echo=False)
