"""Shared PipelineState for one CI/CD run.

Aurora PostgreSQL is the production checkpointer in ADR-001. This sprint
keeps that binding as an interface and snapshots state in local Sqlite.
"""

from __future__ import annotations

import operator
import time
import uuid
from typing import Annotated, TypedDict

from . import registry

MAX_ATTEMPTS = 2
MAX_PLAN_ATTEMPTS = 2
# Sprint 3: the first round of ecosystem questions plus one follow-up round.
MAX_CLARIFY_ROUNDS = 2
MAX_ARTIFACT_REVISIONS = 1
# Below the recursion_limit (160 in session.py and demo.py) so the budget guardrail
# escalates before LangGraph raises GraphRecursionError.
MAX_HOPS = 128
# Human pauses are subtracted in budget_reason. What remains is compute
# plus waiting on a designated workflow, still inside the session window.
MAX_SECONDS = 3000

STAGE_NAMES = (
    "build",
    "test",
    "scanners",
    "dast",
    "package",
    "container",
    "release",
    "deploy",
    "uat",
)
ALLOWED_TARGETS = ("local", "dev", "staging", "uat")
FORBIDDEN_TARGETS = ("production", "prod")
STATUSES = registry.STATUSES
# A stage in one of these statuses satisfies the stages that depend on it. `planned` is a
# first-class outcome: the provider could not apply and recorded what it would have run.
SATISFIED_STATUSES = ("pass", "skipped", "planned")


def merge_dict(left: dict | None, right: dict | None) -> dict:
    merged = dict(left or {})
    merged.update(right or {})
    return merged


class Question(TypedDict):
    id: str
    prompt: str
    reason: str


class RepoProfile(TypedDict):
    root: str
    languages: list[str]
    build_tools: list[str]
    dependency_signals: list[str]


class PipelineState(TypedDict):
    run_id: str
    repo: RepoProfile
    ecosystem: dict
    open_questions: list[Question]
    answers: Annotated[dict, merge_dict]
    pipeline: dict
    stage_results: Annotated[list[dict], operator.add]
    env_state: Annotated[dict, merge_dict]
    approvals: Annotated[dict, merge_dict]
    attempt: Annotated[dict, merge_dict]
    trace: Annotated[list[str], operator.add]
    jev_decisions: Annotated[list[dict], operator.add]
    candidates: list[dict]
    clarify_decision: dict
    gate: dict
    report: str
    status: str
    hops: Annotated[int, operator.add]
    started_at: float
    ready_stages: list[str]
    diagnosis: str
    stop_at: str
    held_seconds: Annotated[float, operator.add]
    retrieval: dict
    episodes: list[dict]
    readiness: dict
    runner_plan: Annotated[dict, merge_dict]
    generated_artifacts: Annotated[dict, merge_dict]
    policy: Annotated[list[dict], operator.add]
    clarify_rounds: Annotated[int, operator.add]
    follow_up_decision: dict
    artifact_review: Annotated[dict, merge_dict]


def initial_state(root: str, run_id: str | None = None) -> PipelineState:
    return {
        "run_id": run_id or str(uuid.uuid4()),
        "repo": {
            "root": root,
            "languages": [],
            "build_tools": [],
            "dependency_signals": [],
        },
        "ecosystem": {},
        "open_questions": [],
        "answers": {},
        "pipeline": {},
        "stage_results": [],
        "env_state": {
            "runtime": "in-process",
            "checkpointer": "sqlite",
            "aurora": "deferred",
            "aws_called": False,
            "past_uat": False,
            "target": "unset",
            "platform": "local",
            "cloud_apply": False,
        },
        "approvals": {
            "pipeline": "pending",
            "deploy": "pending",
            "uat": "pending",
        },
        "attempt": {},
        "trace": [],
        "jev_decisions": [],
        "candidates": [],
        "clarify_decision": {},
        "gate": {},
        "report": "",
        "status": "running",
        "hops": 0,
        "started_at": time.time(),
        "ready_stages": [],
        "diagnosis": "",
        "stop_at": "",
        "held_seconds": 0.0,
        "retrieval": {},
        "episodes": [],
        "readiness": {},
        "runner_plan": {},
        "generated_artifacts": {},
        "policy": [],
        "clarify_rounds": 0,
        "follow_up_decision": {},
        "artifact_review": {},
    }


def latest_results(state: dict) -> dict[str, dict]:
    plan_id = (state.get("pipeline") or {}).get("id")
    latest: dict[str, dict] = {}
    for row in state.get("stage_results") or []:
        if plan_id and row.get("plan_id") != plan_id:
            continue
        latest[row["stage"]] = row
    return latest


def failed_stages(state: dict) -> list[str]:
    return [name for name, row in latest_results(state).items() if row.get("status") == "fail"]


def blocked_stages(state: dict) -> list[str]:
    return [name for name, row in latest_results(state).items() if row.get("blocked")]


def pending_stages(state: dict) -> list[str]:
    latest = latest_results(state)
    pending: list[str] = []
    for stage in (state.get("pipeline") or {}).get("stages") or []:
        row = latest.get(stage["name"])
        if row is None or row.get("status") not in SATISFIED_STATUSES:
            pending.append(stage["name"])
    return pending


def planned_stages(state: dict) -> list[str]:
    return [name for name, row in latest_results(state).items() if row.get("status") == "planned"]


def budget_reason(state: dict) -> str | None:
    hops = int(state.get("hops") or 0)
    if hops >= MAX_HOPS:
        return f"hop budget {MAX_HOPS} reached"
    started = float(state.get("started_at") or time.time())
    held = float(state.get("held_seconds") or 0)
    elapsed = max(0.0, time.time() - started - held)
    if elapsed >= MAX_SECONDS:
        return f"time budget {MAX_SECONDS}s reached"
    return None


def target_of(answers: dict | None) -> str:
    raw = str((answers or {}).get("env_target", "local")).strip().lower()
    if raw in FORBIDDEN_TARGETS:
        return raw
    if raw in ALLOWED_TARGETS:
        return raw
    return "local"


def platform_of(answers: dict | None) -> str:
    raw = str((answers or {}).get("env_platform", "local")).strip().lower()
    return raw if raw in registry.PLATFORMS else "local"


def runner_default_for(platform: str) -> str:
    runners = registry.runners_for(platform)
    return runners[0] if runners else "local"
