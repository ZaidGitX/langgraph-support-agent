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
    machine_report,
    route_after_discover,
    route_after_follow_up,
)
from orchestrator.guardrails import apply_guardrails
from orchestrator.llm import TypeSafeJev
from orchestrator.local_ops import deploy
from orchestrator.schemas import (
    ArtifactReview,
    ArtifactSet,
    CandidatePipeline,
    ClarifyDecision,
    DeployDoc,
    EcosystemQuestion,
    FollowUpDecision,
    FollowUpSet,
    GateDecision,
    GeneratedArtifact,
    PlanProposal,
    QuestionSet,
    RankDecision,
    RunnerDecision,
    StageBrief,
    StageSpec,
)
from orchestrator.state import STAGE_NAMES, initial_state

ECHO_SCRIPT = "#!/usr/bin/env bash\nset -euo pipefail\necho generated-ok\n"


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

    # Sprint 3 doubles: deterministic, offline.
    def follow_up(self, payload: dict) -> FollowUpSet:
        if int(payload.get("rounds") or 0) == 1 and payload.get("retrieved"):
            return FollowUpSet(
                questions=[
                    EcosystemQuestion(
                        id="scan_policy",
                        prompt="Which scan severity should fail the run?",
                        reason="The retrieved Dockerfile suggests a container scan.",
                    )
                ]
            )
        return FollowUpSet(questions=[])

    def generate_artifacts(self, payload: dict) -> ArtifactSet:
        stage = str(payload["stage"]["name"])
        return ArtifactSet(
            stage=stage,
            artifacts=[GeneratedArtifact(kind="shell", path=f"ci/{stage}.sh", purpose="echo", content=ECHO_SCRIPT)],
        )

    def write_docs(self, payload: dict) -> DeployDoc:
        return DeployDoc(path="docs/deploy/local-local.md", content="# demo deployment\n")


class GeneratedAstra(FakeAstra):
    """A stochastic candidate with a generated container_scan stage on the local runner."""

    def propose(self, payload: dict) -> PlanProposal:
        stochastic = CandidatePipeline(
            id="scan-route",
            summary="build, test, container scan, uat",
            mode="stochastic",
            stages=[
                StageSpec(name="build", purpose="compile"),
                StageSpec(name="test", depends_on=["build"], purpose="unit tests"),
                StageSpec(name="container_scan", depends_on=["build"], purpose="scan the image", runner="local", provider="trivy"),
                StageSpec(name="uat", depends_on=["test", "container_scan"], purpose="sign-off"),
            ],
        )
        plain = CandidatePipeline(
            id="plain",
            summary="build, test, uat",
            stages=[
                StageSpec(name="build", purpose="compile"),
                StageSpec(name="test", depends_on=["build"], purpose="unit tests"),
                StageSpec(name="uat", depends_on=["test"], purpose="sign-off"),
            ],
        )
        return PlanProposal(candidates=[stochastic, plain])


class PlannedCloudAstra(FakeAstra):
    """A stochastic candidate whose deploy_dev stage wants AWS CodeBuild (no credentials)."""

    def propose(self, payload: dict) -> PlanProposal:
        cloud = CandidatePipeline(
            id="aws-route",
            summary="build, test, deploy to dev on CodeBuild, uat",
            mode="stochastic",
            environment={"tier": "dev", "platform": "aws"},
            stages=[
                StageSpec(name="build", purpose="compile"),
                StageSpec(name="test", depends_on=["build"], purpose="unit tests"),
                StageSpec(name="deploy_dev", depends_on=["test"], purpose="deploy", runner="aws_codebuild", environment={"tier": "dev", "platform": "aws"}),
                StageSpec(name="uat", depends_on=["deploy_dev"], purpose="sign-off"),
            ],
        )
        plain = CandidatePipeline(
            id="plain",
            summary="build, test, uat",
            stages=[
                StageSpec(name="build", purpose="compile"),
                StageSpec(name="test", depends_on=["build"], purpose="unit tests"),
                StageSpec(name="uat", depends_on=["test"], purpose="sign-off"),
            ],
        )
        return PlanProposal(candidates=[cloud, plain])


class BadArtifactAstra(GeneratedAstra):
    """Writes an artifact the policy must reject, on every attempt."""

    def generate_artifacts(self, payload: dict) -> ArtifactSet:
        stage = str(payload["stage"]["name"])
        bad = "#!/usr/bin/env bash\nset -euo pipefail\npython -m orchestrator.local_ops deploy --target production\n"
        return ArtifactSet(stage=stage, artifacts=[GeneratedArtifact(kind="shell", path=f"ci/{stage}.sh", purpose="bad", content=bad)])


class FakeJev:
    def clarify(self, payload: dict) -> ClarifyDecision:
        if payload.get("open_questions"):
            return ClarifyDecision(action="ask", rationale="gaps remain")
        return ClarifyDecision(action="skip", rationale="no gaps")

    def follow_up(self, payload: dict) -> FollowUpDecision:
        if int(payload.get("rounds") or 0) == 1 and payload.get("retrieved"):
            return FollowUpDecision(action="ask_more", rationale="the retrieved code leaves the scan policy open")
        return FollowUpDecision(action="proceed", rationale="enough context")

    def runner(self, payload: dict) -> RunnerDecision:
        return RunnerDecision(
            runners={stage["name"]: stage["runner_options"][0] for stage in payload["stages"]},
            rationale="first option",
        )

    def artifact_review(self, payload: dict) -> ArtifactReview:
        ok = bool((payload.get("policy") or {}).get("ok"))
        left = int(payload.get("revisions_left") or 0)
        return ArtifactReview(verdict="accept" if ok else ("revise" if left else "reject"), rationale="policy result")

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
    if gate in {"ecosystem_qa", "follow_up_qa"}:
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
    assert route_after_discover({"clarify_decision": {"action": "skip"}, "open_questions": []}) == "follow_up"
    question = {"id": "scan_policy", "prompt": "severity?", "reason": "open"}
    assert route_after_follow_up({"open_questions": [question], "clarify_rounds": 1}) == "clarify_with_user"
    assert route_after_follow_up({"open_questions": [question], "clarify_rounds": 2}) == "plan_pipeline"
    assert route_after_follow_up({"open_questions": [], "clarify_rounds": 1}) == "plan_pipeline"


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


# ---------------------------------------------------------------- Sprint 3 phase 5


def _write_checkout(tmp_path) -> str:
    (tmp_path / "app").mkdir(exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("# uvicorn serves /status on port 8000\nfrom fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\nEXPOSE 8000\n", encoding="utf-8")
    return str(tmp_path)


def test_generated_stage_runs_a_local_echo_script(tmp_path):
    graph = build_graph(MemorySaver(), jev=FakeJev(), astra=GeneratedAstra(), commands=_echo())
    values = drive(graph, initial_state(str(tmp_path)), _resume_for_test, echo=False)
    assert values["status"] == "passed"
    assert values["pipeline"]["mode"] == "stochastic"
    assert [stage["name"] for stage in values["pipeline"]["stages"]] == ["build", "container_scan", "test", "uat"]
    assert "container_scan" in values["trace"] and "generate_artifacts" in values["trace"]
    row = next(item for item in values["stage_results"] if item["stage"] == "container_scan")
    assert row["kind"] == "generated" and row["runner"] == "local" and row["status"] == "pass"
    assert "generated-ok" in row["evidence"]
    assert row["checks"][0]["tool"] == "trivy"
    records = values["generated_artifacts"]["container_scan"]
    assert {record["kind"] for record in records} == {"shell", "env_example"}
    script = next(record for record in records if record["kind"] == "shell")
    assert len(script["sha256"]) == 64 and script["policy"]["ok"] is True
    assert script["stored_at"].startswith(str(tmp_path / "var" / "generated"))
    assert values["runner_plan"]["container_scan"]["runner"] == "local"
    assert values["artifact_review"]["container_scan"]["verdict"] == "accept"
    chosen = [item for item in values["candidates"] if item["chosen"]]
    assert chosen and chosen[0]["id"] == "scan-route" and chosen[0]["stages"] == ["build", "test", "container_scan", "uat"]
    assert {item["kind"] for item in values["jev_decisions"]} >= {"clarify", "rank", "runner", "artifact_review", "gate"}
    assert "Generated artifacts" in values["report"] and "ci/container_scan.sh" in values["report"]
    assert values["approvals"]["uat"] == "approved" and values["env_state"]["past_uat"] is False


def test_follow_up_round_asks_once_and_stops(tmp_path):
    root = _write_checkout(tmp_path)
    graph = build_graph(MemorySaver(), jev=FakeJev(), astra=FakeAstra(), commands=_echo())
    values = drive(graph, initial_state(root), _resume_for_test, echo=False)
    assert values["status"] == "passed"
    assert values["trace"].count("clarify_with_user") == 2
    assert values["trace"].count("follow_up") == 2
    assert values["clarify_rounds"] == 2
    assert values["answers"]["scan_policy"] == "test-answer-scan_policy"
    assert values["follow_up_decision"]["action"] == "proceed"
    kinds = [item["kind"] for item in values["jev_decisions"]]
    assert kinds.count("follow_up") == 2


def test_planned_cloud_stage_does_not_fail_the_run(tmp_path):
    graph = build_graph(MemorySaver(), jev=FakeJev(), astra=PlannedCloudAstra(), commands=_echo())
    values = drive(graph, initial_state(str(tmp_path)), _resume_for_test, echo=False)
    row = next(item for item in values["stage_results"] if item["stage"] == "deploy_dev")
    assert row["status"] == "planned" and row["runner"] == "aws_codebuild"
    assert row["plan"]["mode"] == "plan" and row["plan"]["would_run"][0][:3] == ["aws", "codebuild", "start-build"]
    assert {"AWS_REGION", "AWS_ACCESS_KEY_ID"} <= set(row["plan"]["missing"])
    assert values["gate"]["route"] == "pass"
    assert values["status"] == "passed"
    assert values["env_state"]["aws_called"] is False
    assert values["runner_plan"]["deploy_dev"]["mode"] == "plan"
    report = machine_report(values)
    assert report["planned_stages"] == ["deploy_dev"]
    assert "deploy_dev attempt 1 planned" in values["report"]


def test_artifact_review_reject_drops_the_stage(tmp_path):
    graph = build_graph(MemorySaver(), jev=FakeJev(), astra=BadArtifactAstra(), commands=_echo())
    values = drive(graph, initial_state(str(tmp_path)), _resume_for_test, echo=False)
    names = [stage["name"] for stage in values["pipeline"]["stages"]]
    assert "container_scan" not in names and names[-1] == "uat"
    assert any("container_scan" in item for item in values["pipeline"]["dropped_stages"])
    assert values["artifact_review"]["container_scan"]["verdict"] == "reject"
    assert values["artifact_review"]["container_scan"]["revisions"] == 1
    assert {row["rule"] for row in values["policy"]} >= {"prod.reference"}
    assert values["status"] == "passed"
    assert all(item["stage"] != "container_scan" for item in values["stage_results"])


def test_no_production_target_anywhere_after_a_generated_run(tmp_path):
    import json
    from pathlib import Path

    graph = build_graph(MemorySaver(), jev=FakeJev(), astra=GeneratedAstra(), commands=_echo())
    values = drive(graph, initial_state(str(tmp_path)), _resume_for_test, echo=False)
    for stage in values["pipeline"]["stages"]:
        assert stage["name"] not in {"production", "prod"}
        assert stage["environment"].get("tier") in {"local", "dev", "staging", "uat"}
    for records in values["generated_artifacts"].values():
        for record in records:
            text = Path(record["stored_at"]).read_text(encoding="utf-8")
            assert "--target production" not in text and "tier=production" not in text
    report = json.dumps(machine_report(values))
    assert '"production"' not in report


def test_jev_follow_up_uses_noul_and_choice():
    classifier = _Classifier(
        _response(
            {
                "needs_more_context": NoulAnswer(type="noul", noul=0.81),
                "action": ChoiceAnswer(type="choice", choice="ask_more", probabilities={"proceed": 0.2, "ask_more": 0.8}, confidence=0.7),
            }
        )
    )
    decision = TypeSafeJev(classifier=classifier).follow_up({"rounds": 1, "max_rounds": 2, "retrieved": {"docker": []}})
    questions = classifier.calls[0]["questions"]
    assert questions["needs_more_context"].type == "noul"
    assert set(questions["action"].criteria) == {"proceed", "ask_more"}
    assert decision.action == "ask_more" and "Noul" in decision.rationale
    capped = TypeSafeJev(classifier=classifier).follow_up({"rounds": 2, "max_rounds": 2})
    assert capped.action == "proceed" and "guardrail" in capped.rationale


def test_jev_runner_asks_one_choice_per_undecided_stage():
    classifier = _Classifier(
        _response(
            {
                "runner:container_scan": ChoiceAnswer(type="choice", choice="docker", probabilities={"local": 0.3, "docker": 0.7}, confidence=0.6),
            }
        )
    )
    decision = TypeSafeJev(classifier=classifier).runner(
        {
            "stages": [
                {"name": "container_scan", "runner_options": ["local", "docker"]},
                {"name": "deploy_dev", "runner_options": ["aws_codebuild"]},
                {"name": "uat", "runner_options": ["local", "docker"]},
            ],
            "runners_available": {"local": {"cli": True, "missing": [], "mode": "apply"}, "docker": {"cli": True, "missing": [], "mode": "apply"}, "aws_codebuild": {"cli": True, "missing": ["AWS_REGION"], "mode": "plan"}},
            "platform": "docker",
            "platform_runners": ["docker", "local"],
        }
    )
    asked = classifier.calls[0]["questions"]
    assert set(asked) == {"runner:container_scan"}
    assert "missing" not in asked["runner:container_scan"].criteria["docker"]
    assert decision.runners == {"container_scan": "docker", "deploy_dev": "aws_codebuild", "uat": "local"}


def test_jev_artifact_review_guards():
    classifier = _Classifier(
        _response({"verdict": ChoiceAnswer(type="choice", choice="accept", probabilities={"accept": 0.9, "revise": 0.05, "reject": 0.05}, confidence=0.8)})
    )
    jev = TypeSafeJev(classifier=classifier)
    assert jev.artifact_review({"policy": {"ok": True}, "revisions_left": 1}).verdict == "accept"
    revised = jev.artifact_review({"policy": {"ok": False}, "revisions_left": 1})
    assert revised.verdict == "revise" and "guardrail" in revised.rationale
    assert jev.artifact_review({"policy": {"ok": False}, "revisions_left": 0}).verdict == "reject"
    assert set(classifier.calls[0]["questions"]["verdict"].criteria) == {"accept", "revise", "reject"}


def test_jev_rank_criteria_mention_mode_and_readiness():
    classifier = _Classifier(
        _response({"pipeline": ChoiceAnswer(type="choice", choice="aws-route", probabilities={"aws-route": 0.6, "plain": 0.4}, confidence=0.5)})
    )
    TypeSafeJev(classifier=classifier).rank(
        {
            "candidates": [
                {"id": "aws-route", "summary": "cloud", "mode": "stochastic", "environment": {"tier": "dev", "platform": "aws"}, "stages": [{"name": "build"}], "readiness_note": "plan only: missing AWS_REGION"},
                {"id": "plain", "summary": "local", "stages": [{"name": "build"}]},
            ]
        }
    )
    criteria = classifier.calls[0]["questions"]["pipeline"].criteria
    assert "Mode: stochastic" in criteria["aws-route"] and "plan only: missing AWS_REGION" in criteria["aws-route"]
    assert "dev on aws" in criteria["aws-route"] and "Mode: deterministic" in criteria["plain"]
