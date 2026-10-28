from __future__ import annotations

import operator
import time
from typing import Annotated, TypedDict

import pytest
from fastapi.testclient import TestClient
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.main import app
from orchestrator.checkpoints import open_checkpointer
from orchestrator.dispatch import workflows_for
from orchestrator.graph import build_graph, interrupt_payload
from orchestrator.schemas import (
    CandidatePipeline,
    EcosystemQuestion,
    FollowUpDecision,
    FollowUpSet,
    PlanProposal,
    RankDecision,
    StageSpec,
)
from orchestrator.session import SessionService
from orchestrator.state import initial_state
from tests.test_orchestrator import FakeAstra, FakeJev, GeneratedAstra, _echo, _thread


def _wait(service: SessionService, phase: str, timeout: float = 8) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = service.snapshot()
        if last["phase"] == phase:
            return last
        if last["phase"] == "error":
            raise AssertionError(last["error"])
        time.sleep(0.02)
    raise AssertionError(last)


def _answers(view: dict) -> dict[str, str]:
    return {question["id"]: f"test-answer-{question['id']}" for question in view["questions"]}


class ShortAstra(FakeAstra):
    def __init__(self) -> None:
        super().__init__(
            [
                StageSpec(name="build", depends_on=[], purpose="compile"),
                StageSpec(name="test", depends_on=["build"], purpose="unit tests"),
            ]
        )

    def propose(self, payload: dict) -> PlanProposal:
        short = self.stages
        long = [
            StageSpec(name="build", depends_on=[], purpose="compile"),
            StageSpec(name="test", depends_on=["build"], purpose="unit tests"),
            StageSpec(name="package", depends_on=["build"], purpose="artifact"),
            StageSpec(name="scanners", depends_on=["build"], purpose="sast"),
            StageSpec(name="dast", depends_on=["test"], purpose="dast"),
            StageSpec(name="release", depends_on=["package"], purpose="release"),
            StageSpec(name="deploy", depends_on=["release"], purpose="deploy"),
        ]
        return PlanProposal(
            candidates=[
                CandidatePipeline(id="short", summary="build and test", stages=short),
                CandidatePipeline(id="long", summary="includes stages that must not run", stages=long),
            ]
        )


class PickShort(FakeJev):
    def rank(self, payload: dict) -> RankDecision:
        return RankDecision(
            chosen_id="short",
            ordered_ids=[item["id"] for item in payload["candidates"]],
            drop_stages=[],
            rationale="the short pipeline",
        )


def test_http_answers_are_required(tmp_path):
    service = SessionService(
        db_path=str(tmp_path / "session.sqlite"),
        jev=FakeJev(),
        astra=FakeAstra(),
        commands=_echo(),
        repo_root=str(tmp_path),
    )
    with TestClient(app) as client:
        original = app.state.sessions
        app.state.sessions = service
        try:
            started = client.post("/api/orchestrator/session")
            assert started.status_code == 200
            view = _wait(service, "questions")
            assert view["questions"]
            blank = client.post(
                "/api/orchestrator/session/answer",
                json={"gate": "ecosystem_qa", "answers": {}},
            )
            assert blank.status_code == 400
            assert service.snapshot()["phase"] == "questions"
            accepted = client.post(
                "/api/orchestrator/session/answer",
                json={"gate": "ecosystem_qa", "answers": _answers(view)},
            )
            assert accepted.status_code == 200
            approval = _wait(service, "approval")
            assert approval["run_id"] == started.json()["run_id"]
            assert approval["pipeline"]["stages"]
        finally:
            service.close()
            app.state.sessions = original


def test_short_pipeline_does_not_run_undesignated_stages(tmp_path):
    service = SessionService(
        db_path=str(tmp_path / "short.sqlite"),
        jev=PickShort(),
        astra=ShortAstra(),
        commands=_echo(),
        repo_root=str(tmp_path),
    )
    with TestClient(app) as client:
        original = app.state.sessions
        app.state.sessions = service
        try:
            client.post("/api/orchestrator/session")
            view = _wait(service, "questions")
            client.post(
                "/api/orchestrator/session/answer",
                json={"gate": "ecosystem_qa", "answers": _answers(view)},
            )
            approval = _wait(service, "approval")
            names = [stage["name"] for stage in approval["pipeline"]["stages"]]
            assert names == ["build", "test"]
            client.post(
                "/api/orchestrator/session/answer",
                json={"gate": "pipeline_approval", "decision": "approved"},
            )
            finished = _wait(service, "results")
            report = finished["report"]
            assert report["approved_stages"] == ["build", "test"]
            assert {row["stage"] for row in report["stage_evidence"]} == {"build", "test"}
            ran = {row["stage"] for row in report["stage_results"]}
            assert ran == {"build", "test"}
            dispatched = workflows_for(report["approved_stages"])
            assert dispatched == ["build.yml", "test.yml"]
            for banned in ("package", "scanners", "dast", "release", "deploy"):
                assert banned not in ran
                assert f"{banned}.yml" not in dispatched
            viewed = client.post("/api/orchestrator/session/viewed")
            assert viewed.status_code == 200
            assert viewed.json()["results_viewed"] is True
        finally:
            service.close()
            app.state.sessions = original


class FollowUpAstra(ShortAstra):
    def follow_up(self, payload: dict) -> FollowUpSet:
        if int(payload.get("rounds") or 0) == 1:
            return FollowUpSet(questions=[EcosystemQuestion(id="scan_policy", prompt="Which severity fails the run?", reason="follow-up")])
        return FollowUpSet(questions=[])


class FollowUpJev(PickShort):
    def follow_up(self, payload: dict) -> FollowUpDecision:
        if int(payload.get("rounds") or 0) == 1:
            return FollowUpDecision(action="ask_more", rationale="one more round")
        return FollowUpDecision(action="proceed", rationale="enough")


def test_follow_up_gate_round_trip_over_http(tmp_path):
    service = SessionService(
        db_path=str(tmp_path / "follow.sqlite"),
        jev=FollowUpJev(),
        astra=FollowUpAstra(),
        commands=_echo(),
        repo_root=str(tmp_path),
    )
    with TestClient(app) as client:
        original = app.state.sessions
        app.state.sessions = service
        try:
            client.post("/api/orchestrator/session")
            view = _wait(service, "questions")
            client.post("/api/orchestrator/session/answer", json={"gate": "ecosystem_qa", "answers": _answers(view)})
            follow = _wait(service, "follow_up")
            assert follow["gate"] == "follow_up_qa" and follow["clarify_round"] == 2
            assert [question["id"] for question in follow["questions"]] == ["scan_policy"]
            wrong_gate = client.post("/api/orchestrator/session/answer", json={"gate": "ecosystem_qa", "answers": _answers(follow)})
            assert wrong_gate.status_code == 400
            accepted = client.post("/api/orchestrator/session/answer", json={"gate": "follow_up_qa", "answers": _answers(follow)})
            assert accepted.status_code == 200
            approval = _wait(service, "approval")
            assert [stage["name"] for stage in approval["pipeline"]["stages"]] == ["build", "test"]
            assert approval["candidates"] and approval["candidates"][0]["id"] == "short"
            assert approval["artifacts"] == {} and approval["runner_plan"]["build"]["runner"] == "local"
            assert approval["retrieval"]["index_id"]
        finally:
            service.close()
            app.state.sessions = original


def test_approval_is_refused_while_an_artifact_has_policy_errors(tmp_path):
    service = SessionService(db_path=str(tmp_path / "policy.sqlite"), jev=FakeJev(), astra=FakeAstra(), commands=_echo(), repo_root=str(tmp_path))
    service.view["phase"] = "approval"
    service.view["artifacts"] = {"container_scan": [{"path": "ci/container_scan.sh", "kind": "shell", "policy": {"ok": False, "errors": 1, "warns": 0}}]}
    with pytest.raises(ValueError, match="policy errors"):
        service.submit({"gate": "pipeline_approval", "decision": "approved"})
    assert service.view["phase"] == "approval"
    rejected = service.submit({"gate": "pipeline_approval", "decision": "rejected"})
    assert rejected["phase"] == "running"


def test_artifacts_and_episodes_endpoints_follow_a_generated_run(tmp_path):
    service = SessionService(
        db_path=str(tmp_path / "generated.sqlite"),
        jev=FakeJev(),
        astra=GeneratedAstra(),
        commands=_echo(),
        repo_root=str(tmp_path),
    )
    with TestClient(app) as client:
        original = app.state.sessions
        app.state.sessions = service
        try:
            assert client.get("/api/orchestrator/episodes").json() == []
            assert client.get("/api/orchestrator/session/artifacts").json() == {}
            client.post("/api/orchestrator/session")
            view = _wait(service, "questions")
            client.post("/api/orchestrator/session/answer", json={"gate": "ecosystem_qa", "answers": _answers(view)})
            approval = _wait(service, "approval")
            assert approval["candidates"][0]["mode"] == "stochastic" and approval["candidates"][0]["chosen"]
            assert {stage["runner"] for stage in approval["stages"]} == {"local"}

            listing = client.get("/api/orchestrator/session/artifacts")
            assert listing.status_code == 200
            records = listing.json()["container_scan"]
            assert {record["kind"] for record in records} == {"shell", "env_example"}
            assert all(record["policy"]["ok"] and record["content"] for record in records)
            index = next(position for position, record in enumerate(records) if record["kind"] == "shell")
            text = client.get(f"/api/orchestrator/session/artifacts/container_scan/{index}")
            assert text.status_code == 200 and text.headers["content-type"].startswith("text/plain")
            assert text.text == records[index]["content"]
            assert client.get("/api/orchestrator/session/artifacts/container_scan/99").status_code == 404
            assert client.get("/api/orchestrator/session/artifacts/nope/0").status_code == 404

            client.post("/api/orchestrator/session/answer", json={"gate": "pipeline_approval", "decision": "approved"})
            _wait(service, "uat", timeout=20)
            client.post("/api/orchestrator/session/answer", json={"gate": "uat_signoff", "decision": "approved"})
            results = _wait(service, "results", timeout=20)
            rows = {row["stage"]: row for row in results["report"]["stage_evidence"]}
            assert rows["container_scan"]["kind"] == "generated" and rows["container_scan"]["runner"] == "local"
            assert rows["container_scan"]["family"] == "scan" and rows["container_scan"]["plan"] == {}
            assert rows["build"]["kind"] == "fixed" and rows["build"]["family"] == "build"
            assert results["artifacts"]["container_scan"][index]["content"] == text.text

            episodes = client.get("/api/orchestrator/episodes?limit=5")
            assert episodes.status_code == 200
            assert [item["run_id"] for item in episodes.json()] == [results["run_id"]]
            assert episodes.json()[0]["chosen"]["mode"] == "stochastic"
            assert "container_scan" in episodes.json()[0]["chosen"]["stages"]
            assert client.get("/api/orchestrator/episodes?limit=0").status_code == 422
        finally:
            service.close()
            app.state.sessions = original


def test_sqlite_thread_survives_a_pause_and_resume(tmp_path):
    db_path = str(tmp_path / "paused.sqlite")
    state = initial_state(str(tmp_path))
    thread = _thread(state["run_id"])
    with open_checkpointer(db_path) as saver:
        graph = build_graph(saver, jev=FakeJev(), astra=FakeAstra(), commands=_echo())
        graph.invoke(state, thread)
        payload = interrupt_payload(graph, thread)
        assert payload["gate"] == "ecosystem_qa"
        saved = graph.get_state(thread)
        assert saved.values["run_id"] == state["run_id"]
        assert saved.values["jev_decisions"]
        tuple_ = saver.get_tuple(thread)
        channels = tuple_.checkpoint["channel_values"]
        assert channels["run_id"] == state["run_id"]
        assert channels["trace"]
    with open_checkpointer(db_path) as saver:
        graph = build_graph(saver, jev=FakeJev(), astra=FakeAstra(), commands=_echo())
        restored = graph.get_state(thread)
        assert restored.config["configurable"]["thread_id"] == state["run_id"]
        assert restored.values["open_questions"]
        graph.invoke(
            Command(
                resume={
                    question["id"]: f"test-answer-{question['id']}"
                    for question in payload["questions"]
                }
            ),
            thread,
        )
        resumed = graph.get_state(thread)
        assert resumed.config["configurable"]["thread_id"] == state["run_id"]
        assert resumed.values["answers"]["env_target"] == "test-answer-env_target"
        assert resumed.values["jev_decisions"]
        assert interrupt_payload(graph, thread)["gate"] == "pipeline_approval"
        channels = saver.get_tuple(thread).checkpoint["channel_values"]
        assert channels["answers"]["env_target"] == "test-answer-env_target"
        assert channels["pipeline"]["stages"]


def test_internal_agent_state_round_trips_through_the_same_sqlite_file(tmp_path):
    db_path = str(tmp_path / "agent.sqlite")

    class Memory(TypedDict):
        note: str
        hops: Annotated[int, operator.add]

    def remember(state: Memory) -> dict:
        return {"note": "kept", "hops": 1}

    thread = {"configurable": {"thread_id": "agent-internal"}}
    with open_checkpointer(db_path) as saver:
        builder = StateGraph(Memory)
        builder.add_node("remember", remember)
        builder.add_edge(START, "remember")
        builder.add_edge("remember", END)
        builder.compile(checkpointer=saver).invoke({"note": "", "hops": 0}, thread)
    with open_checkpointer(db_path) as saver:
        builder = StateGraph(Memory)
        builder.add_node("remember", remember)
        builder.add_edge(START, "remember")
        builder.add_edge("remember", END)
        restored = builder.compile(checkpointer=saver).get_state(thread)
        assert restored.values["note"] == "kept"
        assert restored.values["hops"] == 1
        channels = saver.get_tuple(thread).checkpoint["channel_values"]
        assert channels["note"] == "kept"
