from __future__ import annotations

import time

from langgraph.checkpoint.memory import MemorySaver

from orchestrator import episodes
from orchestrator.demo import drive
from orchestrator.graph import build_graph
from orchestrator.state import initial_state
from tests.test_orchestrator import FakeAstra, FakeJev, _echo, _resume_for_test

# Built at runtime so the test file never holds a credential-shaped literal.
FAKE_SECRET = "sk-" + "1234567890" + "abcdefghijklmnop"


def _state(root: str, run_id: str, *, planned: bool = False, failed: bool = False) -> dict:
    state = initial_state(root, run_id=run_id)
    state["pipeline"] = {
        "id": "local-unit-1",
        "mode": "deterministic",
        "stages": [
            {"name": "build", "depends_on": [], "runner": "local", "kind": "fixed"},
            {"name": "test", "depends_on": ["build"], "runner": "local", "kind": "fixed"},
            {"name": "deploy_dev", "depends_on": ["test"], "runner": "aws_codebuild", "kind": "generated"},
            {"name": "uat", "depends_on": ["deploy_dev"], "runner": "local", "kind": "fixed"},
        ],
    }
    state["env_state"]["platform"] = "aws"
    state["env_state"]["target"] = "dev"
    state["attempt"] = {"build": 1, "test": 2}
    rows = [
        {"stage": "build", "plan_id": "local-unit-1", "status": "pass", "attempt": 1, "checks": []},
        {"stage": "test", "plan_id": "local-unit-1", "status": "fail", "attempt": 1,
         "checks": [{"name": "Pytest", "tool": "pytest", "status": "fail", "findings": "1 failed " + FAKE_SECRET}]},
        {"stage": "test", "plan_id": "local-unit-1", "status": "pass" if not failed else "fail", "attempt": 2,
         "checks": [{"name": "Pytest", "tool": "pytest", "status": "fail", "findings": "2 failed"}] if failed else []},
    ]
    if planned:
        rows.append({"stage": "deploy_dev", "plan_id": "local-unit-1", "status": "planned", "attempt": 1,
                     "runner": "aws_codebuild", "plan": {"runner": "aws_codebuild", "mode": "plan", "missing": ["AWS_REGION", "AWS_CODEBUILD_PROJECT"]}, "checks": []})
    state["stage_results"] = rows
    state["jev_decisions"] = [{"kind": "rank", "model": "jev-latest", "choice": {"pipeline": {"choice": "local-unit"}}}]
    state["generated_artifacts"] = {"deploy_dev": [{"path": "ci/deploy_dev.sh", "kind": "shell", "sha256": "ab" * 32, "content": "never stored"}]}
    state["policy"] = [{"stage": "deploy_dev", "rule": "pin.floating_ref", "severity": "warn"}]
    return state


def test_record_then_recall_round_trip(tmp_path):
    root = str(tmp_path)
    db = str(tmp_path / "episodes.sqlite")
    row = episodes.record(_state(root, "run-1", planned=True), "passed", db_path=db)
    assert "error" not in row
    recalled = episodes.recall(root, db_path=db)
    assert len(recalled) == 1
    item = recalled[0]
    assert item["run_id"] == "run-1"
    assert item["status"] == "passed"
    assert item["platform"] == "aws" and item["tier"] == "dev"
    assert item["chosen"] == {"id": "local-unit-1", "mode": "deterministic", "stages": ["build", "test", "deploy_dev", "uat"]}
    assert item["stage_statuses"]["deploy_dev"] == "planned"
    assert item["planned"] == [{"stage": "deploy_dev", "missing": ["AWS_REGION", "AWS_CODEBUILD_PROJECT"], "runner": "aws_codebuild"}]
    assert item["failures"] == []
    assert "deploy_dev was planned only: missing AWS_REGION, AWS_CODEBUILD_PROJECT" in item["lesson"]
    assert "test passed after a retry" in item["lesson"]
    assert item["finished_at"].endswith("Z")
    assert item["policy_errors"] == 0


def test_failures_are_redacted_and_short(tmp_path):
    root = str(tmp_path)
    db = str(tmp_path / "episodes.sqlite")
    episodes.record(_state(root, "run-f", failed=True), "escalated", db_path=db)
    item = episodes.recall(root, db_path=db)[0]
    assert item["failures"] and item["failures"][0]["stage"] == "test"
    assert all(len(f["finding"]) <= episodes.FINDING_CHARS + 20 for f in item["failures"])
    assert FAKE_SECRET not in str(item) and "sk-" not in str(item["failures"])
    assert item["lesson"].startswith("escalated: ")
    assert "test failed" in item["lesson"]


def test_recall_is_newest_first_limited_and_scoped_to_the_repo(tmp_path):
    root = str(tmp_path / "repo")
    other = str(tmp_path / "other")
    db = str(tmp_path / "episodes.sqlite")
    episodes.record(_state(root, "older"), "passed", db_path=db)
    time.sleep(0.01)
    episodes.record(_state(root, "newer"), "passed", db_path=db)
    episodes.record(_state(other, "elsewhere"), "passed", db_path=db)
    assert [item["run_id"] for item in episodes.recall(root, db_path=db)] == ["newer", "older"]
    assert [item["run_id"] for item in episodes.recall(root, limit=1, db_path=db)] == ["newer"]
    assert [item["run_id"] for item in episodes.recall(other, db_path=db)] == ["elsewhere"]


def test_record_is_an_upsert(tmp_path):
    root = str(tmp_path)
    db = str(tmp_path / "episodes.sqlite")
    episodes.record(_state(root, "same"), "escalated", db_path=db)
    episodes.record(_state(root, "same"), "passed", db_path=db)
    recalled = episodes.recall(root, db_path=db)
    assert len(recalled) == 1 and recalled[0]["status"] == "passed"


def test_recall_without_a_database_is_empty(tmp_path):
    assert episodes.recall(str(tmp_path), db_path=str(tmp_path / "missing.sqlite")) == []


def test_default_db_path_prefers_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCHESTRATOR_DB", "/tmp/elsewhere.sqlite")
    assert episodes.default_db_path(str(tmp_path)) == "/tmp/elsewhere.sqlite"
    monkeypatch.delenv("ORCHESTRATOR_DB")
    assert episodes.default_db_path(str(tmp_path)) == str(tmp_path / "var" / "orchestrator.sqlite")


def test_lesson_for_all_green():
    state = initial_state("/tmp/x", run_id="g")
    state["pipeline"] = {"id": "p", "stages": [{"name": "build"}, {"name": "uat"}]}
    assert episodes.lesson_for(state, "passed") == "passed: all 2 stages passed on local/unset"


def test_fake_run_records_an_episode_and_the_next_run_recalls_it(tmp_path):
    root = str(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text(
        "# Served by uvicorn on port 8000; /status is the healthcheck the deploy target probes.\n"
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n@app.get('/status')\ndef status():\n    return {'ok': True}\n",
        encoding="utf-8",
    )
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\nEXPOSE 8000\nCMD [\"uvicorn\", \"app.main:app\"]\n", encoding="utf-8")
    first_graph = build_graph(MemorySaver(), jev=FakeJev(), astra=FakeAstra(), commands=_echo())
    first = drive(first_graph, initial_state(root), _resume_for_test, echo=False)
    assert first["status"] == "passed"
    assert first["retrieval"]["index_id"] and first["retrieval"]["files"] == 2
    assert first["episodes"] == []
    assert first["ecosystem"]["evidence"]
    assert "Retrieval evidence" in first["report"]
    assert "Dockerfile" in first["report"] or "app/main.py" in first["report"]
    assert "Episodes recalled\n  none" in first["report"]
    assert (tmp_path / "var" / "orchestrator.sqlite").is_file()

    recalled = episodes.recall(root)
    assert [item["run_id"] for item in recalled] == [first["run_id"]]
    stages = recalled[0]["chosen"]["stages"]
    assert set(stages) == {"build", "test", "package", "uat"} and stages[-1] == "uat"

    second_graph = build_graph(MemorySaver(), jev=FakeJev(), astra=FakeAstra(), commands=_echo())
    second = drive(second_graph, initial_state(root), _resume_for_test, echo=False)
    assert second["status"] == "passed"
    assert [item["run_id"] for item in second["episodes"]] == [first["run_id"]]
    assert second["ecosystem"]["episodes"] == second["episodes"]
    assert first["run_id"] in second["report"]
    assert "all 4 stages passed" in second["report"]
    assert [item["run_id"] for item in episodes.recall(root)] == [second["run_id"], first["run_id"]]
