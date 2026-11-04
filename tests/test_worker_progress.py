"""After live run 4 (2026-11-04): a worker call that times out is retried once at the next
lower effort before the stage is given up, each effort has its own timeout, and the
generation node reports every worker's start and end to the session timeline."""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver

from orchestrator import llm, runner
from orchestrator.graph import build_graph
from orchestrator.schemas import StageBrief
from orchestrator.state import initial_state
from tests.test_orchestrator import FakeJev, _echo, _resume_for_test, drive
from tests.test_workers import OrchestratingAstra


def test_effort_timeouts_never_drop_below_the_env_default(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_ASTRA_TIMEOUT", "300")
    assert llm.timeout_for("high") == 900.0 and llm.timeout_for("medium") == 600.0
    assert llm.timeout_for("low") == llm.timeout_for("minimal") == llm.timeout_for("") == 300.0
    monkeypatch.setenv("ORCHESTRATOR_ASTRA_TIMEOUT", "700")
    assert llm.timeout_for("medium") == 700.0 and llm.timeout_for("high") == 900.0


def test_a_timed_out_call_is_retried_once_at_the_next_lower_effort(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-orchestrator")
    monkeypatch.setenv("ORCHESTRATOR_ASTRA_TIMEOUT", "300")
    seen: list[tuple[str, float | None]] = []

    def fake_invoke(model_name, schema, system, payload, *, temperature=None, reasoning=None, timeout=None):
        seen.append((reasoning, timeout))
        if reasoning == "high":
            raise RuntimeError("gpt-6-astra structured output failed: Request timed out.")
        return StageBrief(summary="ok", watch_for="nothing")

    monkeypatch.setattr(llm, "invoke_structured", fake_invoke)
    worker = llm.AstraWorker("gpt-6-astra", "high", stage="pipeline")
    assert worker.brief({"stage": "pipeline"}).summary == "ok"
    assert seen == [("high", 900.0), ("medium", 600.0)]
    assert worker.reasoning_effort == "medium" and worker.timed_out_at == "high"

    always = llm.AstraWorker("gpt-6-astra", "medium")
    monkeypatch.setattr(llm, "invoke_structured", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x: Request timed out.")))
    with pytest.raises(RuntimeError, match="timed out"):
        always.brief({"stage": "build"})  # one retry at low, then the error stands
    assert always.reasoning_effort == "low" and always.timed_out_at == "medium"

    minimal = llm.AstraWorker("gpt-6-astra", "minimal")
    with pytest.raises(RuntimeError, match="timed out"):
        minimal.brief({"stage": "build"})
    assert minimal.reasoning_effort == "minimal" and minimal.timed_out_at == ""


def test_the_generation_node_reports_every_worker_to_the_progress_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-orchestrator")
    monkeypatch.setenv("ORCHESTRATOR_ASTRA_WORKER_MODELS", "gpt-test-worker")
    monkeypatch.setenv("ORCHESTRATOR_ASTRA_REASONING", "low")
    monkeypatch.setenv("ORCHESTRATOR_ASTRA_CONCURRENCY", "2")
    reported: list[tuple] = []
    previous = runner.set_progress(lambda kind, name, detail, **extra: reported.append((kind, name, detail, extra)))
    try:
        graph = build_graph(MemorySaver(), jev=FakeJev(), astra=OrchestratingAstra(), commands=_echo())
        final = drive(graph, initial_state(str(tmp_path)), _resume_for_test, echo=False)
    finally:
        runner.reset_progress(previous)
    assert "container_scan" in final["generated_artifacts"]
    kinds = [(kind, name, extra.get("action")) for kind, name, _detail, extra in reported]
    assert ("generation", "generate_artifacts", None) in kinds
    assert ("worker", "container_scan", "start") in kinds and ("worker", "container_scan", "done") in kinds
    start = next(item for item in reported if item[1] == "container_scan" and item[3].get("action") == "start")
    done = next(item for item in reported if item[1] == "container_scan" and item[3].get("action") == "done")
    assert start[3]["model"] == "gpt-test-worker" and start[3]["effort"] == "high"
    assert "writing files with gpt-test-worker at high effort" in start[2]
    assert "file(s) in" in done[2] and done[3]["seconds"] >= 0 and done[3]["effort"] == "high"


def test_a_failing_hook_never_fails_the_run():
    previous = runner.set_progress(lambda *a, **k: (_ for _ in ()).throw(ValueError("ui gone")))
    try:
        runner.progress("worker", "build", "detail")
    finally:
        runner.reset_progress(previous)
    runner.progress("worker", "build", "no hook installed is fine")
