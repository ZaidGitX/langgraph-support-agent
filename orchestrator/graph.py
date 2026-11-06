"""LangGraph supervisor for one local CI/CD run.

Jev (`langchain_typesafe.TypeSafeClassifier`) chooses the clarify edge,
ranks one pipeline, and routes the quality gate. Astra writes the
ecosystem questions, proposes the candidate DAGs, briefs each designated
stage, writes next steps for test and scan findings, and diagnoses failures.
Astra does not invent shell commands or findings that were not in the log.
Designated stages run their GitHub Actions workflow, or the same fixed
commands locally.
"""

from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from app import config

from . import credentials, episodes, generator, policy, registry, retrieval, scan, suite
from .catalog import commands_for
from .dispatch import execute_stage, execute_stage_v2
from .findings import apply_astra_advice
from .guardrails import apply_guardrails
from .llm import (
    REASONING_EFFORTS,
    AstraQuotaError,
    OpenAIAstra,
    TypeSafeJev,
    astra_concurrency,
    astra_reasoning,
    astra_worker_models,
    guard_review,
    scrub,
)
from .providers import registry as providers
from .runner import progress
from .schemas import (
    ArtifactReview,
    CandidatePipeline,
    EnvironmentSpec,
    FollowUpDecision,
    GateDecision,
    RunnerDecision,
    StageBrief,
)
from .state import (
    ALLOWED_TARGETS,
    MAX_ARTIFACT_REVISIONS,
    MAX_ATTEMPTS,
    MAX_CLARIFY_ROUNDS,
    MAX_PLAN_ATTEMPTS,
    SATISFIED_STATUSES,
    STAGE_NAMES,
    PipelineState,
    blocked_stages,
    budget_reason,
    failed_stages,
    latest_results,
    pending_stages,
    planned_stages,
    platform_of,
    runner_default_for,
    target_of,
)

TOOL_BINDINGS = {
    "build": "compileall, ruff, pip-audit, and a redacted secret scan",
    "test": "pytest",
    "scanners": "CodeQL, SonarQube Cloud, Semgrep, Trivy, Snyk Open Source, and Gitleaks",
    "dast": "smoke, then OWASP ZAP and StackHawk against the Compose app",
    "package": "tar artifact (S3 not called)",
    "container": "Dockerfile check (image push not called)",
    "release": "release plan (ECR not called)",
    "deploy": "gated plan for local|dev|staging|uat (ECS not called)",
    "uat": "checklist plus human sign-off",
}


def route_after_discover(state: dict) -> str:
    action = (state.get("clarify_decision") or {}).get("action")
    if action == "ask" and state.get("open_questions"):
        return "clarify_with_user"
    return "follow_up"


def route_after_follow_up(state: dict) -> str:
    """Another human round only while questions are open and the round cap allows it."""
    rounds = int(state.get("clarify_rounds") or 0)
    if state.get("open_questions") and rounds < MAX_CLARIFY_ROUNDS:
        return "clarify_with_user"
    return "plan_pipeline"


def route_after_plan(state: dict) -> str:
    if (state.get("gate") or {}).get("route") == "escalate":
        return "report"
    if not (state.get("pipeline") or {}).get("stages"):
        return "report"
    return "generate_artifacts"


def route_after_generate(state: dict) -> str:
    if (state.get("gate") or {}).get("route") == "escalate":
        return "report"
    if not (state.get("pipeline") or {}).get("stages"):
        return "report"
    return "approve_pipeline"


def route_after_approval(state: dict) -> str:
    if (state.get("approvals") or {}).get("pipeline") == "approved":
        return "dispatch_stages"
    if int((state.get("attempt") or {}).get("plan", 0)) < MAX_PLAN_ATTEMPTS:
        return "plan_pipeline"
    return "report"


def route_after_gate(state: dict) -> str:
    gate = state.get("gate") or {}
    route = gate.get("route")
    if route == "escalate":
        return "report"
    if route == "remediate" and gate.get("replan"):
        return "plan_pipeline"
    if route == "remediate":
        return "dispatch_stages"
    if pending_stages(state):
        return "dispatch_stages"
    return "report"


def _deps_met(stage: dict, latest: dict[str, dict]) -> bool:
    for dep in stage.get("depends_on") or []:
        row = latest.get(dep)
        if row is None or row.get("status") not in SATISFIED_STATUSES:
            return False
    return True


def ready_stage_names(state: dict) -> list[str]:
    stages = (state.get("pipeline") or {}).get("stages") or []
    latest = latest_results(state)
    gate = state.get("gate") or {}
    names: list[str] = []
    if gate.get("route") == "remediate":
        wanted = set(gate.get("retry_stages") or [])
        for stage in stages:
            name = stage["name"]
            if name not in wanted:
                continue
            if int((state.get("attempt") or {}).get(name, 0)) >= MAX_ATTEMPTS:
                continue
            if _deps_met(stage, latest):
                names.append(name)
        return names
    for stage in stages:
        name = stage["name"]
        row = latest.get(name)
        if row and row.get("status") in {*SATISFIED_STATUSES, "fail"}:
            continue
        if _deps_met(stage, latest):
            names.append(name)
    return names


def route_dispatch(state: dict):
    if state.get("stop_at") == "report" or not state.get("ready_stages"):
        return "report"
    by_name = {stage["name"]: stage for stage in state["pipeline"]["stages"]}
    sends = []
    for name in state["ready_stages"]:
        if name not in by_name:
            continue
        stage = by_name[name]
        # Fixed stages have their own node; every registry or custom stage runs on the
        # generic generated_stage node with its spec and artifacts in the Send payload.
        target = name if name in STAGE_NAMES else "generated_stage"
        sends.append(
            Send(
                target,
                {
                    "dispatched_stage": name,
                    "spec": {**stage, "run_id": state.get("run_id")},
                    "artifacts": list(
                        (state.get("generated_artifacts") or {}).get(name)
                        or ((state.get("generated_artifacts") or {}).get(suite.SUITE_STAGE) if name in STAGE_NAMES else None)
                        or []
                    ),
                    "readiness": state.get("readiness") or {},
                    "run_id": state.get("run_id"),
                    "commands": stage.get("commands") or [],
                    "attempt_number": state["attempt"][name],
                    "plan_id": state["pipeline"]["id"],
                    "repo": state["repo"],
                    "answers": state.get("answers") or {},
                    "purpose": stage.get("purpose") or "",
                },
            )
        )
    return sends


def _question_id(raw: str, used: set[str]) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", (raw or "").strip().lower()).strip("_")
    if not slug or not slug[0].isalpha():
        slug = f"q_{slug}" if slug else "question"
    base = slug[:40]
    ident = base
    number = 2
    while ident in used:
        ident = f"{base[:36]}_{number}"
        number += 1
    used.add(ident)
    return ident


def menu_by_family() -> dict[str, list[str]]:
    """Registry stage names grouped by family: compact enough for every prompt."""
    grouped: dict[str, list[str]] = {}
    for item in registry.stage_menu():
        grouped.setdefault(item["family"], []).append(item["name"])
    return grouped


def retrieval_context(root: str, index: dict | None) -> dict:
    """Excerpts and evidence from the checkout index for Astra's prompts. Never raises."""
    try:
        retrieved = retrieval.context_for(root, list(retrieval.QUESTION_TOPICS))
    except (OSError, ValueError):
        retrieved = {}
    index = index or {}
    summary = (
        f"Local BM25 index {index.get('index_id') or 'n/a'} over {index.get('files', 0)} files "
        f"and {index.get('chunks', 0)} chunks ({index.get('retriever') or 'bm25'}). "
        "graphify and OpenSearch are deferred and were not queried."
    )
    return {"retrieved": retrieved, "evidence": retrieval.evidence(retrieved), "retrieval": summary}


def questions_from_astra(planner, ecosystem: dict, answers: dict | None) -> list[dict]:
    """Astra writes the prompts. Already-answered ids are not asked again."""
    proposed = planner.write_questions(
        {
            "ecosystem": {
                "languages": ecosystem.get("languages"),
                "entry_points": ecosystem.get("entry_points"),
                "gaps": ecosystem.get("gaps"),
                "build_tools": ecosystem.get("build_tools"),
            },
            "retrieved": retrieval.trim_context(ecosystem.get("retrieved") or {}),
            "episodes": list(ecosystem.get("episodes") or []),
            "stage_menu": menu_by_family(),
            "already_answered": sorted((answers or {}).keys()),
        }
    )
    used: set[str] = set()
    questions: list[dict] = []
    for item in proposed.questions:
        ident = _question_id(item.id, used)
        if str((answers or {}).get(ident, "")).strip():
            continue
        prompt = (item.prompt or "").strip()
        if not prompt:
            continue
        questions.append(
            {
                "id": ident,
                "prompt": prompt,
                "reason": (item.reason or "").strip(),
            }
        )
    return questions


def recalled_environment(answers: dict, questions: list[dict], recalled: list[dict]) -> dict[str, str]:
    """``env_target`` / ``env_platform`` from the newest episode when Astra did not ask them.

    Astra skips a question an earlier run already answered; the plan still needs the answer,
    so it comes from that episode: what the person answered then (``answered``), else what
    ran (``platform`` / ``tier``). An explicit answer always wins, and a value is used only
    when it would pass as a typed answer. Live run 2 (2026-11-03) stalled on exactly this:
    three questions, no platform, and a local plan for an AWS run.
    """
    asked = {str(question.get("id") or "") for question in questions or []}
    wanted = {
        "env_platform": lambda value: value in registry.PLATFORMS,
        "env_target": lambda value: value in ALLOWED_TARGETS,
    }
    found: dict[str, str] = {}
    for ident, valid in wanted.items():
        if ident in asked or str((answers or {}).get(ident) or "").strip():
            continue
        for episode in recalled or []:
            if not isinstance(episode, dict):
                continue
            answered = episode.get("answered") if isinstance(episode.get("answered"), dict) else {}
            ran = episode.get("platform") if ident == "env_platform" else episode.get("tier")
            for candidate in (answered.get(ident), ran):
                value = str(candidate or "").strip().lower()
                if value and valid(value):
                    found[ident] = value
                    break
            if ident in found:
                break
    return found


def _fall_back_to_host(stages: list[dict], platform_runner: str, generated: dict[str, list[dict]]) -> list[str]:
    """Move every stage bound to the platform runner onto the local host; returns their names.

    Without a suite there is no provisioned project and no source upload, so a platform build
    cannot start: a fixed stage runs its fixed commands here, a generated stage runs its own
    script here (the local provider reads the stored script). ``uat`` never moves.
    """
    host = providers.for_runner("local")
    moved: list[str] = []
    for stage in stages:
        if stage.get("name") == "uat" or stage.get("runner") != platform_runner:
            continue
        stage["runner"] = "local"
        stage["tool"] = _stage_tool(stage["name"], "local")
        if stage.get("kind") == "generated":
            stage["commands"] = host.commands_for(stage, generated.get(stage["name"]) or [])
        moved.append(str(stage["name"]))
    return moved


def _stage_tool(name: str, runner: str) -> str:
    if name in TOOL_BINDINGS:
        return TOOL_BINDINGS[name]
    template = registry.template_for(name)
    if template is not None:
        return f"{template.summary} via {runner}"
    return f"custom stage via {runner}"


def _stage_name(raw: object) -> str | None:
    try:
        return registry.validate_stage_name(raw)
    except ValueError:
        return None


def normalize_stages(
    stages: list[dict], *, allow_generated: bool = True
) -> tuple[list[dict], list[str]]:
    """Order approved stages into an acyclic DAG.

    A name is accepted when it is a registry stage or a valid custom slug; any other name is
    ignored. Stages that were not proposed are not added. A dependency on a missing stage, or
    an edge that closes a cycle, is removed. Nothing may depend on `uat`, and `uat` ends the
    list ("uat->moved_last" is recorded when it had to move). With allow_generated=False only
    the nine fixed stages are kept, because the generic node that runs the others is not in
    the graph yet.
    """
    by_name: dict[str, dict] = {}
    removed: list[str] = []
    for stage in stages:
        name = _stage_name(stage.get("name"))
        if name is None or name in by_name:
            continue
        if not allow_generated and not registry.is_fixed(name):
            removed.append(f"{name}->generated_stage_unavailable")
            continue
        deps: list[str] = []
        for raw in stage.get("depends_on") or []:
            dep = _stage_name(raw)
            if dep is None or dep == name or dep == "uat":
                removed.append(f"{name}->{dep or raw}")
                continue
            deps.append(dep)
        runner = str(stage.get("runner") or "local")
        if name == "uat":
            runner = "local"
        elif runner not in registry.RUNNERS:
            removed.append(f"{name}->runner_reset")
            runner = "local"
        try:
            environment = EnvironmentSpec.model_validate(stage.get("environment") or {}).model_dump()
        except ValueError:
            removed.append(f"{name}->environment_reset")
            environment = EnvironmentSpec().model_dump()
        by_name[name] = {
            "name": name,
            "depends_on": deps,
            "purpose": stage.get("purpose") or "",
            "family": registry.family_of(name),
            "kind": "fixed" if registry.is_fixed(name) else "generated",
            "runner": runner,
            "provider": str(stage.get("provider") or ""),
            "environment": environment,
            "artifacts": [dict(item) for item in stage.get("artifacts") or [] if isinstance(item, dict)],
            "stochastic": bool(stage.get("stochastic")),
        }
    for stage in by_name.values():
        kept: list[str] = []
        for dep in stage["depends_on"]:
            if dep not in by_name:
                removed.append(f"{stage['name']}->{dep}")
                continue
            kept.append(dep)
        stage["depends_on"] = kept
    pending = dict(by_name)
    ordered: list[dict] = []
    while pending:
        ready = sorted(
            name
            for name, stage in pending.items()
            if all(dep not in pending for dep in stage["depends_on"])
        )
        if not ready:
            name = min(pending)
            dep = next(item for item in pending[name]["depends_on"] if item in pending)
            pending[name]["depends_on"] = [
                item for item in pending[name]["depends_on"] if item != dep
            ]
            removed.append(f"{name}->{dep}")
            continue
        for name in ready:
            stage = pending.pop(name)
            stage["tool"] = _stage_tool(name, stage["runner"])
            ordered.append(stage)
    if ordered and any(stage["name"] == "uat" for stage in ordered) and ordered[-1]["name"] != "uat":
        position = next(index for index, stage in enumerate(ordered) if stage["name"] == "uat")
        ordered.append(ordered.pop(position))
        removed.append("uat->moved_last")
    return ordered, removed


def pipeline_view(pipeline: dict) -> dict:
    return {
        "id": pipeline.get("id"),
        "summary": pipeline.get("summary"),
        "mode": pipeline.get("mode") or "deterministic",
        "environment": dict(pipeline.get("environment") or {}),
        "suite": dict(pipeline.get("suite") or {}),
        "stages": [
            {
                "name": stage["name"],
                "depends_on": stage.get("depends_on") or [],
                "purpose": stage.get("purpose") or "",
                "tool": stage.get("tool") or "",
                "commands": [item.get("label") for item in stage.get("commands") or []],
                "runner": stage.get("runner") or "local",
                "provider": stage.get("provider") or "",
                "environment": dict(stage.get("environment") or {}),
                "worker": dict(stage.get("worker") or {}),
                "kind": stage.get("kind") or "fixed",
                "family": stage.get("family") or registry.family_of(stage["name"]),
            }
            for stage in pipeline.get("stages") or []
        ],
    }


def candidate_views(proposal_candidates: list[dict], rank_choice: dict | None, chosen_id: str) -> list[dict]:
    """Candidates as the UI shows them: mode, environment, stage names, Jev probability."""
    probabilities = {}
    pipeline_choice = (rank_choice or {}).get("pipeline") if isinstance(rank_choice, dict) else None
    if isinstance(pipeline_choice, dict):
        probabilities = dict(pipeline_choice.get("probabilities") or {})
    views = []
    for item in proposal_candidates:
        ident = str(item.get("id") or "")
        views.append(
            {
                "id": ident,
                "summary": item.get("summary") or "",
                "mode": item.get("mode") or "deterministic",
                "rationale": item.get("rationale") or "",
                "environment": dict(item.get("environment") or {}),
                "stages": [str(stage.get("name") or "") for stage in item.get("stages") or []],
                "runners": {str(stage.get("name") or ""): stage.get("runner") or "local" for stage in item.get("stages") or []},
                "probability": probabilities.get(ident),
                "chosen": ident == chosen_id,
                "readiness_note": item.get("readiness_note") or "",
            }
        )
    return views


def readiness_note_for(candidate: dict, readiness: dict | None) -> str:
    """'runs now' or 'plan only: missing NAME, …' for a candidate's runners (names only)."""
    runners = (readiness or {}).get("runners") or {}
    missing: list[str] = []
    plan_only = False
    for stage in candidate.get("stages") or []:
        info = runners.get(str(stage.get("runner") or "local")) or {}
        if info and info.get("mode") == "plan":
            plan_only = True
            for name in info.get("missing") or []:
                if name not in missing:
                    missing.append(name)
    if not plan_only:
        return "runs now"
    return "plan only: missing " + ", ".join(missing) if missing else "plan only: cloud apply is off"


def jev_event(router, kind: str, schema: str, decision, applied=None) -> dict:
    meta = getattr(router, "last_call", None) or {}
    event = {
        "kind": kind,
        "model": meta.get("model") or "injected",
        "client": meta.get("client") or type(router).__name__,
        "endpoint": meta.get("endpoint") or "",
        "schema": schema,
        "primitives": list(meta.get("primitives") or []),
        "noul": meta.get("noul") or {},
        "choice": meta.get("choice") or {},
        "request_id": meta.get("request_id"),
        "decision": decision.model_dump(),
    }
    if applied is not None:
        event["applied"] = applied.model_dump()
    return event


def evidence_rows(state: dict) -> list[dict]:
    specs = {stage.get("name"): stage for stage in (state.get("pipeline") or {}).get("stages") or []}
    rows = []
    for name, row in latest_results(state).items():
        spec = specs.get(name) or {}
        rows.append(
            {
                "stage": name,
                "status": row.get("status"),
                "exit_code": row.get("exit_code"),
                "attempt": row.get("attempt"),
                "blocked": bool(row.get("blocked")),
                "evidence": (row.get("evidence") or "")[:500],
                "brief": row.get("brief") or "",
                "watch_for": row.get("watch_for") or "",
                "checks": list(row.get("checks") or []),
                "runner": row.get("runner") or spec.get("runner") or "local",
                "provider": row.get("provider") or spec.get("provider") or "",
                "kind": row.get("kind") or spec.get("kind") or ("fixed" if registry.is_fixed(name) else "generated"),
                "family": row.get("family") or spec.get("family") or registry.family_of(name),
                "plan": dict(row.get("plan") or {}),
            }
        )
    return rows


def router_line(state: dict) -> str:
    decisions = state.get("jev_decisions") or []
    if not decisions:
        return "langchain_typesafe.TypeSafeClassifier (no decision recorded yet)"
    parts = []
    for item in decisions:
        primitives = ", ".join(item.get("primitives") or []) or "none"
        parts.append(
            f"{item.get('kind')} via {item.get('client')} "
            f"model={item.get('model')} schema={item.get('schema')} "
            f"primitives={primitives} choice={item.get('choice')} noul={item.get('noul')}"
        )
    return " | ".join(parts)


def machine_report(state: dict) -> dict:
    """JSON the dashboard and the GitHub artifact both read."""
    pipeline = state.get("pipeline") or {}
    decisions = state.get("jev_decisions") or []
    tracing = bool(os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY"))
    return {
        "run_id": state.get("run_id"),
        "status": state.get("status"),
        "trace": list(state.get("trace") or []),
        "jev_decisions": decisions,
        "candidates": state.get("candidates") or [],
        "chosen_pipeline": {
            **pipeline_view(pipeline),
            "summary": pipeline.get("summary"),
            "uat_restored": False,
            "router_model": pipeline.get("router_model"),
            "router_client": pipeline.get("router_client"),
            "planner_model": pipeline.get("planner_model"),
        },
        "stage_evidence": evidence_rows(state),
        "stage_results": state.get("stage_results") or [],
        "quality_gate": state.get("gate") or {},
        "approvals": state.get("approvals") or {},
        "past_uat": False,
        "aws_called": False,
        "diagnosis": state.get("diagnosis") or "",
        "report_markdown": state.get("report") or "",
        "langsmith": {
            "tracing_configured": tracing,
            "note": (
                "TypeSafe classifications are LangSmith-traced when a LangSmith "
                "key is configured. This panel shows the graph trace and the "
                "TypeSafe request ids this run actually produced."
            ),
            "trace": list(state.get("trace") or []),
            "request_ids": [item.get("request_id") for item in decisions if item.get("request_id")],
        },
        "approved_stages": [stage.get("name") for stage in (pipeline.get("stages") or []) if stage.get("name")],
        "retrieval": {
            **dict(state.get("retrieval") or {}),
            "summary": (state.get("ecosystem") or {}).get("retrieval") or "",
            "evidence": list((state.get("ecosystem") or {}).get("evidence") or []),
        },
        "episodes": list(state.get("episodes") or []),
        "readiness": dict(state.get("readiness") or {}),
        "runner_plan": dict(state.get("runner_plan") or {}),
        "worker_plan": dict(state.get("worker_plan") or {}),
        "generated_artifacts": {
            stage: [{key: value for key, value in record.items() if key != "findings"} for record in records]
            for stage, records in (state.get("generated_artifacts") or {}).items()
        },
        "artifact_review": dict(state.get("artifact_review") or {}),
        "policy": list(state.get("policy") or []),
        "planned_stages": planned_stages(state),
        "provision": dict(state.get("provision") or {}),
        "injection": dict(state.get("injection") or {}),
        "suite": dict(pipeline.get("suite") or {}),
        "follow_up": dict(state.get("follow_up_decision") or {}),
        "clarify_rounds": int(state.get("clarify_rounds") or 0),
        "cloudwatch": {
            "called": False,
            "note": (
                "AWS CloudWatch was not called. The signals below are the "
                "stage evidence this run produced."
            ),
            "stage_evidence": evidence_rows(state),
        },
    }


def visible_report(state: dict) -> dict:
    """Report limited to stages that were actually in the approved pipeline."""
    report = machine_report(state)
    approved = (state.get("approvals") or {}).get("pipeline") == "approved"
    names = list(report.get("approved_stages") or [])
    allowed = set(names)

    def keep(rows: list[dict] | None) -> list[dict]:
        if not approved:
            return []
        return [row for row in rows or [] if row.get("stage") in allowed]

    report["stage_evidence"] = keep(report.get("stage_evidence"))
    report["stage_results"] = keep(report.get("stage_results"))
    cloud = dict(report.get("cloudwatch") or {})
    cloud["stage_evidence"] = keep(cloud.get("stage_evidence"))
    report["cloudwatch"] = cloud
    report["chosen_pipeline_ran"] = approved
    if not approved:
        report["approved_stages"] = []
    return report


def conclude(state: dict) -> str:
    if (state.get("approvals") or {}).get("pipeline") == "rejected":
        return "escalated"
    if (state.get("env_state") or {}).get("past_uat"):
        return "escalated"
    if (state.get("gate") or {}).get("route") == "escalate":
        return "escalated"
    if failed_stages(state) or blocked_stages(state) or pending_stages(state):
        return "escalated"
    if (state.get("approvals") or {}).get("uat") != "approved":
        return "escalated"
    return "passed"


def _retrieval_lines(state: dict) -> list[str]:
    rows = (state.get("ecosystem") or {}).get("evidence") or []
    if not rows:
        return ["  none"]
    return [f"  {row.get('path')}:{row.get('lines')} ({row.get('topic')})" for row in rows[:20]]


def _readiness_brief(readiness: dict) -> dict:
    """Names only, small enough for a prompt: per provider, its mode and missing names."""
    return {
        "providers": {
            key: {"mode": status.get("mode"), "missing": list(status.get("missing") or []), "cli": status.get("cli")}
            for key, status in (readiness.get("providers") or {}).items()
        },
        "runners": {
            runner: {"mode": info.get("mode"), "missing": list(info.get("missing") or [])}
            for runner, info in (readiness.get("runners") or {}).items()
        },
        "toggles": dict(readiness.get("toggles") or {}),
    }


def _bound_secret_names(records: list[dict], platform: str = "") -> list[str]:
    """Every secret name the run's platform files bind by reference (``suite.bound_secret_names``):
    never the platform's own identity secrets, never a name only a file of another platform
    declares (live run 9 escalated on AWS_SESSION_TOKEN from an env manifest and a GitHub workflow)."""
    return suite.bound_secret_names(records, platform)


def approval_payload(state: dict) -> dict:
    """What the person sees before approving: the pipeline, every candidate, the runner plan,
    every generated artifact with its policy verdict, and the retrieval evidence."""
    pipeline = state.get("pipeline") or {}
    # Only what the person would approve: the files of the stages still in the pipeline, and
    # the suite when it is in use. A dropped stage's files (rejected, so no longer in the plan)
    # stay in the report but no longer block the gate (live run 5 could not be approved by
    # anyone because two dropped stages and a rejected suite had policy errors).
    in_plan = {str(stage.get("name")) for stage in pipeline.get("stages") or []}
    if str((pipeline.get("suite") or {}).get("status") or "") == "ok":
        in_plan.add(suite.SUITE_STAGE)
    generated = state.get("generated_artifacts") or {}
    artifacts = {stage: generator.records_for_view(records) for stage, records in generated.items() if stage in in_plan}
    reviews = {stage: review for stage, review in (state.get("artifact_review") or {}).items() if stage in in_plan}
    dropped_artifacts = {stage: generator.records_for_view(records) for stage, records in generated.items() if stage not in in_plan}
    return {
        "gate": "pipeline_approval",
        "pipeline": pipeline_view(pipeline),
        "candidates": list(state.get("candidates") or []),
        "runner_plan": dict(state.get("runner_plan") or {}),
        "worker_plan": dict(state.get("worker_plan") or {}),
        "artifacts": artifacts,
        "dropped_artifacts": dropped_artifacts,
        "artifact_review": reviews,
        "policy": list(state.get("policy") or []),
        "readiness": _readiness_brief(state.get("readiness") or {}),
        "retrieval_evidence": list((state.get("ecosystem") or {}).get("evidence") or []),
        "dropped_stages": list(pipeline.get("dropped_stages") or []),
        "provision": dict(state.get("provision") or {}),
        "injection": dict(state.get("injection") or {}),
        "suite": dict(pipeline.get("suite") or {}),
    }


def _artifact_lines(state: dict) -> list[str]:
    records = state.get("generated_artifacts") or {}
    if not records:
        return ["  none"]
    lines = []
    for stage, rows in records.items():
        review = (state.get("artifact_review") or {}).get(stage) or {}
        lines.append(f"  {stage}: review={review.get('verdict', '-')} revisions={review.get('revisions', 0)}")
        for row in rows:
            summary = row.get("policy") or {}
            lines.append(
                f"    {row.get('path')} ({row.get('kind')}, {row.get('bytes', 0)} bytes, sha256 {str(row.get('sha256') or '')[:12]}) "
                f"policy ok={summary.get('ok')} errors={summary.get('errors', 0)} warns={summary.get('warns', 0)}"
            )
    return lines


def _platform_lines(state: dict) -> list[str]:
    """Suite platform, provisioning, and injection (names only) for the text report."""
    info = (state.get("pipeline") or {}).get("suite") or {}
    if not info:
        return ["  fixed stages ran on the local host or GitHub Actions; no platform suite"]
    lines = [f"  suite for {info.get('platform')} via {info.get('runner')} ({info.get('status', 'ok')})"]
    for stage, path in (info.get("stage_files") or {}).items():
        lines.append(f"    {stage}: {path or 'no file'}")
    for key in ("provision", "injection"):
        item = state.get(key) or {}
        if item:
            names = ", ".join(item.get("names") or []) or "-"
            lines.append(f"  {key}: {item.get('status')} ({item.get('mode')}) names={names} missing={', '.join(item.get('missing') or []) or 'none'}")
    return lines


def _policy_lines(state: dict) -> list[str]:
    rows = state.get("policy") or []
    if not rows:
        return ["  no findings"]
    return [f"  {row.get('severity')} {row.get('stage')}:{row.get('path')}:{row.get('line')} {row.get('rule')}" for row in rows[:30]]


def _readiness_lines(state: dict) -> list[str]:
    readiness = state.get("readiness") or {}
    if not readiness:
        return ["  not checked"]
    lines = []
    toggles = readiness.get("toggles") or {}
    lines.append(
        f"  dispatch={toggles.get('ORCHESTRATOR_DISPATCH', 'local')} cloud_apply={toggles.get('cloud_apply', False)} "
        f"embeddings={toggles.get('ORCHESTRATOR_EMBEDDINGS', 'bm25')}"
    )
    for key, status in (readiness.get("providers") or {}).items():
        cli = status.get("cli")
        cli_text = "" if cli is None else (" cli=present" if cli else " cli=absent")
        missing = ", ".join(status.get("missing") or []) or "none"
        lines.append(f"  {key}: mode={status.get('mode')}{cli_text} missing={missing}")
    return lines


def _episode_lines(state: dict) -> list[str]:
    rows = state.get("episodes") or []
    if not rows:
        return ["  none"]
    return [
        f"  {row.get('run_id')} {row.get('status')} {((row.get('chosen') or {}).get('id') or '-')}: {row.get('lesson')}"
        for row in rows
    ]


def render_report(state: dict, status: str) -> str:
    repo = state.get("repo") or {}
    lines = [
        f"Agentic CI/CD run {state.get('run_id')}",
        f"Status: {status}",
        f"Jev (router): {router_line(state)}",
        f"Astra (planner): {config.astra_model()}",
        "Checkpointer: sqlite. Aurora PostgreSQL is the deferred production binding.",
        "AgentCore, OpenSearch, ECR, ECS, and S3 were not called.",
        "Past UAT: false. This graph has no production edge.",
        "",
        "Repo",
        f"  root: {repo.get('root')}",
        f"  languages: {', '.join(repo.get('languages') or [])}",
        f"  build tools: {', '.join(repo.get('build_tools') or [])}",
        f"  dependency signals: {', '.join(repo.get('dependency_signals') or [])}",
        "",
        "Ecosystem",
        f"  retrieval: {(state.get('ecosystem') or {}).get('retrieval')}",
        f"  entry points: {', '.join((state.get('ecosystem') or {}).get('entry_points') or [])}",
        f"  dast: {(state.get('ecosystem') or {}).get('dast')}",
        "",
        "Readiness (names only)",
        *_readiness_lines(state),
        "",
        "Retrieval evidence",
        *_retrieval_lines(state),
        "",
        "Episodes recalled",
        *_episode_lines(state),
        "",
        "Questions",
    ]
    questions = state.get("open_questions") or []
    if not questions:
        lines.append("  none")
    for question in questions:
        lines.append(f"  {question['id']}: {question['prompt']}")
    lines.append("Answers")
    answers = state.get("answers") or {}
    if not answers:
        lines.append("  none")
    for key, value in answers.items():
        lines.append(f"  {key}: {value}")
    lines.extend(["", "Candidates"])
    candidates = state.get("candidates") or []
    if not candidates:
        lines.append("  none")
    for candidate in candidates:
        names = ", ".join(
            stage if isinstance(stage, str) else str(stage.get("name", ""))
            for stage in candidate.get("stages") or []
        )
        label = f"{candidate.get('mode') or 'deterministic'}{', chosen' if candidate.get('chosen') else ''}"
        probability = candidate.get("probability")
        shown = f" p={probability:.2f}" if isinstance(probability, (int, float)) else ""
        lines.append(f"  - {candidate.get('id')} [{label}{shown}]: {names}")
    lines.extend(["", "Chosen pipeline"])
    view = pipeline_view(state.get("pipeline") or {})
    lines.append(f"  id: {view.get('id')}")
    lines.append(f"  summary: {(state.get('pipeline') or {}).get('summary')}")
    lines.append(f"  mode: {view.get('mode')} environment: {view.get('environment')}")
    for stage in view["stages"]:
        deps = ", ".join(stage["depends_on"]) or "none"
        commands = ", ".join(stage["commands"])
        lines.append(
            f"  - {stage['name']} [{stage['kind']}, runner {stage['runner']}"
            + (f", provider {stage['provider']}" if stage.get("provider") else "")
            + f"] after [{deps}] via {stage['tool']} ({commands})"
        )
    lines.extend(["", "Workers (Astra's sub-agents)"])
    workers = state.get("worker_plan") or {}
    if not workers:
        lines.append("  none")
    for name, worker in workers.items():
        effort = worker.get("reasoning_effort") or "model default"
        lines.append(f"  - {name}: {worker.get('model')} at {effort} effort, {worker.get('assigned_by') or 'default'} ({worker.get('rationale') or ''})")
    lines.extend(["", "Generated artifacts", *_artifact_lines(state), "", "Policy findings", *_policy_lines(state)])
    lines.extend(["", "Platform", *_platform_lines(state)])
    lines.extend(["", "Stage evidence"])
    if not state.get("stage_results"):
        lines.append("  none")
    for row in state.get("stage_results") or []:
        lines.append(
            f"  - {row['stage']} attempt {row.get('attempt')} {row.get('status')} exit {row.get('exit_code')}"
            + (f" runner {row.get('runner')}" if row.get("runner") else "")
            + (f" planned: missing {', '.join((row.get('plan') or {}).get('missing') or []) or 'nothing'}" if row.get("status") == "planned" else "")
        )
        for record in row.get("commands") or []:
            evidence = record.get("evidence") or ""
            if '"status": "skipped"' in evidence:
                lines.append(f"    {record.get('label')} skipped (binary not on PATH; CI workflow runs it)")
            else:
                lines.append(f"    {record.get('label')} exit={record.get('exit_code')}")
    gate = state.get("gate") or {}
    lines.extend(
        [
            "",
            "Quality gate",
            f"  route: {gate.get('route')}",
            f"  retry: {gate.get('retry')}",
            f"  rationale: {gate.get('rationale')}",
            "",
            "Approvals",
            f"  pipeline: {(state.get('approvals') or {}).get('pipeline')}",
            f"  deploy: {(state.get('approvals') or {}).get('deploy')}",
            f"  uat: {(state.get('approvals') or {}).get('uat')}",
            "",
            "Trace",
            "  " + " → ".join(state.get("trace") or []),
        ]
    )
    if state.get("diagnosis"):
        lines.extend(["", "Diagnosis", f"  {state['diagnosis']}"])
    return "\n".join(lines) + "\n"


def interrupt_payload(graph, config) -> dict | None:
    snapshot = graph.get_state(config)
    found = []
    for task in snapshot.tasks:
        found.extend(getattr(task, "interrupts", ()) or ())
    if not found:
        return None
    value = found[0].value
    return value if isinstance(value, dict) else {"raw": value}


def build_graph(checkpointer, *, jev=None, astra=None, commands=None, episodes_db=None):
    """Compile the supervisor.

    ``episodes_db`` is the SQLite file that holds the ``episodes`` table (the session passes
    its checkpointer path). When it is None, ``episodes.default_db_path`` resolves
    ``ORCHESTRATOR_DB`` or ``<repo>/var/orchestrator.sqlite`` at record and recall time.
    """
    router = jev or TypeSafeJev()
    planner = astra or OpenAIAstra()

    def checkout(state: PipelineState) -> dict:
        root = state["repo"]["root"]
        profile = scan.scan_checkout(root)
        try:
            index = retrieval.build_index(root)
        except (OSError, ValueError) as exc:
            index = {
                "index_id": "",
                "files": 0,
                "chunks": 0,
                "retriever": "unavailable",
                "error": scrub(f"{type(exc).__name__}: {exc}"),
            }
        readiness = credentials.readiness()
        return {
            "repo": profile,
            "retrieval": index,
            "readiness": readiness,
            "trace": ["checkout"],
            "hops": 1,
            "env_state": {
                "runtime": "in-process",
                "checkpointer": "sqlite",
                "aurora": "deferred",
                "aws_called": False,
                "past_uat": False,
                "agentcore": "not called",
                "opensearch": "not called",
                "ecr": "not called",
                "ecs": "not called",
                "s3": "not called",
                "cloud_apply": bool((readiness.get("toggles") or {}).get("cloud_apply")),
            },
        }

    def discover_ecosystem(state: PipelineState) -> dict:
        root = state["repo"]["root"]
        ecosystem = scan.discover(root)
        ecosystem.update(retrieval_context(root, state.get("retrieval") or {}))
        ecosystem["episodes"] = episodes.recall(root, limit=3, db_path=episodes_db)
        questions = questions_from_astra(planner, ecosystem, state.get("answers") or {})
        payload = {
            "open_questions": questions,
            "languages": ecosystem["languages"],
            "manifests": ecosystem["manifests"],
            "entry_points": ecosystem["entry_points"],
        }
        decision = router.clarify(payload)
        return {
            "ecosystem": ecosystem,
            "episodes": list(ecosystem.get("episodes") or []),
            "open_questions": questions,
            "clarify_decision": decision.model_dump(),
            "jev_decisions": [jev_event(router, "clarify", "ClarifyDecision", decision)],
            "trace": ["discover_ecosystem"],
            "hops": 1,
        }

    def clarify_with_user(state: PipelineState) -> dict:
        questions = state.get("open_questions") or []
        if not questions:
            return {"trace": ["clarify_with_user"], "hops": 1}
        rounds = int(state.get("clarify_rounds") or 0)
        gate = "ecosystem_qa" if rounds == 0 else "follow_up_qa"
        paused = time.time()
        payload = interrupt({"gate": gate, "questions": questions, "round": rounds + 1})
        waited = max(0.0, time.time() - paused)
        answers = payload if isinstance(payload, dict) else {"raw": str(payload)}
        update = {
            "answers": answers,
            "open_questions": [],
            "clarify_rounds": 1,
            "trace": ["clarify_with_user"],
            "hops": 1,
            "held_seconds": waited,
        }
        if rounds == 0:
            recalled = recalled_environment(answers, questions, state.get("episodes") or [])
            update["answers"] = {**recalled, **answers}
            update["recalled_answers"] = recalled
        return update

    def follow_up(state: PipelineState) -> dict:
        """After the first answers, Jev decides whether one more round would change the plan."""
        rounds = int(state.get("clarify_rounds") or 0)
        ecosystem = state.get("ecosystem") or {}
        payload = {
            "answers": state.get("answers") or {},
            "retrieved": retrieval.trim_context(ecosystem.get("retrieved") or {}, max_chars=6000),
            "readiness": _readiness_brief(state.get("readiness") or {}),
            "rounds": rounds,
            "max_rounds": MAX_CLARIFY_ROUNDS,
        }
        if hasattr(router, "follow_up"):
            decision = router.follow_up(payload)
            events = [jev_event(router, "follow_up", "FollowUpDecision", decision)]
        else:
            decision = FollowUpDecision(action="proceed", rationale="router has no follow_up edge")
            events = []
        if decision.action == "ask_more" and rounds >= MAX_CLARIFY_ROUNDS:
            decision = FollowUpDecision(action="proceed", rationale=decision.rationale + " | guardrail: clarify rounds exhausted")
        questions: list[dict] = []
        if decision.action == "ask_more" and rounds > 0 and hasattr(planner, "follow_up"):
            proposed = planner.follow_up(payload)
            used: set[str] = set()
            answers = state.get("answers") or {}
            for item in proposed.questions:
                ident = _question_id(item.id, used)
                prompt = (item.prompt or "").strip()
                if not prompt or str(answers.get(ident, "")).strip():
                    continue
                questions.append({"id": ident, "prompt": prompt, "reason": (item.reason or "").strip()})
        return {
            "open_questions": questions,
            "follow_up_decision": decision.model_dump(),
            "jev_decisions": events,
            "trace": ["follow_up"],
            "hops": 1,
        }

    def plan_pipeline(state: PipelineState) -> dict:
        if budget_reason(state):
            decision = GateDecision(
                route="escalate",
                retry=False,
                replan=False,
                retry_stages=[],
                rationale=f"guardrail: {budget_reason(state)}",
            )
            return {
                "gate": decision.model_dump(),
                "trace": ["plan_pipeline"],
                "hops": 1,
            }
        readiness = state.get("readiness") or {}
        platform = platform_of(state.get("answers"))
        proposal = planner.propose(
            {
                "repo": {
                    "languages": state["repo"].get("languages"),
                    "build_tools": state["repo"].get("build_tools"),
                    "dependency_signals": state["repo"].get("dependency_signals"),
                },
                "entry_points": (state.get("ecosystem") or {}).get("entry_points"),
                "answers": state.get("answers") or {},
                "diagnosis": state.get("diagnosis") or "",
                "retrieved": retrieval.trim_context((state.get("ecosystem") or {}).get("retrieved") or {}),
                "episodes": list((state.get("ecosystem") or {}).get("episodes") or []),
                "platform": platform,
                "runners_available": dict(readiness.get("runners") or {}),
                "stage_menu": registry.stage_menu(),
                "fixed_stages": list(STAGE_NAMES),
            }
        )
        # Live proposals must carry uat; the Sprint 2 doubles may not, so the schema is lenient
        # and the graph re-validates here, dropping a candidate that breaks the uat rules.
        candidates: list[dict] = []
        dropped_candidates: list[str] = []
        for item in proposal.candidates:
            dumped = item.model_dump()
            try:
                CandidatePipeline.model_validate(dumped, context={"require_uat": True})
            except ValueError as exc:
                if any(stage.name == "uat" for stage in item.stages):
                    dropped_candidates.append(f"{item.id}: {scrub(str(exc))[:160]}")
                    continue
            dumped["readiness_note"] = readiness_note_for(dumped, readiness)
            candidates.append(dumped)
        if not candidates:
            gate = GateDecision(route="escalate", retry=False, replan=False, retry_stages=[],
                                rationale=f"guardrail: every candidate broke the uat rules ({'; '.join(dropped_candidates)})")
            return {"candidates": [], "gate": gate.model_dump(), "trace": ["plan_pipeline"], "hops": 1,
                    "attempt": {"plan": int(state["attempt"].get("plan", 0)) + 1}}
        rank_payload = {"candidates": candidates, "answers": state.get("answers") or {}}
        decision = router.rank(rank_payload)
        rank_event = jev_event(router, "rank", "RankDecision", decision)
        ids = [item["id"] for item in candidates]
        if decision.chosen_id not in ids:
            gate = GateDecision(
                route="escalate",
                retry=False,
                replan=False,
                retry_stages=[],
                rationale=f"Jev chose {decision.chosen_id!r}, which is not one of {ids}",
            )
            return {
                "candidates": candidate_views(candidates, rank_event.get("choice"), ""),
                "gate": gate.model_dump(),
                "jev_decisions": [rank_event],
                "trace": ["plan_pipeline"],
                "hops": 1,
                "attempt": {"plan": int(state["attempt"].get("plan", 0)) + 1},
            }
        chosen = next(item for item in candidates if item["id"] == decision.chosen_id)
        dropped = set(decision.drop_stages)
        kept = [stage for stage in chosen["stages"] if stage.get("name") not in dropped]
        ordered, removed_edges = normalize_stages(kept)
        events = [rank_event]
        runner_plan = _runner_plan(ordered, readiness, platform, events)
        for stage in ordered:
            stage["runner"] = runner_plan[stage["name"]]["runner"]
            stage["tool"] = _stage_tool(stage["name"], stage["runner"])
            stage["commands"] = commands_for(stage["name"], state.get("answers"), commands) if stage["kind"] == "fixed" else []
        worker_plan = _worker_plan(ordered, state, platform)
        for stage in ordered:
            stage["worker"] = dict(worker_plan.get(stage["name"]) or {})
        plan_number = int(state["attempt"].get("plan", 0)) + 1
        pipeline = {
            "id": f"{decision.chosen_id}-{plan_number}",
            "summary": chosen.get("summary") or "",
            "mode": chosen.get("mode") or "deterministic",
            "environment": dict(chosen.get("environment") or {}),
            "stages": ordered,
            "jev": decision.model_dump(),
            "uat_restored": False,
            "dropped_edges": removed_edges,
            "dropped_stages": [],
            "dropped_candidates": dropped_candidates,
            "runner_plan": runner_plan,
            "worker_plan": worker_plan,
            "router_model": (getattr(router, "last_call", None) or {}).get("model") or "injected",
            "router_client": (getattr(router, "last_call", None) or {}).get("client")
            or type(router).__name__,
            "planner_model": config.astra_model(),
        }
        return {
            "candidates": candidate_views(candidates, rank_event.get("choice"), decision.chosen_id),
            "pipeline": pipeline,
            "runner_plan": runner_plan,
            "worker_plan": worker_plan,
            "gate": {"route": "", "retry": False, "replan": False, "retry_stages": [], "rationale": ""},
            "jev_decisions": events,
            "attempt": {"plan": plan_number},
            "trace": ["plan_pipeline"],
            "hops": 1,
        }

    def _runner_plan(ordered: list[dict], readiness: dict, platform: str, events: list[dict]) -> dict[str, dict]:
        """Fixed stages are forced by code; generated stages are Jev's Choice among ready runners."""
        runners_available = dict(readiness.get("runners") or {})
        platform_runners = registry.runners_for(platform)
        dispatch = os.getenv("ORCHESTRATOR_DISPATCH", "local").strip().lower()
        dispatch_github = dispatch == "github" or (dispatch == "platform" and platform == "github")
        # Sprint 4 phase 9: with ORCHESTRATOR_DISPATCH=platform the fixed stages run on the
        # answered platform through its provider and the generated suite. UAT stays local.
        platform_runner = runner_default_for(platform) if dispatch == "platform" and platform in suite.SUITE_PLATFORMS else ""
        plan: dict[str, dict] = {}
        asks: list[dict] = []
        for stage in ordered:
            name = stage["name"]
            if stage["kind"] == "fixed":
                from .dispatch import STAGE_WORKFLOWS

                if platform_runner and name != "uat":
                    info = runners_available.get(platform_runner) or {}
                    plan[name] = {"runner": platform_runner, "provider": "", "mode": info.get("mode") or "plan"}
                    continue
                runner = "github_actions" if dispatch_github and name in STAGE_WORKFLOWS else "local"
                plan[name] = {"runner": runner, "provider": "", "mode": "apply"}
                continue
            template = registry.template_for(name)
            options: list[str] = []
            for candidate in [stage.get("runner") or "", *platform_runners, *(template.default_runners if template else ()), "local"]:
                if candidate and candidate in registry.RUNNERS and candidate not in options:
                    options.append(candidate)
            asks.append({"name": name, "runner_options": options, "family": stage.get("family")})
        if asks:
            if hasattr(router, "runner"):
                decision = router.runner({"stages": asks, "runners_available": runners_available, "platform": platform, "platform_runners": platform_runners})
                events.append(jev_event(router, "runner", "RunnerDecision", decision))
            else:
                decision = RunnerDecision(runners={ask["name"]: ask["runner_options"][0] for ask in asks}, rationale="router has no runner edge")
            for ask in asks:
                runner = decision.runners.get(ask["name"]) or ask["runner_options"][0]
                if runner not in registry.RUNNERS:
                    runner = runner_default_for(platform)
                info = runners_available.get(runner) or {}
                plan[ask["name"]] = {
                    "runner": runner,
                    "provider": next((s.get("provider") or "" for s in ordered if s["name"] == ask["name"]), ""),
                    "mode": info.get("mode") or ("apply" if runner in providers.LOCAL_RUNNERS else "plan"),
                }
        return plan

    def _worker_plan(ordered: list[dict], state: dict, platform: str) -> dict[str, dict]:
        """Astra, as the orchestrator, assigns every stage its sub-agent: a model from
        ``ORCHESTRATOR_ASTRA_WORKER_MODELS`` (the orchestrator model is always an option) and a
        reasoning effort. Code validates the answer; an unknown model or effort, or a failed
        call, falls back to the orchestrator model at ``ORCHESTRATOR_ASTRA_REASONING``. The
        pseudo-stage ``pipeline`` is the platform suite's worker when a suite is wanted.
        """
        orchestrator_model = config.astra_model()
        options = list(dict.fromkeys([*astra_worker_models(), orchestrator_model]))
        default_effort = astra_reasoning()
        names = [stage["name"] for stage in ordered]
        if suite.wants_suite(state):
            names.append(suite.SUITE_STAGE)
        plan = {
            name: {"model": orchestrator_model, "reasoning_effort": default_effort, "rationale": "orchestrator default", "assigned_by": "default"}
            for name in names
        }
        if not hasattr(planner, "assign_workers"):
            return plan
        stages = [
            {
                "name": stage["name"],
                "kind": stage.get("kind") or "fixed",
                "family": stage.get("family") or "",
                "runner": stage.get("runner") or "local",
                "provider": stage.get("provider") or "",
                "purpose": stage.get("purpose") or "",
            }
            for stage in ordered
        ]
        if suite.SUITE_STAGE in plan:
            stages.append({
                "name": suite.SUITE_STAGE, "kind": "suite", "family": "custom", "runner": runner_default_for(platform), "provider": "",
                "purpose": f"the whole {platform} suite: one file per fixed stage plus the platform's project documents",
            })
        payload = {
            "stages": stages,
            "models_available": options,
            "efforts_available": list(REASONING_EFFORTS),
            "orchestrator_model": orchestrator_model,
            "default_effort": default_effort or "model default",
            "platform": platform,
            "answers": state.get("answers") or {},
        }
        try:
            proposal = planner.assign_workers(payload)
        except AstraQuotaError:
            raise
        except (RuntimeError, ValueError, TypeError, OSError) as exc:
            note = scrub(str(exc))[:160]
            for worker in plan.values():
                worker["rationale"] = f"orchestrator default (assignment failed: {note})"
            return plan
        for item in proposal.workers:
            if item.stage not in plan:
                continue
            plan[item.stage] = {
                "model": item.model if item.model in options else orchestrator_model,
                "reasoning_effort": item.reasoning_effort if item.reasoning_effort in REASONING_EFFORTS else default_effort,
                "rationale": scrub(item.rationale)[:240],
                "assigned_by": "astra",
            }
        return plan

    def _brief_for(worker, payload: dict) -> StageBrief:
        """Astra's brief for a stage, or a plain note when Astra cannot answer. The brief is
        advisory: an approved stage runs on its platform without it (live run 10 reached AWS and
        then died on a 429 "no credits remaining" raised by the first stage's brief)."""
        try:
            return worker.brief(payload)
        except (RuntimeError, ValueError, TypeError, OSError) as exc:  # AstraQuotaError is a RuntimeError
            reason = scrub(str(exc))[:200]
            return StageBrief(
                summary=f"Astra was unavailable for this brief ({reason}); the stage ran as approved.",
                watch_for="The stage's own summary line and exit code.",
            )

    def _worker_for(spec: dict):
        """The sub-agent bound to a stage's assignment; the planner itself when it has no workers."""
        factory = getattr(planner, "worker", None)
        if factory is None:
            return planner
        return factory(dict(spec.get("worker") or {}))

    review_lock = threading.Lock()

    def _worker_label(worker, spec: dict | None = None) -> tuple[str, str]:
        """(model, effort) for the timeline: the worker's own (an AstraWorker, which may have
        lowered its effort after a timeout), else the assignment the orchestrator recorded."""
        assigned = dict(spec or {})
        model = str(getattr(worker, "model", "") or assigned.get("model") or "")
        effort = str(getattr(worker, "reasoning_effort", "") or assigned.get("reasoning_effort") or "")
        return model, effort

    def _generate_stage(state: dict, stage: dict, run_id: str, root: str) -> dict:
        """One generated stage on a worker thread, reported to the timeline as it starts and
        ends (live run 4 showed a 21-minute node with no sign of life)."""
        name = stage["name"]
        worker = _worker_for(stage)
        spec = dict(stage.get("worker") or {})
        model, effort = _worker_label(worker, spec)
        started = time.time()
        progress("worker", name, f"{name}: writing files with {model or 'astra'} at {effort or 'default'} effort", model=model, effort=effort, action="start")
        outcome = _generate_stage_body(state, stage, run_id, root, worker)
        seconds = round(time.time() - started, 1)
        _, final_effort = _worker_label(worker, spec)
        if outcome["dropped"]:
            detail = f"{name}: dropped after {seconds}s ({outcome['dropped']})"
        else:
            verdict = (outcome["review"] or {}).get("verdict") or "none"
            detail = f"{name}: {len(outcome['records'])} file(s) in {seconds}s, review {verdict}"
        timed_out_at = str(getattr(worker, "timed_out_at", "") or "")
        if timed_out_at:
            detail += f"; timed out at {timed_out_at} effort, finished at {final_effort}"
        progress("worker", name, detail, model=model, effort=final_effort, seconds=seconds, action="dropped" if outcome["dropped"] else "done")
        return outcome

    def _generate_stage_body(state: dict, stage: dict, run_id: str, root: str, worker) -> dict:
        """One generated stage: Astra writes its files, the policy checks them, Jev reviews.

        Runs on a worker thread beside the other generated stages and the platform suite
        (live run 2 spent 40 minutes writing six stages one after another). Everything the
        node records comes back in the outcome, so the update is assembled in stage order.
        """
        name = stage["name"]
        outcome: dict = {"name": name, "stage": stage, "records": [], "review": None, "rows": [], "events": [], "dropped": "", "kept": False}
        try:
            provider = providers.for_runner(stage.get("runner") or "local")
        except KeyError as exc:
            outcome["dropped"] = f"{name}: {exc}"
            return outcome
        revisions_left = MAX_ARTIFACT_REVISIONS
        try:
            context = generator.context_for(state, stage, provider)
            artifact_set = generator.with_env_manifest(worker.generate_artifacts(context), stage, context["placeholders"], run_id=context["run_id"])
        except AstraQuotaError:
            raise
        except (RuntimeError, ValueError, TypeError, OSError) as exc:
            outcome["dropped"] = f"{name}: generation failed: {scrub(str(exc))[:200]}"
            return outcome
        result = policy.check_artifact_set(artifact_set, stage)
        review = _review(name, result, revisions_left, [a.path for a in artifact_set.artifacts], outcome["events"])
        if review.verdict == "revise" and revisions_left > 0:
            revisions_left -= 1
            try:
                revised = worker.generate_artifacts(
                    generator.context_for(state, stage, provider, policy_findings=[item.model_dump() for item in result.findings])
                )
                artifact_set = generator.with_env_manifest(revised, stage, context["placeholders"], run_id=context["run_id"])
            except AstraQuotaError:
                raise
            except (RuntimeError, ValueError, TypeError, OSError):
                pass
            result = policy.check_artifact_set(artifact_set, stage)
            review = _review(name, result, revisions_left, [a.path for a in artifact_set.artifacts], outcome["events"])
        records = generator.write(run_id, stage, artifact_set, root)
        outcome["records"] = records
        outcome["rows"] = generator.policy_rows(records, name)
        outcome["review"] = {"verdict": review.verdict, "rationale": scrub(review.rationale), "revisions": MAX_ARTIFACT_REVISIONS - revisions_left, "ok": result.ok}
        if review.verdict == "reject" or not result.ok:
            outcome["dropped"] = f"{name}: artifacts rejected ({review.verdict})"
            return outcome
        stage["commands"] = provider.commands_for(stage, records)
        outcome["kept"] = True
        return outcome

    def _generate_suite(state: dict, platform_runner: str, events: list[dict]) -> tuple[dict | None, str]:
        """The platform suite on its own worker thread: (outcome, fallback reason), reported to
        the timeline as it starts and ends like a stage."""
        spec = dict((state.get("worker_plan") or {}).get(suite.SUITE_STAGE) or {})
        worker = _worker_for({"worker": spec})
        model, effort = _worker_label(worker, spec)
        started = time.time()
        progress("worker", suite.SUITE_STAGE, f"{suite.SUITE_STAGE}: writing the {platform_runner} suite with {model or 'astra'} at {effort or 'default'} effort", model=model, effort=effort, action="start")
        outcome, fallback = _generate_suite_body(state, platform_runner, events, worker)
        seconds = round(time.time() - started, 1)
        _, final_effort = _worker_label(worker, spec)
        if outcome is None:
            detail = f"{suite.SUITE_STAGE}: failed after {seconds}s ({fallback})"
        else:
            detail = f"{suite.SUITE_STAGE}: {len(outcome.get('records') or [])} file(s) in {seconds}s, review {(outcome.get('review') or {}).get('verdict') or 'none'}"
        timed_out_at = str(getattr(worker, "timed_out_at", "") or "")
        if timed_out_at:
            detail += f"; timed out at {timed_out_at} effort, finished at {final_effort}"
        progress("worker", suite.SUITE_STAGE, detail, model=model, effort=final_effort, seconds=seconds, action="dropped" if outcome is None else "done")
        return outcome, fallback

    def _generate_suite_body(state: dict, platform_runner: str, events: list[dict], worker) -> tuple[dict | None, str]:
        try:
            suite_provider = providers.for_runner(platform_runner)
            outcome = suite.generate(
                state, worker, suite_provider,
                lambda result, left, paths: _review(suite.SUITE_STAGE, result, left, paths, events),
                max_revisions=MAX_ARTIFACT_REVISIONS,
            )
        except AstraQuotaError:
            raise
        except (RuntimeError, ValueError, TypeError, OSError, KeyError) as exc:
            return None, f"suite generation failed: {scrub(str(exc))[:200]}"
        return outcome, ""

    def generate_artifacts(state: PipelineState) -> dict:
        """Astra writes the files each generated stage needs; policy and Jev review them.

        The generated stages and the platform suite are written concurrently
        (``ORCHESTRATOR_ASTRA_CONCURRENCY`` workers) and the update is assembled in pipeline
        order, so the result matches a serial run. A quota error on any worker stops the node.
        """
        pipeline = dict(state.get("pipeline") or {})
        stages = [dict(stage) for stage in pipeline.get("stages") or []]
        base = {"trace": ["generate_artifacts"], "hops": 1}
        wants_suite = suite.wants_suite(state) and any(
            stage.get("kind", "fixed") == "fixed" and stage["name"] != "uat" and stage.get("runner") not in ("local", "github_actions")
            for stage in stages
        )
        if not any(stage.get("kind") == "generated" for stage in stages) and not wants_suite:
            return base
        root = state["repo"]["root"]
        run_id = str(state.get("run_id"))
        can_generate = hasattr(planner, "generate_artifacts")
        to_generate = [stage for stage in stages if stage.get("kind") == "generated"] if can_generate else []
        platform = suite.platform_for(state) if suite.wants_suite(state) else ""
        platform_runner = runner_default_for(platform) if platform else ""
        run_suite = wants_suite and hasattr(planner, "generate_suite")
        suite_events: list[dict] = []
        suite_result: tuple[dict | None, str] = (None, "planner has no generate_suite")
        outcomes: dict[str, dict] = {}
        workers = max(1, min(astra_concurrency(), len(to_generate) + (1 if run_suite else 0)))
        progress(
            "generation", "generate_artifacts",
            f"writing {len(to_generate)} generated stage(s)" + (f" and the {platform} suite" if run_suite else "") + f" on {workers} worker thread(s)",
            stages=[stage["name"] for stage in to_generate], suite=run_suite, workers=workers,
        )
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="astra")
        try:
            futures = {pool.submit(_generate_stage, state, stage, run_id, root): stage["name"] for stage in to_generate}
            suite_future = pool.submit(_generate_suite, state, platform_runner, suite_events) if run_suite else None
            if suite_future is not None:
                futures[suite_future] = suite.SUITE_STAGE
            for future in as_completed(futures):
                if future is suite_future:
                    suite_result = future.result()
                else:
                    done = future.result()
                    outcomes[done["name"]] = done
        except Exception:
            # A quota error (or any other failure) stops the node now; queued work is dropped
            # and a call already in flight ends on its own without being waited for.
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown(wait=True)
        generated: dict[str, list[dict]] = {}
        reviews: dict[str, dict] = {}
        rows: list[dict] = []
        events: list[dict] = []
        kept: list[dict] = []
        dropped: list[str] = []
        for stage in stages:
            name = stage["name"]
            if stage.get("kind") != "generated":
                kept.append(stage)
                continue
            if not can_generate:
                dropped.append(f"{name}: planner has no generate_artifacts")
                continue
            done = outcomes[name]
            events.extend(done["events"])
            rows.extend(done["rows"])
            if done["review"] is not None:
                reviews[name] = done["review"]
            if done["records"]:
                generated[name] = done["records"]
            if done["dropped"]:
                dropped.append(done["dropped"])
                continue
            kept.append(done["stage"])
        provision: dict = {}
        injection: dict = {}
        suite_info: dict = {}
        if wants_suite:
            outcome, fallback = suite_result
            if outcome is not None:
                events.extend(suite_events)
                generated[suite.SUITE_STAGE] = outcome["records"]
                reviews[suite.SUITE_STAGE] = outcome["review"]
                rows.extend(outcome["policy_rows"])
                if outcome["ok"]:
                    suite_info = {"platform": platform, "runner": platform_runner, "status": "ok", "stage_files": outcome["stage_files"]}
                    suite_answers = {**(state.get("answers") or {}), "run_id": run_id}
                    try:
                        suite_provider = providers.for_runner(platform_runner)
                        provision = suite_provider.provision(
                            outcome["records"], root, mode="plan", answers=suite_answers, bundle=suite.provision_bundle(generated)
                        ).as_dict()
                        # Every file of the run may bind a secret by name (the suite's buildspecs, a
                        # generated stage's files, the ECS task's Secrets): inject exactly those.
                        names = _bound_secret_names(suite.provision_bundle(generated), platform)
                        injection = suite_provider.inject_secrets(names, mode="plan", answers=suite_answers, cwd=root).as_dict()
                    except (RuntimeError, ValueError, TypeError, OSError, KeyError) as exc:
                        fallback = f"suite generation failed: {scrub(str(exc))[:200]}"
                        suite_info = {}
                else:
                    fallback = f"suite rejected ({outcome['review']['verdict']})"
            if fallback:
                suite_info = {"platform": platform, "runner": platform_runner, "status": fallback, "stage_files": {}}
        if suite.wants_suite(state) and (not suite_info or suite_info.get("status") != "ok"):
            # No usable suite means no provisioned project and no source upload: every stage
            # bound to the platform runner (fixed or generated) runs on the host instead, and
            # the approval screen says so.
            moved = _fall_back_to_host(kept, platform_runner, generated)
            if moved:
                status = str(suite_info.get("status") or "") if suite_info else "no suite was generated for the platform"
                suite_info = {"platform": platform, "runner": platform_runner, "status": f"{status}; on the host instead: {', '.join(moved)}", "stage_files": {}}
        if suite_info:
            pipeline["suite"] = suite_info
        if dropped:
            ordered, removed = normalize_stages(kept)
            by_name = {stage["name"]: stage for stage in kept}
            for stage in ordered:
                stage["commands"] = by_name[stage["name"]].get("commands") or []
                stage["runner"] = by_name[stage["name"]].get("runner") or "local"
                stage["tool"] = _stage_tool(stage["name"], stage["runner"])
            pipeline["stages"] = ordered
            pipeline["dropped_edges"] = [*(pipeline.get("dropped_edges") or []), *removed]
            pipeline["dropped_stages"] = [*(pipeline.get("dropped_stages") or []), *dropped]
        else:
            pipeline["stages"] = kept
        update = {
            **base,
            "pipeline": pipeline,
            "generated_artifacts": generated,
            "artifact_review": reviews,
            "policy": rows,
            "jev_decisions": events,
            "provision": provision,
            "injection": injection,
        }
        runnable = [stage for stage in pipeline["stages"] if stage["name"] != "uat"]
        if not runnable:
            update["gate"] = GateDecision(
                route="escalate", retry=False, replan=False, retry_stages=[],
                rationale="guardrail: no runnable stage after artifact review",
            ).model_dump()
        return update

    def _review(name: str, result, revisions_left: int, paths: list[str], events: list[dict]) -> ArtifactReview:
        payload = {"stage": name, "policy": result.model_dump(), "revisions_left": revisions_left, "artifact_paths": paths}
        if hasattr(router, "artifact_review"):
            with review_lock:  # the router stamps last_call for jev_event: one review at a time
                review = router.artifact_review(payload)
                events.append(jev_event(router, "artifact_review", "ArtifactReview", review))
        else:
            review = ArtifactReview(verdict="accept" if result.ok else "revise", rationale="router has no artifact_review edge")
        return guard_review(review, result.ok, revisions_left)

    def approve_pipeline(state: PipelineState) -> dict:
        paused = time.time()
        payload = interrupt(approval_payload(state))
        waited = max(0.0, time.time() - paused)
        approved = isinstance(payload, dict) and payload.get("decision") == "approved"
        decided_by = str(payload.get("by") or "human") if isinstance(payload, dict) else "human"
        update = {
            "approvals": {"pipeline": "approved" if approved else "rejected", "pipeline_by": decided_by},
            "trace": ["approve_pipeline"],
            "hops": 1,
            "held_seconds": waited,
        }
        if isinstance(payload, dict) and payload.get("feedback"):
            update["answers"] = {"pipeline_feedback": payload["feedback"]}
        return update

    def dispatch_stages(state: PipelineState) -> dict:
        reason = budget_reason(state)
        base = {"trace": ["dispatch_stages"], "hops": 1, "stop_at": ""}
        if reason:
            return {
                **base,
                "ready_stages": [],
                "stop_at": "report",
                "gate": {
                    "route": "escalate",
                    "retry": False,
                    "replan": False,
                    "retry_stages": [],
                    "rationale": f"guardrail: {reason}",
                },
            }
        platform_update = _platform_setup(state)
        if platform_update.get("stop"):
            return {**base, **platform_update["update"], "ready_stages": [], "stop_at": "report"}
        base.update(platform_update.get("update") or {})
        ready = ready_stage_names(state)
        if not ready:
            gate = {
                "route": "escalate",
                "retry": False,
                "replan": False,
                "retry_stages": [],
                "rationale": "guardrail: no runnable stage remains",
            }
            if not pending_stages(state):
                gate = dict(state.get("gate") or gate)
            return {**base, "ready_stages": [], "stop_at": "report", "gate": gate}
        return {
            **base,
            "ready_stages": ready,
            "attempt": {
                name: int((state.get("attempt") or {}).get(name, 0)) + 1
                for name in ready
            },
        }

    def _platform_setup(state: dict) -> dict:
        """Provision the platform objects and inject secret names before the first wave (phase 9).

        Plan mode is idempotent and re-recorded on every wave; an apply runs once and keeps its
        pass or fail. A failed apply stops the run through the quality gate.
        """
        info = (state.get("pipeline") or {}).get("suite") or {}
        if not info or info.get("status") != "ok" or (state.get("approvals") or {}).get("pipeline") != "approved":
            return {}
        if (state.get("provision") or {}).get("status") in ("pass", "fail"):
            return {}
        runner = str(info.get("runner") or "")
        try:
            provider = providers.for_runner(runner)
        except KeyError:
            return {}
        status = providers.status_from_readiness(state.get("readiness"), runner) or provider.detect()
        if status.mode == "apply":
            status = provider.detect()  # confirm live before anything leaves this host
        generated_all = dict(state.get("generated_artifacts") or {})
        records = list(generated_all.get(suite.SUITE_STAGE) or [])
        bundle = suite.provision_bundle(generated_all)
        answers = {**(state.get("answers") or {}), "run_id": str(state.get("run_id"))}
        root = state["repo"]["root"]
        provision = provider.provision(records, root, mode=status.mode, answers=answers, bundle=bundle).as_dict()
        names = _bound_secret_names(bundle, suite.platform_for(state))
        injection = provider.inject_secrets(names, mode=status.mode, answers=answers, cwd=root).as_dict()
        provision["evidence"] = scrub(str(provision.get("evidence") or ""))
        injection["evidence"] = scrub(str(injection.get("evidence") or ""))
        update = {"provision": provision, "injection": injection}
        if provision.get("status") == "fail":
            update["gate"] = {
                "route": "escalate", "retry": False, "replan": False, "retry_stages": [],
                "rationale": "guardrail: platform provisioning failed",
            }
            return {"stop": True, "update": update}
        if injection.get("status") == "fail":
            # Every platform build would fail resolving the missing secret; stop before the first.
            update["gate"] = {
                "route": "escalate", "retry": False, "replan": False, "retry_stages": [],
                "rationale": "guardrail: secret injection failed (" + str(injection.get("evidence") or "")[:160] + ")",
            }
            return {"stop": True, "update": update}
        return {"update": update}

    def make_runner(stage_name: str):
        def node(state: dict) -> dict:
            worker = _worker_for(state.get("spec") or {})
            brief = _brief_for(
                worker,
                {
                    "stage": stage_name,
                    "purpose": state.get("purpose") or "",
                    "answers": state.get("answers") or {},
                },
            )
            spec = dict(state.get("spec") or {})
            runner = str(spec.get("runner") or "local")
            if runner not in ("local", "github_actions"):
                # Sprint 4: a fixed stage on a platform runs its suite file through the provider.
                ran = execute_stage_v2(
                    {**spec, "name": stage_name, "kind": "fixed", "provider": spec.get("provider") or ""},
                    [],
                    state["repo"]["root"],
                    state.get("answers") or {},
                    state.get("readiness") or {},
                    records=list(state.get("artifacts") or []),
                )
            else:
                ran = execute_stage(
                    stage_name,
                    state.get("commands") or [],
                    state["repo"]["root"],
                    state.get("answers") or {},
                )
            checks = apply_astra_advice(list(ran.get("checks") or []), getattr(worker, "advise", None))
            for check in checks:
                check["findings"] = scrub(str(check.get("findings") or ""))
                check["next_step"] = scrub(str(check.get("next_step") or ""))
            result = {
                "stage": stage_name,
                "status": ran["status"],
                "exit_code": ran["exit_code"],
                "attempt": state.get("attempt_number") or 1,
                "plan_id": state.get("plan_id"),
                "blocked": ran["blocked"],
                "evidence": ran["evidence"],
                "commands": ran["commands"],
                "checks": checks,
                "brief": scrub(brief.summary),
                "watch_for": scrub(brief.watch_for),
                "runner": runner,
                "provider": spec.get("provider") or "",
                "kind": "fixed",
                "family": spec.get("family") or registry.family_of(stage_name),
                "plan": dict(ran.get("plan") or {}),
            }
            update: dict = {
                "stage_results": [result],
                "trace": [stage_name],
                "hops": 1,
                "env_state": {"past_uat": False, "aws_called": False},
            }
            if stage_name == "deploy":
                refused = ran["blocked"] or ran["status"] not in ("pass", "planned")
                update["approvals"] = {"deploy": "refused" if refused else "gated-plan"}
                update["env_state"]["target"] = target_of(state.get("answers"))
            if stage_name == "uat" and ran["status"] == "pass":
                paused = time.time()
                signoff = interrupt(
                    {
                        "gate": "uat_signoff",
                        "constraint": "No deploy edge past UAT.",
                        "target": target_of(state.get("answers")),
                    }
                )
                waited = max(0.0, time.time() - paused)
                approved = isinstance(signoff, dict) and signoff.get("decision") == "approved"
                result["status"] = "pass" if approved else "fail"
                result["signoff"] = signoff if isinstance(signoff, dict) else {"raw": str(signoff)}
                update["approvals"] = {"uat": "approved" if approved else "rejected"}
                update["held_seconds"] = waited
            elif stage_name == "uat":
                update["approvals"] = {"uat": "rejected"}
            return update

        return node

    def generated_stage(state: dict) -> dict:
        """One generic node runs every registry or custom stage through its provider."""
        stage_name = str(state.get("dispatched_stage") or "stage")
        spec = dict(state.get("spec") or {"name": stage_name, "runner": "local", "kind": "generated"})
        spec["run_id"] = state.get("run_id") or spec.get("run_id")
        worker = _worker_for(spec)
        brief = _brief_for(
            worker,
            {
                "stage": stage_name,
                "purpose": state.get("purpose") or spec.get("purpose") or "",
                "answers": state.get("answers") or {},
            },
        )
        ran = execute_stage_v2(
            spec,
            state.get("commands") or [],
            state["repo"]["root"],
            state.get("answers") or {},
            state.get("readiness") or {},
            state.get("artifacts") or [],
        )
        checks = apply_astra_advice(list(ran.get("checks") or []), getattr(worker, "advise", None))
        for check in checks:
            check["findings"] = scrub(str(check.get("findings") or ""))
            check["next_step"] = scrub(str(check.get("next_step") or ""))
        result = {
            "stage": stage_name,
            "status": ran["status"],
            "exit_code": ran["exit_code"],
            "attempt": state.get("attempt_number") or 1,
            "plan_id": state.get("plan_id"),
            "blocked": bool(ran.get("blocked")),
            "evidence": ran.get("evidence") or "",
            "commands": ran.get("commands") or [],
            "checks": checks,
            "brief": scrub(brief.summary),
            "watch_for": scrub(brief.watch_for),
            "runner": spec.get("runner") or "local",
            "provider": spec.get("provider") or "",
            "environment": dict(spec.get("environment") or {}),
            "kind": "generated",
            "family": spec.get("family") or registry.family_of(stage_name),
            "plan": dict(ran.get("plan") or {}),
        }
        return {
            "stage_results": [result],
            "trace": [stage_name],
            "hops": 1,
            "env_state": {"past_uat": False, "aws_called": False},
        }

    def quality_gate(state: PipelineState) -> dict:
        payload = {
            "attempts": state.get("attempt") or {},
            "pending": pending_stages(state),
            "failures": failed_stages(state),
            "blocked": blocked_stages(state),
            "planned": planned_stages(state),
            "evidence": evidence_rows(state),
            "max_attempts": MAX_ATTEMPTS,
        }
        if budget_reason(state):
            raw = GateDecision(
                route="escalate",
                retry=False,
                replan=False,
                retry_stages=[],
                rationale=f"guardrail: {budget_reason(state)}",
            )
            called_model = False
        else:
            raw = router.gate(payload)
            called_model = True
        decision = apply_guardrails(state, raw)
        event = jev_event(router, "gate", "GateDecision", raw, applied=decision)
        if not called_model:
            event.update(
                {
                    "model": "guardrail",
                    "client": "orchestrator.guardrails",
                    "endpoint": "",
                    "primitives": [],
                    "noul": {},
                    "choice": {},
                    "request_id": None,
                }
            )
        update = {
            "gate": decision.model_dump(),
            "jev_decisions": [event],
            "trace": ["quality_gate"],
            "hops": 1,
            "stop_at": "",
        }
        if decision.route in {"remediate", "escalate"} and failed_stages(state):
            failed = failed_stages(state)
            spec = next((stage for stage in (state.get("pipeline") or {}).get("stages") or [] if stage.get("name") == failed[0]), {})
            try:
                diagnosis = _worker_for(spec).diagnose(payload)
                update["diagnosis"] = scrub(diagnosis.summary)
            except (RuntimeError, ValueError, TypeError, OSError) as exc:
                # The diagnosis is advisory; the gate's decision stands without it (live run 10).
                update["diagnosis"] = f"Astra was unavailable for the diagnosis ({scrub(str(exc))[:200]}); see the stage evidence."
        return update

    def report(state: PipelineState) -> dict:
        status = conclude(state)
        shown = dict(state)
        shown["trace"] = [*(state.get("trace") or []), "report"]
        _write_deploy_doc(state)
        episodes.record(shown, status, db_path=episodes_db)
        return {
            "status": status,
            "report": render_report(shown, status),
            "trace": ["report"],
            "hops": 1,
            "env_state": {"past_uat": False},
        }

    def _write_deploy_doc(state: dict) -> None:
        """Optional: Astra writes the deployment document for the approved pipeline."""
        if not hasattr(planner, "write_docs") or not state.get("generated_artifacts"):
            return
        pipeline = state.get("pipeline") or {}
        if (state.get("approvals") or {}).get("pipeline") != "approved":
            return
        try:
            doc = planner.write_docs(
                {
                    "pipeline": pipeline_view(pipeline),
                    "environment": pipeline.get("environment") or {},
                    "artifacts": {
                        stage: [{"path": record.get("path"), "purpose": record.get("purpose")} for record in records]
                        for stage, records in (state.get("generated_artifacts") or {}).items()
                    },
                    "readiness": _readiness_brief(state.get("readiness") or {}),
                    "answers": state.get("answers") or {},
                }
            )
            relative = generator.doc_relative_path(pipeline.get("environment"))
            verdict = policy.check_text(scrub(doc.content), kind="doc", path=relative)
            if not verdict.ok or not policy.check_path(relative).ok:
                return
            target = generator.docs_path(state["repo"]["root"], str(state.get("run_id")), pipeline.get("environment"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(scrub(doc.content), encoding="utf-8")
        except (RuntimeError, ValueError, TypeError, OSError):
            # The document is optional; a quota error here must not end the run at report time.
            return

    builder = StateGraph(PipelineState)
    builder.add_node("checkout", checkout)
    builder.add_node("discover_ecosystem", discover_ecosystem)
    builder.add_node("clarify_with_user", clarify_with_user)
    builder.add_node("follow_up", follow_up)
    builder.add_node("plan_pipeline", plan_pipeline)
    builder.add_node("generate_artifacts", generate_artifacts)
    builder.add_node("approve_pipeline", approve_pipeline)
    builder.add_node("dispatch_stages", dispatch_stages)
    builder.add_node("generated_stage", generated_stage)
    builder.add_node("quality_gate", quality_gate)
    builder.add_node("report", report)
    for name in STAGE_NAMES:
        builder.add_node(name, make_runner(name))
        builder.add_edge(name, "quality_gate")
    builder.add_edge("generated_stage", "quality_gate")
    builder.add_edge(START, "checkout")
    builder.add_edge("checkout", "discover_ecosystem")
    builder.add_conditional_edges(
        "discover_ecosystem",
        route_after_discover,
        ["follow_up", "clarify_with_user"],
    )
    builder.add_edge("clarify_with_user", "follow_up")
    builder.add_conditional_edges(
        "follow_up",
        route_after_follow_up,
        ["clarify_with_user", "plan_pipeline"],
    )
    builder.add_conditional_edges(
        "plan_pipeline",
        route_after_plan,
        ["generate_artifacts", "report"],
    )
    builder.add_conditional_edges(
        "generate_artifacts",
        route_after_generate,
        ["approve_pipeline", "report"],
    )
    builder.add_conditional_edges(
        "approve_pipeline",
        route_after_approval,
        ["dispatch_stages", "plan_pipeline", "report"],
    )
    builder.add_conditional_edges(
        "dispatch_stages",
        route_dispatch,
        [*STAGE_NAMES, "generated_stage", "quality_gate", "report"],
    )
    builder.add_conditional_edges(
        "quality_gate",
        route_after_gate,
        ["dispatch_stages", "plan_pipeline", "report"],
    )
    builder.add_edge("report", END)
    return builder.compile(checkpointer=checkpointer)
