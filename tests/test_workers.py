"""Astra, as the orchestrator, assigns every stage a sub-agent (model and reasoning effort), and
the stage workers use it for generation, briefs, advice, and diagnosis (Sprint 4, before live run 3)."""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app import config
from orchestrator import llm
from orchestrator.demo import drive
from orchestrator.graph import build_graph, pipeline_view
from orchestrator.schemas import StageBrief, WorkerAssignment, WorkerPlan
from orchestrator.state import initial_state
from tests.test_orchestrator import (
    FakeJev,
    GeneratedAstra,
    _echo,
    _fail,
    _resume_for_test,
)


class RecordingWorker:
    """A sub-agent double: records which assignment served which call, then defers to the parent."""

    def __init__(self, parent, assignment: dict) -> None:
        self.parent = parent
        self.assignment = dict(assignment)

    def _log(self, method: str, payload: dict) -> None:
        stage = payload.get("stage")
        name = stage.get("name") if isinstance(stage, dict) else stage
        self.parent.calls.append((method, str(name or ""), self.assignment.get("model"), self.assignment.get("reasoning_effort")))

    def brief(self, payload: dict):
        self._log("brief", payload)
        return self.parent.brief(payload)

    def diagnose(self, payload: dict):
        self._log("diagnose", payload)
        return self.parent.diagnose(payload)

    def generate_artifacts(self, payload: dict):
        self._log("generate_artifacts", payload)
        return self.parent.generate_artifacts(payload)

    def generate_suite(self, payload: dict):
        self._log("generate_suite", payload)
        return self.parent.generate_suite(payload)


class OrchestratingAstra(GeneratedAstra):
    """Gives the scan a worker model at high effort, names an unknown model for build, and an
    unknown stage; everything else keeps the orchestrator default."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []
        self.assignment_payloads: list[dict] = []

    def assign_workers(self, payload: dict) -> WorkerPlan:
        self.assignment_payloads.append(payload)
        return WorkerPlan(
            workers=[
                WorkerAssignment(stage="container_scan", model="gpt-test-worker", reasoning_effort="high", rationale="a scan needs care"),
                WorkerAssignment(stage="build", model="gpt-nobody-has-this", reasoning_effort="minimal", rationale="an unknown model falls back"),
                WorkerAssignment(stage="ghost", model="gpt-test-worker", reasoning_effort="low", rationale="not a stage"),
            ],
            rationale="assigned",
        )

    def worker(self, assignment: dict) -> RecordingWorker:
        return RecordingWorker(self, assignment)


class FailingAssignmentAstra(OrchestratingAstra):
    def assign_workers(self, payload: dict) -> WorkerPlan:
        raise RuntimeError("gpt-orchestrator structured output failed: boom")


@pytest.fixture
def worker_env(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-orchestrator")
    monkeypatch.setenv("ORCHESTRATOR_ASTRA_WORKER_MODELS", "gpt-test-worker")
    monkeypatch.setenv("ORCHESTRATOR_ASTRA_REASONING", "low")
    monkeypatch.setenv("ORCHESTRATOR_ASTRA_CONCURRENCY", "2")


def test_the_orchestrator_assigns_workers_and_the_stages_use_them(tmp_path, worker_env):
    astra = OrchestratingAstra()
    graph = build_graph(MemorySaver(), jev=FakeJev(), astra=astra, commands=_echo())
    final = drive(graph, initial_state(str(tmp_path)), _resume_for_test, echo=False)
    plan = final["worker_plan"]
    assert set(plan) == {"build", "test", "container_scan", "uat"}
    assert plan["container_scan"] == {"model": "gpt-test-worker", "reasoning_effort": "high", "rationale": "a scan needs care", "assigned_by": "astra"}
    assert plan["build"]["model"] == "gpt-orchestrator" and plan["build"]["reasoning_effort"] == "minimal" and plan["build"]["assigned_by"] == "astra"
    assert plan["test"] == {"model": "gpt-orchestrator", "reasoning_effort": "low", "rationale": "orchestrator default", "assigned_by": "default"}
    payload = astra.assignment_payloads[0]
    assert [stage["name"] for stage in payload["stages"]] == [stage["name"] for stage in final["pipeline"]["stages"]]
    assert {stage["name"] for stage in payload["stages"]} == {"build", "test", "container_scan", "uat"}
    assert payload["models_available"] == ["gpt-test-worker", "gpt-orchestrator"]
    assert payload["efforts_available"] == list(llm.REASONING_EFFORTS)
    assert ("generate_artifacts", "container_scan", "gpt-test-worker", "high") in astra.calls
    assert ("brief", "container_scan", "gpt-test-worker", "high") in astra.calls
    assert ("brief", "build", "gpt-orchestrator", "minimal") in astra.calls
    assert ("brief", "test", "gpt-orchestrator", "low") in astra.calls
    assert all(stage["worker"]["model"] for stage in pipeline_view(final["pipeline"])["stages"])
    assert final["pipeline"]["worker_plan"] == plan
    assert "Workers (Astra's sub-agents)" in final["report"]
    assert "container_scan: gpt-test-worker at high effort, astra (a scan needs care)" in final["report"]


def test_a_failed_assignment_falls_back_to_the_orchestrator_defaults(tmp_path, worker_env):
    astra = FailingAssignmentAstra()
    graph = build_graph(MemorySaver(), jev=FakeJev(), astra=astra, commands=_echo())
    final = drive(graph, initial_state(str(tmp_path)), _resume_for_test, echo=False)
    plan = final["worker_plan"]
    assert {worker["model"] for worker in plan.values()} == {"gpt-orchestrator"}
    assert all(worker["assigned_by"] == "default" and "assignment failed" in worker["rationale"] for worker in plan.values())
    assert ("generate_artifacts", "container_scan", "gpt-orchestrator", "low") in astra.calls


def test_the_failed_stage_is_diagnosed_by_its_own_worker(tmp_path, worker_env):
    astra = OrchestratingAstra()
    graph = build_graph(MemorySaver(), jev=FakeJev(), astra=astra, commands=_fail())
    final = drive(graph, initial_state(str(tmp_path)), _resume_for_test, echo=False)
    assert final["diagnosis"]
    diagnoses = [call for call in astra.calls if call[0] == "diagnose"]
    assert diagnoses and diagnoses[0][2:] == ("gpt-orchestrator", "minimal"), diagnoses


def test_openai_astra_builds_workers_from_the_assignment(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-orchestrator")
    monkeypatch.setenv("ORCHESTRATOR_ASTRA_REASONING", "low")
    planner = llm.OpenAIAstra(temperature=0.3)
    worker = planner.worker({"stage": "sbom", "model": "gpt-test-worker", "reasoning_effort": "high"})
    assert worker.describe() == {"model": "gpt-test-worker", "reasoning_effort": "high", "stage": "sbom"}
    assert planner.worker({}).describe() == {"model": "gpt-orchestrator", "reasoning_effort": "low", "stage": ""}
    assert planner.worker({"reasoning_effort": ""}).reasoning_effort == ""
    seen: list[dict] = []

    def fake_invoke(model_name, schema, system, payload, *, temperature=None, reasoning=None):
        seen.append({"model": model_name, "schema": schema.__name__, "temperature": temperature, "reasoning": reasoning})
        return StageBrief(summary="ok", watch_for="nothing")

    monkeypatch.setattr(llm, "invoke_structured", fake_invoke)
    worker.brief({"stage": "sbom"})
    worker.generate_artifacts({"stage": {"name": "sbom"}})
    assert seen[0] == {"model": "gpt-test-worker", "schema": "StageBrief", "temperature": None, "reasoning": "high"}
    assert seen[1] == {"model": "gpt-test-worker", "schema": "ArtifactSet", "temperature": 0.3, "reasoning": "high"}


def test_a_worker_whose_model_is_unavailable_falls_back_to_the_orchestrator_model_once(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-orchestrator")
    calls: list[str] = []

    def fake_invoke(model_name, schema, system, payload, *, temperature=None, reasoning=None):
        calls.append(model_name)
        if model_name == "gpt-retired":
            raise RuntimeError("gpt-retired structured output failed: Error code: 404 - model_not_found: The model `gpt-retired` does not exist")
        return StageBrief(summary="ok", watch_for="nothing")

    monkeypatch.setattr(llm, "invoke_structured", fake_invoke)
    worker = llm.AstraWorker("gpt-retired", "low")
    assert worker.brief({"stage": "build"}).summary == "ok"
    assert calls == ["gpt-retired", "gpt-orchestrator"] and worker.model == "gpt-orchestrator"
    other = llm.AstraWorker("gpt-retired", "low")
    monkeypatch.setattr(llm, "invoke_structured", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gpt-retired structured output failed: bad payload")))
    with pytest.raises(RuntimeError, match="bad payload"):
        other.brief({"stage": "build"})


def test_worker_models_come_from_the_env_with_the_6_1_pair_as_default(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_ASTRA_WORKER_MODELS", raising=False)
    assert llm.astra_worker_models() == ["gpt-6.1-terra", "gpt-6.1-sol"]
    monkeypatch.setenv("ORCHESTRATOR_ASTRA_WORKER_MODELS", " gpt-a, gpt-b ,gpt-a,, ")
    assert llm.astra_worker_models() == ["gpt-a", "gpt-b"]
    monkeypatch.setenv("ORCHESTRATOR_ASTRA_WORKER_MODELS", "")
    assert llm.astra_worker_models() == []
    assert config.astra_model()
