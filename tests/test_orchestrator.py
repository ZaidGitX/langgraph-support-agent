from __future__ import annotations

import json
import operator
import sys
import time
from typing import Annotated, TypedDict

import pytest
from langchain_typesafe import ChoiceAnswer, ClassifierResponse, NoulAnswer, Usage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send

from app import config
from orchestrator.checkpoints import open_checkpointer
from orchestrator.demo import drive
from orchestrator.graph import (
    build_graph,
    interrupt_payload,
    route_after_discover,
)
from orchestrator.guardrails import apply_guardrails
from orchestrator.llm import TypeSafeJev
from orchestrator.local_ops import deploy
from orchestrator.schemas import (
    CandidatePipeline,
    ClarifyDecision,
    EcosystemQuestion,
    GateDecision,
    PlanProposal,
    QuestionSet,
    RankDecision,
    StageBrief,
    StageSpec,
)
from orchestrator.state import STAGE_NAMES, initial_state


def _echo() -> dict[str, list[list[str]]]:
    command = [sys.executable, "-c", "print('ok')"]
    return {name: [command] for name in STAGE_NAMES}


def _fail() -> dict[str, list[list[str]]]:
    command = [sys.executable, "-c", "import sys; raise SystemExit(1)"]
    return {name: [command] for name in STAGE_NAMES}


class FakeAstra:
    def __init__(self, stages: list[StageSpec] | None = None) -> None:
        self.stages = stages or [
            StageSpec(name="build", depends_on=[], purpose="compile"),
            StageSpec(name="test", depends_on=["build"], purpose="unit tests"),
            StageSpec(name="package", depends_on=["build"], purpose="artifact"),
            StageSpec(name="uat", depends_on=["test", "package"], purpose="sign-off"),
        ]

    def write_questions(self, payload: dict) -> QuestionSet:
        return QuestionSet(
            questions=[
                EcosystemQuestion(
                    id="env_target",
                    prompt="Which environment should this run target?",
                    reason="No deployment-target document is in the checkout.",
                ),
                EcosystemQuestion(
                    id="test_depth",
                    prompt="What test depth should this run use?",
                    reason="The release bar is not written down.",
                ),
                EcosystemQuestion(
                    id="release_policy",
                    prompt="What release policy should this run follow?",
                    reason="No release-policy file is in the checkout.",
                ),
            ]
        )

    def brief(self, payload: dict) -> StageBrief:
        return StageBrief(
            summary=f"Brief for {payload.get('stage')}.",
            watch_for="A non-zero exit needs a bounded retry.",
        )

    def propose(self, payload: dict) -> PlanProposal:
        primary = CandidatePipeline(id="primary", summary="primary", stages=self.stages)
        alt = CandidatePipeline(id="alt", summary="shorter", stages=self.stages[:1])
        return PlanProposal(candidates=[primary, alt])

    def diagnose(self, payload: dict):
        from orchestrator.schemas import Diagnosis

        return Diagnosis(summary="the command exited non-zero", retry_advice="retry once")


class FakeJev:
    def clarify(self, payload: dict) -> ClarifyDecision:
        if payload.get("open_questions"):
            return ClarifyDecision(action="ask", rationale="gaps remain")
        return ClarifyDecision(action="skip", rationale="no gaps")

    def rank(self, payload: dict) -> RankDecision:
        ids = [item["id"] for item in payload["candidates"]]
        return RankDecision(
            chosen_id=ids[0],
            ordered_ids=ids,
            drop_stages=[],
            rationale="first candidate",
        )

    def gate(self, payload: dict) -> GateDecision:
        if payload.get("failures") or payload.get("blocked"):
            return GateDecision(
                route="remediate",
                retry=True,
                retry_stages=list(payload.get("failures") or []),
                replan=False,
                rationale="retry the failure",
            )
        return GateDecision(
            route="pass",
            retry=False,
            retry_stages=[],
            replan=False,
            rationale="evidence passed",
        )


def _resume_for_test(payload: dict) -> dict:
    """Test-only answers. The CI workflow does not call this."""
    gate = payload.get("gate")
    if gate == "ecosystem_qa":
        return {
            question["id"]: f"test-answer-{question['id']}"
            for question in payload.get("questions") or []
        }
    if gate in {"pipeline_approval", "uat_signoff"}:
        return {"decision": "approved"}
    raise AssertionError(f"unexpected gate {gate}")


def _thread(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}, "recursion_limit": 40}


def test_stage_results_append_reducer():
    class Sample(TypedDict):
        stage_results: Annotated[list[str], operator.add]

    def fan(_state):
        return [Send("left", {}), Send("right", {})]

    builder = StateGraph(Sample)
    builder.add_node("start", lambda _state: {})
    builder.add_node("left", lambda _state: {"stage_results": ["left"]})
    builder.add_node("right", lambda _state: {"stage_results": ["right"]})
    builder.add_node("join", lambda _state: {"stage_results": ["join"]})
    builder.add_edge(START, "start")
    builder.add_conditional_edges("start", fan, ["left", "right"])
    builder.add_edge("left", "join")
    builder.add_edge("right", "join")
    builder.add_edge("join", END)
    result = builder.compile().invoke({"stage_results": []})
    assert result["stage_results"][-1] == "join"
    assert set(result["stage_results"][:2]) == {"left", "right"}


def test_route_skips_clarify_when_there_are_no_gaps():
    assert route_after_discover({"clarify_decision": {"action": "skip"}, "open_questions": []}) == "plan_pipeline"


def test_route_asks_when_jev_selects_ask():
    state = {
        "clarify_decision": {"action": "ask"},
        "open_questions": [{"id": "env_target", "prompt": "target?", "reason": "missing"}],
    }
    assert route_after_discover(state) == "clarify_with_user"


def test_graph_skips_clarify_node_when_answers_close_the_gaps(tmp_path):
    checkpointer = MemorySaver()
    graph = build_graph(checkpointer, jev=FakeJev(), astra=FakeAstra(), commands=_echo())
    state = initial_state(str(tmp_path))
    state["answers"] = {
        "env_target": "local",
        "test_depth": "unit",
        "release_policy": "Stop at UAT.",
    }
    graph.invoke(state, _thread(state["run_id"]))
    snapshot = graph.get_state(_thread(state["run_id"]))
    assert "clarify_with_user" not in snapshot.values["trace"]
    assert interrupt_payload(graph, _thread(state["run_id"]))["gate"] == "pipeline_approval"


def test_interrupt_resumes_from_sqlite(tmp_path):
    db_path = str(tmp_path / "run.sqlite")
    state = initial_state(str(tmp_path))
    thread = _thread(state["run_id"])
    with open_checkpointer(db_path) as saver:
        graph = build_graph(saver, jev=FakeJev(), astra=FakeAstra(), commands=_echo())
        graph.invoke(state, thread)
        payload = interrupt_payload(graph, thread)
        assert payload is not None
        assert payload["gate"] == "ecosystem_qa"
    with open_checkpointer(db_path) as saver:
        graph = build_graph(saver, jev=FakeJev(), astra=FakeAstra(), commands=_echo())
        graph.invoke(Command(resume=_resume_for_test(payload)), thread)
        resumed = graph.get_state(thread)
        assert resumed.values["answers"]["env_target"] == "test-answer-env_target"
        assert resumed.config["configurable"]["thread_id"] == state["run_id"]
        assert interrupt_payload(graph, thread)["gate"] == "pipeline_approval"


def test_bounded_retry_stops(tmp_path):
    checkpointer = MemorySaver()
    stages = [StageSpec(name="build", depends_on=[], purpose="fails")]
    graph = build_graph(
        checkpointer,
        jev=FakeJev(),
        astra=FakeAstra(stages),
        commands=_fail(),
    )
    state = initial_state(str(tmp_path))
    values = drive(graph, state, _resume_for_test, echo=False)
    build_rows = [row for row in values["stage_results"] if row["stage"] == "build"]
    assert len(build_rows) == 2
    assert values["attempt"]["build"] == 2
    assert values["gate"]["route"] == "escalate"
    assert "guardrail" in values["gate"]["rationale"]
    assert values["status"] == "escalated"
    assert all(row["stage"] != "production" for row in values["stage_results"])


def test_guardrail_stops_retry_at_attempt_cap():
    decision = GateDecision(
        route="remediate",
        retry=True,
        retry_stages=["build"],
        replan=False,
        rationale="again",
    )
    state = {
        "attempt": {"build": 2},
        "hops": 1,
        "started_at": time.time(),
        "pipeline": {"id": "p", "stages": [{"name": "build"}]},
        "stage_results": [
            {"stage": "build", "plan_id": "p", "status": "fail", "blocked": False}
        ],
        "env_state": {},
    }
    guarded = apply_guardrails(state, decision)
    assert guarded.route == "escalate"
    assert guarded.retry is False


def test_no_edge_past_uat():
    graph = build_graph(MemorySaver(), jev=FakeJev(), astra=FakeAstra(), commands=_echo())
    drawn = graph.get_graph()
    names = set(drawn.nodes)
    assert "production" not in names
    assert "prod" not in names
    targets = []
    for edge in drawn.edges:
        source = getattr(edge, "source", None)
        target = getattr(edge, "target", None)
        if source is None:
            source, target = edge[0], edge[1]
        if source == "uat":
            targets.append(target)
    assert targets == ["quality_gate"]
    schema = GateDecision.model_json_schema()
    assert "production" not in schema["properties"]["route"]["enum"]


def test_deploy_refuses_production(tmp_path):
    code = deploy(tmp_path, "production")
    assert code == 2
    plan = json.loads((tmp_path / "dist" / "deploy-plan.json").read_text(encoding="utf-8"))
    assert plan["refused"] is True
    assert plan["past_uat"] is False
    assert plan["aws_called"] is False


def test_scripted_fake_run_stops_at_uat(tmp_path):
    graph = build_graph(MemorySaver(), jev=FakeJev(), astra=FakeAstra(), commands=_echo())
    values = drive(graph, initial_state(str(tmp_path)), _resume_for_test, echo=False)
    assert values["status"] == "passed"
    assert values["env_state"]["past_uat"] is False
    assert values["approvals"]["uat"] == "approved"
    assert values["approvals"]["pipeline"] == "approved"
    assert "clarify_with_user" in values["trace"]
    names = {row["stage"] for row in values["stage_results"]}
    assert {"build", "test", "package", "uat"} <= names
    assert "production" not in names


def test_model_bindings(monkeypatch):
    monkeypatch.delenv("TYPESAFE_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-6-astra")
    assert config.jev_model() == "jev-latest"
    assert config.astra_model() == "gpt-6-astra"


class _Classifier:
    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[dict] = []
        self.base_url = "https://api.typesafe.ai"

    def invoke(self, payload):
        self.calls.append(payload)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _response(answers: dict) -> ClassifierResponse:
    return ClassifierResponse(
        model="jev-latest",
        request_id="req_unit",
        usage=Usage(input_tokens=3, output_tokens=1),
        answers=answers,
    )


def test_jev_clarify_uses_noul_and_choice():
    classifier = _Classifier(
        _response(
            {
                "needs_human": NoulAnswer(type="noul", noul=0.12),
                "action": ChoiceAnswer(
                    type="choice",
                    choice="skip",
                    probabilities={"skip": 0.91, "ask": 0.09},
                    confidence=0.82,
                ),
            }
        )
    )
    decision = TypeSafeJev(classifier=classifier).clarify({"open_questions": []})
    questions = classifier.calls[0]["questions"]
    assert questions["needs_human"].type == "noul"
    assert questions["action"].type == "choice"
    assert set(questions["action"].criteria) == {"skip", "ask"}
    assert decision.action == "skip"
    assert "Noul" in decision.rationale
    assert "Choice" in decision.rationale


def test_jev_rank_and_gate_use_choice():
    rank_classifier = _Classifier(
        _response(
            {
                "pipeline": ChoiceAnswer(
                    type="choice",
                    choice="primary",
                    probabilities={"primary": 0.8, "alt": 0.2},
                    confidence=0.7,
                )
            }
        )
    )
    ranked = TypeSafeJev(classifier=rank_classifier).rank(
        {
            "candidates": [
                {"id": "primary", "summary": "build test uat", "stages": [{"name": "build"}]},
                {"id": "alt", "summary": "shorter", "stages": [{"name": "test"}]},
            ]
        }
    )
    assert ranked.chosen_id == "primary"
    assert rank_classifier.calls[0]["questions"]["pipeline"].type == "choice"
    assert set(rank_classifier.calls[0]["questions"]["pipeline"].criteria) == {"primary", "alt"}

    gate_classifier = _Classifier(
        _response(
            {
                "route": ChoiceAnswer(
                    type="choice",
                    choice="pass",
                    probabilities={"pass": 0.9, "remediate": 0.05, "escalate": 0.03, "skip": 0.02},
                    confidence=0.88,
                )
            }
        )
    )
    gated = TypeSafeJev(classifier=gate_classifier).gate({"failures": [], "evidence": []})
    criteria = gate_classifier.calls[0]["questions"]["route"].criteria
    assert set(criteria) == {"pass", "remediate", "escalate", "skip"}
    assert gated.route == "pass"
    assert gated.retry is False


def test_typesafe_failure_is_raised_without_openai_fallback(monkeypatch):
    classifier = _Classifier(RuntimeError("typesafe unavailable"))

    def _boom(*_args, **_kwargs):
        raise AssertionError("ChatOpenAI must not be used when TypeSafe fails")

    monkeypatch.setattr("orchestrator.llm.ChatOpenAI", _boom)
    with pytest.raises(RuntimeError, match="TypeSafeClassifier call failed"):
        TypeSafeJev(classifier=classifier).clarify({"open_questions": []})
