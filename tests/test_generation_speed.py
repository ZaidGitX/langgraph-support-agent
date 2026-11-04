"""Generation runs the generated stages and the suite at once, Astra reasons at a capped effort,
and a CodeBuild build past the cap is stopped (the fixes before live run 3, 2026-11-04)."""

from __future__ import annotations

import threading
import time

import pytest
from langgraph.checkpoint.memory import MemorySaver
from openai import OpenAIError

from app import config
from orchestrator import llm, policy, templates
from orchestrator import suite as suite_module
from orchestrator.demo import drive
from orchestrator.graph import build_graph
from orchestrator.providers import aws as aws_module
from orchestrator.schemas import (
    CandidatePipeline,
    GeneratedArtifact,
    PlanProposal,
    StageBrief,
    StageSpec,
)
from orchestrator.state import initial_state
from tests.test_orchestrator import FakeAstra, FakeJev, _echo, _resume_for_test

SLOW = 0.5
GENERATED = ("container_scan", "sbom", "dockerfile_lint")


class SlowAstra(FakeAstra):
    """Three generated local stages; every generation sleeps so concurrency shows in the clock."""

    def __init__(self) -> None:
        super().__init__()
        self.started: list[tuple[str, float, str]] = []
        self._lock = threading.Lock()

    def propose(self, payload: dict) -> PlanProposal:
        route = CandidatePipeline(
            id="parallel-route",
            summary="build, three generated scans, test, uat",
            mode="stochastic",
            stages=[
                StageSpec(name="build", purpose="compile"),
                StageSpec(name="test", depends_on=["build"], purpose="unit tests"),
                StageSpec(name="container_scan", depends_on=["build"], purpose="scan the image", runner="local", provider="trivy"),
                StageSpec(name="sbom", depends_on=["build"], purpose="software bill of materials", runner="local", provider="syft"),
                StageSpec(name="dockerfile_lint", depends_on=["build"], purpose="lint the Dockerfile", runner="local", provider="hadolint"),
                StageSpec(name="uat", depends_on=["test", "container_scan", "sbom", "dockerfile_lint"], purpose="sign-off"),
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
        return PlanProposal(candidates=[route, plain])

    def generate_artifacts(self, payload: dict):
        with self._lock:
            self.started.append((str(payload["stage"]["name"]), time.monotonic(), threading.current_thread().name))
        time.sleep(SLOW)
        return super().generate_artifacts(payload)


def _run(tmp_path, astra: SlowAstra) -> dict:
    graph = build_graph(MemorySaver(), jev=FakeJev(), astra=astra, commands=_echo())
    return drive(graph, initial_state(str(tmp_path)), _resume_for_test, echo=False)


def _generated_in_pipeline_order(final: dict) -> list[str]:
    return [stage["name"] for stage in final["pipeline"]["stages"] if stage.get("kind") == "generated"]


def test_generated_stages_are_written_concurrently_and_recorded_in_pipeline_order(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_ASTRA_CONCURRENCY", "4")
    astra = SlowAstra()
    final = _run(tmp_path / "parallel", astra)
    starts = sorted(moment for _, moment, _ in astra.started)
    assert len(starts) == 3
    assert starts[-1] - starts[0] < SLOW, "the three generations should start before the first one finishes"
    assert len({thread for _, _, thread in astra.started}) >= 2
    ordered = _generated_in_pipeline_order(final)
    assert sorted(ordered) == sorted(GENERATED)
    assert list(final["generated_artifacts"]) == ordered, "recorded in pipeline order, not completion order"
    assert all(final["artifact_review"][name]["verdict"] == "accept" for name in GENERATED)
    assert final["pipeline"]["stages"][-1]["name"] == "uat"
    assert sum(1 for event in final["jev_decisions"] if event["kind"] == "artifact_review") == 3
    assert all(result["status"] == "pass" for result in final["stage_results"])


def test_concurrency_one_keeps_the_serial_order_and_result(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_ASTRA_CONCURRENCY", "1")
    astra = SlowAstra()
    final = _run(tmp_path / "serial", astra)
    starts = [moment for _, moment, _ in astra.started]
    ordered = _generated_in_pipeline_order(final)
    assert [name for name, _, _ in astra.started] == ordered
    assert starts[-1] - starts[0] >= 2 * SLOW * 0.9
    assert list(final["generated_artifacts"]) == ordered


class LateQuotaAstra(SlowAstra):
    """One stage runs out of credits while the other two are still being written."""

    def generate_artifacts(self, payload: dict):
        name = str(payload["stage"]["name"])
        if name == "sbom":
            time.sleep(0.1)
            raise llm.AstraQuotaError("gpt-test: insufficient_quota")
        time.sleep(1.5)
        return FakeAstra.generate_artifacts(self, payload)


def test_a_quota_error_on_one_worker_stops_the_node_without_waiting_for_the_others(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_ASTRA_CONCURRENCY", "4")
    graph = build_graph(MemorySaver(), jev=FakeJev(), astra=LateQuotaAstra(), commands=_echo())
    started = time.monotonic()
    with pytest.raises(llm.AstraQuotaError):
        drive(graph, initial_state(str(tmp_path)), _resume_for_test, echo=False)
    assert time.monotonic() - started < 1.4


def test_astra_concurrency_is_clamped(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_ASTRA_CONCURRENCY", raising=False)
    assert llm.astra_concurrency() == 4
    for raw, expected in (("0", 1), ("99", 8), ("3", 3), ("many", 4)):
        monkeypatch.setenv("ORCHESTRATOR_ASTRA_CONCURRENCY", raw)
        assert llm.astra_concurrency() == expected


def test_the_client_sends_a_capped_reasoning_effort(monkeypatch):
    monkeypatch.setattr(config, "get_openai_api_key", lambda: "unit-placeholder-" * 3)
    monkeypatch.delenv("ORCHESTRATOR_ASTRA_REASONING", raising=False)
    assert llm._client("gpt-test").reasoning_effort == "low"
    monkeypatch.setenv("ORCHESTRATOR_ASTRA_REASONING", "HIGH")
    assert llm._client("gpt-test").reasoning_effort == "high"
    assert llm._client("gpt-test", reasoning="").reasoning_effort is None
    monkeypatch.setenv("ORCHESTRATOR_ASTRA_REASONING", "extreme")
    assert llm._client("gpt-test").reasoning_effort is None
    monkeypatch.setenv("ORCHESTRATOR_ASTRA_REASONING", "")
    assert llm._client("gpt-test").reasoning_effort is None


class _RejectsReasoning:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        if len(self.calls) == 1:
            raise OpenAIError("Error code: 400 - Unsupported parameter: 'reasoning.effort' is not supported with this model.")
        return StageBrief(summary="ok", watch_for="nothing")


def test_a_model_that_rejects_reasoning_effort_is_retried_without_it(monkeypatch):
    calls: list = []

    def fake_client(model_name, temperature=None, reasoning=None, timeout=None):
        calls.append(reasoning)
        return _RejectsReasoning(calls)

    monkeypatch.setattr(llm, "_client", fake_client)
    brief = llm.invoke_structured("gpt-test", StageBrief, "system", {"stage": "build"})
    assert brief.summary == "ok"
    assert calls == [None, ""]


def test_a_codebuild_build_past_the_cap_is_stopped_and_fails_the_stage(tmp_path, monkeypatch):
    provider = aws_module.AwsProvider()
    provider.timeout = 0.05
    monkeypatch.setattr(aws_module, "POLL_SECONDS", 0.01)
    calls: list[list[str]] = []

    def fake_aws_json(argv, cwd, timeout=120):
        calls.append(list(argv))
        if argv[:3] == ["aws", "codebuild", "start-build"]:
            return 0, {"build": {"id": "proj:abc"}}, ""
        if argv[:3] == ["aws", "codebuild", "batch-get-builds"]:
            return 0, {"builds": [{"buildStatus": "IN_PROGRESS", "phases": [{"phaseType": "BUILD", "phaseStatus": ""}]}]}, ""
        return 0, {}, ""

    monkeypatch.setattr(provider, "_aws_json", fake_aws_json)
    argv = ["aws", "codebuild", "start-build", "--project-name", "p", "--region", "us-east-1"]
    ran = provider._codebuild({"name": "build"}, argv, str(tmp_path))
    assert ran.status == "fail" and ran.exit_code == 1
    assert any(call[:3] == ["aws", "codebuild", "stop-build"] and call[3:5] == ["--id", "proj:abc"] for call in calls)
    assert "STOPPED by the orchestrator" in ran.evidence
    assert aws_module.AwsProvider.timeout == 1200


def test_codebuild_templates_cache_dependencies_and_cap_the_build(tmp_path):
    environment = {"tier": "dev", "platform": "aws", "region": "us-east-1"}
    base = {"name": "pipeline", "family": "custom", "purpose": "the whole pipeline", "environment": environment}
    project = templates.render_template("codebuild_project", base, environment, runner="aws_codebuild", run_id="r1", placeholders=[])
    assert '"cache": {"type": "S3", "location": "$AWS_S3_BUCKET/orchestrator/cache"}' in project
    assert '"timeoutInMinutes": 20' in project
    stages = [{"name": "build", "family": "build", "purpose": "compile", "environment": environment}]
    skeleton = suite_module.render_skeletons("aws", stages, environment, run_id="r1", placeholders=[])["ci/buildspec-build.yml"]
    assert "cache:\n  paths:\n" in skeleton and "/root/.cache/pip" in skeleton and "frontend/node_modules" in skeleton
    assert "python: 3.12" in skeleton and "nodejs: 22" in skeleton
    verdict = policy.check_artifact(GeneratedArtifact(kind="buildspec", path="ci/buildspec-build.yml", purpose="build", content=skeleton))
    assert verdict.ok, [item.message for item in verdict.findings]
    generated = templates.render_template("buildspec", {"name": "sbom", "family": "scan", "purpose": "sbom", "environment": environment}, environment, runner="aws_codebuild", run_id="r1", placeholders=[])
    assert "cache:\n  paths:\n" in generated
