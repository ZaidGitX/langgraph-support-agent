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
import time

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from app import config

from . import credentials, episodes, generator, policy, registry, retrieval, scan
from .catalog import commands_for
from .dispatch import execute_stage, execute_stage_v2
from .findings import apply_astra_advice
from .guardrails import apply_guardrails
from .llm import OpenAIAstra, TypeSafeJev, guard_review, scrub
from .providers import registry as providers
from .schemas import (
    ArtifactReview,
    CandidatePipeline,
    EnvironmentSpec,
    FollowUpDecision,
    GateDecision,
    RunnerDecision,
)
from .state import (
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
                    "artifacts": list((state.get("generated_artifacts") or {}).get(name) or []),
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
        "generated_artifacts": {
            stage: [{key: value for key, value in record.items() if key != "findings"} for record in records]
            for stage, records in (state.get("generated_artifacts") or {}).items()
        },
        "artifact_review": dict(state.get("artifact_review") or {}),
        "policy": list(state.get("policy") or []),
        "planned_stages": planned_stages(state),
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


def approval_payload(state: dict) -> dict:
    """What the person sees before approving: the pipeline, every candidate, the runner plan,
    every generated artifact with its policy verdict, and the retrieval evidence."""
    pipeline = state.get("pipeline") or {}
    artifacts = {
        stage: generator.records_for_view(records)
        for stage, records in (state.get("generated_artifacts") or {}).items()
    }
    return {
        "gate": "pipeline_approval",
        "pipeline": pipeline_view(pipeline),
        "candidates": list(state.get("candidates") or []),
        "runner_plan": dict(state.get("runner_plan") or {}),
        "artifacts": artifacts,
        "artifact_review": dict(state.get("artifact_review") or {}),
        "policy": list(state.get("policy") or []),
        "readiness": _readiness_brief(state.get("readiness") or {}),
        "retrieval_evidence": list((state.get("ecosystem") or {}).get("evidence") or []),
        "dropped_stages": list(pipeline.get("dropped_stages") or []),
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
    lines.extend(["", "Generated artifacts", *_artifact_lines(state), "", "Policy findings", *_policy_lines(state)])
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
        return {
            "answers": answers,
            "open_questions": [],
            "clarify_rounds": 1,
            "trace": ["clarify_with_user"],
            "hops": 1,
            "held_seconds": waited,
        }

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
            "router_model": (getattr(router, "last_call", None) or {}).get("model") or "injected",
            "router_client": (getattr(router, "last_call", None) or {}).get("client")
            or type(router).__name__,
            "planner_model": config.astra_model(),
        }
        return {
            "candidates": candidate_views(candidates, rank_event.get("choice"), decision.chosen_id),
            "pipeline": pipeline,
            "runner_plan": runner_plan,
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
        dispatch_github = os.getenv("ORCHESTRATOR_DISPATCH", "local").strip().lower() == "github"
        plan: dict[str, dict] = {}
        asks: list[dict] = []
        for stage in ordered:
            name = stage["name"]
            if stage["kind"] == "fixed":
                from .dispatch import STAGE_WORKFLOWS

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

    def generate_artifacts(state: PipelineState) -> dict:
        """Astra writes the files each generated stage needs; policy and Jev review them."""
        pipeline = dict(state.get("pipeline") or {})
        stages = [dict(stage) for stage in pipeline.get("stages") or []]
        base = {"trace": ["generate_artifacts"], "hops": 1}
        if not any(stage.get("kind") == "generated" for stage in stages):
            return base
        root = state["repo"]["root"]
        run_id = str(state.get("run_id"))
        can_generate = hasattr(planner, "generate_artifacts")
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
            try:
                provider = providers.for_runner(stage.get("runner") or "local")
            except KeyError as exc:
                dropped.append(f"{name}: {exc}")
                continue
            revisions_left = MAX_ARTIFACT_REVISIONS
            try:
                context = generator.context_for(state, stage, provider)
                artifact_set = generator.with_env_manifest(planner.generate_artifacts(context), stage, context["placeholders"])
            except (RuntimeError, ValueError, TypeError, OSError) as exc:
                dropped.append(f"{name}: generation failed: {scrub(str(exc))[:200]}")
                continue
            result = policy.check_artifact_set(artifact_set, stage)
            review = _review(name, result, revisions_left, [a.path for a in artifact_set.artifacts], events)
            if review.verdict == "revise" and revisions_left > 0:
                revisions_left -= 1
                try:
                    revised = planner.generate_artifacts(
                        generator.context_for(state, stage, provider, policy_findings=[item.model_dump() for item in result.findings])
                    )
                    artifact_set = generator.with_env_manifest(revised, stage, context["placeholders"])
                except (RuntimeError, ValueError, TypeError, OSError):
                    pass
                result = policy.check_artifact_set(artifact_set, stage)
                review = _review(name, result, revisions_left, [a.path for a in artifact_set.artifacts], events)
            records = generator.write(run_id, stage, artifact_set, root)
            rows.extend(generator.policy_rows(records, name))
            reviews[name] = {"verdict": review.verdict, "rationale": scrub(review.rationale), "revisions": MAX_ARTIFACT_REVISIONS - revisions_left, "ok": result.ok}
            if review.verdict == "reject" or not result.ok:
                dropped.append(f"{name}: artifacts rejected ({review.verdict})")
                generated[name] = records
                continue
            stage["commands"] = provider.commands_for(stage, records)
            generated[name] = records
            kept.append(stage)
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
        update = {
            "approvals": {"pipeline": "approved" if approved else "rejected"},
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

    def make_runner(stage_name: str):
        def node(state: dict) -> dict:
            brief = planner.brief(
                {
                    "stage": stage_name,
                    "purpose": state.get("purpose") or "",
                    "answers": state.get("answers") or {},
                }
            )
            ran = execute_stage(
                stage_name,
                state.get("commands") or [],
                state["repo"]["root"],
                state.get("answers") or {},
            )
            checks = apply_astra_advice(list(ran.get("checks") or []), getattr(planner, "advise", None))
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
            }
            update: dict = {
                "stage_results": [result],
                "trace": [stage_name],
                "hops": 1,
                "env_state": {"past_uat": False, "aws_called": False},
            }
            if stage_name == "deploy":
                refused = ran["blocked"] or ran["status"] != "pass"
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
        brief = planner.brief(
            {
                "stage": stage_name,
                "purpose": state.get("purpose") or spec.get("purpose") or "",
                "answers": state.get("answers") or {},
            }
        )
        ran = execute_stage_v2(
            spec,
            state.get("commands") or [],
            state["repo"]["root"],
            state.get("answers") or {},
            state.get("readiness") or {},
            state.get("artifacts") or [],
        )
        checks = apply_astra_advice(list(ran.get("checks") or []), getattr(planner, "advise", None))
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
            diagnosis = planner.diagnose(payload)
            update["diagnosis"] = scrub(diagnosis.summary)
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
            target = generator.docs_path(state["repo"]["root"], str(state.get("run_id")), pipeline.get("environment"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(scrub(doc.content), encoding="utf-8")
        except (RuntimeError, ValueError, TypeError, OSError):
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
