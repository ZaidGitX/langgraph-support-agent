"""LangGraph supervisor for one local CI/CD run.

Jev (`langchain_typesafe.TypeSafeClassifier`) chooses the clarify edge,
ranks one pipeline, and routes the quality gate. Astra proposes the
candidate DAGs and diagnoses failures. Stage nodes run shell commands.
"""

from __future__ import annotations

import os

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from app import config

from . import scan
from .catalog import commands_for
from .guardrails import apply_guardrails
from .llm import OpenAIAstra, TypeSafeJev
from .runner import run_commands
from .schemas import GateDecision
from .state import (
    MAX_ATTEMPTS,
    MAX_PLAN_ATTEMPTS,
    STAGE_NAMES,
    PipelineState,
    blocked_stages,
    budget_reason,
    failed_stages,
    latest_results,
    pending_stages,
    target_of,
)

TOOL_BINDINGS = {
    "build": "compileall, ruff, pip-audit, secret scan, and CI scanners",
    "test": "pytest",
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
    return "plan_pipeline"


def route_after_plan(state: dict) -> str:
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
        if row is None or row.get("status") not in {"pass", "skipped"}:
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
        if row and row.get("status") in {"pass", "skipped", "fail"}:
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
        stage = by_name[name]
        sends.append(
            Send(
                name,
                {
                    "dispatched_stage": name,
                    "commands": stage["commands"],
                    "attempt_number": state["attempt"][name],
                    "plan_id": state["pipeline"]["id"],
                    "repo": state["repo"],
                    "answers": state.get("answers") or {},
                    "purpose": stage.get("purpose") or "",
                },
            )
        )
    return sends


def normalize_stages(stages: list[dict]) -> tuple[list[dict], bool]:
    by_name: dict[str, dict] = {}
    for stage in stages:
        name = stage.get("name")
        if name not in STAGE_NAMES or name in by_name:
            continue
        by_name[name] = {
            "name": name,
            "depends_on": [dep for dep in stage.get("depends_on") or [] if dep in STAGE_NAMES and dep != name],
            "purpose": stage.get("purpose") or "",
        }
    restored = False
    if "uat" not in by_name:
        restored = True
        anchor = next(reversed(by_name), None)
        by_name["uat"] = {
            "name": "uat",
            "depends_on": [anchor] if anchor else [],
            "purpose": "UAT sign-off. No promotion past this stage.",
        }
    pending = dict(by_name)
    ordered: list[dict] = []
    while pending:
        ready = [name for name, stage in pending.items() if all(dep not in pending for dep in stage["depends_on"])]
        if not ready:
            name = next(iter(pending))
            pending[name]["depends_on"] = [dep for dep in pending[name]["depends_on"] if dep not in pending]
            ready = [name]
        for name in ready:
            stage = pending.pop(name)
            stage["depends_on"] = [dep for dep in stage["depends_on"] if dep in by_name]
            stage["tool"] = TOOL_BINDINGS[name]
            ordered.append(stage)
    return ordered, restored


def pipeline_view(pipeline: dict) -> dict:
    return {
        "id": pipeline.get("id"),
        "summary": pipeline.get("summary"),
        "stages": [
            {
                "name": stage["name"],
                "depends_on": stage.get("depends_on") or [],
                "purpose": stage.get("purpose") or "",
                "tool": stage.get("tool") or "",
                "commands": [item.get("label") for item in stage.get("commands") or []],
            }
            for stage in pipeline.get("stages") or []
        ],
    }


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
    rows = []
    for name, row in latest_results(state).items():
        rows.append(
            {
                "stage": name,
                "status": row.get("status"),
                "exit_code": row.get("exit_code"),
                "attempt": row.get("attempt"),
                "blocked": bool(row.get("blocked")),
                "evidence": (row.get("evidence") or "")[:500],
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
            "uat_restored": bool(pipeline.get("uat_restored")),
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
        "cloudwatch": {
            "called": False,
            "note": (
                "AWS CloudWatch was not called. The signals below are the "
                "stage evidence this run produced."
            ),
            "stage_evidence": evidence_rows(state),
        },
    }


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
        names = ", ".join(stage.get("name", "") for stage in candidate.get("stages") or [])
        lines.append(f"  - {candidate.get('id')}: {names}")
    lines.extend(["", "Chosen pipeline"])
    view = pipeline_view(state.get("pipeline") or {})
    lines.append(f"  id: {view.get('id')}")
    lines.append(f"  summary: {(state.get('pipeline') or {}).get('summary')}")
    for stage in view["stages"]:
        deps = ", ".join(stage["depends_on"]) or "none"
        commands = ", ".join(stage["commands"])
        lines.append(f"  - {stage['name']} after [{deps}] via {stage['tool']} ({commands})")
    lines.extend(["", "Stage evidence"])
    if not state.get("stage_results"):
        lines.append("  none")
    for row in state.get("stage_results") or []:
        lines.append(
            f"  - {row['stage']} attempt {row.get('attempt')} {row.get('status')} exit {row.get('exit_code')}"
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


def build_graph(checkpointer, *, jev=None, astra=None, commands=None):
    router = jev or TypeSafeJev()
    planner = astra or OpenAIAstra()

    def checkout(state: PipelineState) -> dict:
        profile = scan.scan_checkout(state["repo"]["root"])
        return {
            "repo": profile,
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
            },
        }

    def discover_ecosystem(state: PipelineState) -> dict:
        ecosystem, questions = scan.discover(state["repo"]["root"], state.get("answers"))
        payload = {
            "open_questions": questions,
            "languages": ecosystem["languages"],
            "manifests": ecosystem["manifests"],
            "entry_points": ecosystem["entry_points"],
        }
        decision = router.clarify(payload)
        return {
            "ecosystem": ecosystem,
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
        payload = interrupt({"gate": "ecosystem_qa", "questions": questions})
        answers = payload if isinstance(payload, dict) else {"raw": str(payload)}
        return {"answers": answers, "trace": ["clarify_with_user"], "hops": 1}

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
                "stage_menu": list(STAGE_NAMES),
            }
        )
        rank_payload = {
            "candidates": [item.model_dump() for item in proposal.candidates],
            "answers": state.get("answers") or {},
        }
        decision = router.rank(rank_payload)
        ids = [item.id for item in proposal.candidates]
        if decision.chosen_id not in ids:
            gate = GateDecision(
                route="escalate",
                retry=False,
                replan=False,
                retry_stages=[],
                rationale=f"Jev chose {decision.chosen_id!r}, which is not one of {ids}",
            )
            return {
                "candidates": rank_payload["candidates"],
                "gate": gate.model_dump(),
                "jev_decisions": [jev_event(router, "rank", "RankDecision", decision)],
                "trace": ["plan_pipeline"],
                "hops": 1,
                "attempt": {"plan": int(state["attempt"].get("plan", 0)) + 1},
            }
        chosen = next(item for item in proposal.candidates if item.id == decision.chosen_id)
        dropped = {name for name in decision.drop_stages if name != "uat"}
        kept = [stage.model_dump() for stage in chosen.stages if stage.name not in dropped]
        ordered, restored = normalize_stages(kept)
        for stage in ordered:
            stage["commands"] = commands_for(stage["name"], state.get("answers"), commands)
        plan_number = int(state["attempt"].get("plan", 0)) + 1
        pipeline = {
            "id": f"{decision.chosen_id}-{plan_number}",
            "summary": chosen.summary,
            "stages": ordered,
            "jev": decision.model_dump(),
            "uat_restored": restored,
            "router_model": (getattr(router, "last_call", None) or {}).get("model") or "injected",
            "router_client": (getattr(router, "last_call", None) or {}).get("client")
            or type(router).__name__,
            "planner_model": config.astra_model(),
        }
        return {
            "candidates": rank_payload["candidates"],
            "pipeline": pipeline,
            "gate": {"route": "", "retry": False, "replan": False, "retry_stages": [], "rationale": ""},
            "jev_decisions": [jev_event(router, "rank", "RankDecision", decision)],
            "attempt": {"plan": plan_number},
            "trace": ["plan_pipeline"],
            "hops": 1,
        }

    def approve_pipeline(state: PipelineState) -> dict:
        payload = interrupt(
            {
                "gate": "pipeline_approval",
                "pipeline": pipeline_view(state.get("pipeline") or {}),
            }
        )
        approved = isinstance(payload, dict) and payload.get("decision") == "approved"
        update = {
            "approvals": {"pipeline": "approved" if approved else "rejected"},
            "trace": ["approve_pipeline"],
            "hops": 1,
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
            ran = run_commands(state.get("commands") or [], state["repo"]["root"])
            result = {
                "stage": stage_name,
                "status": ran["status"],
                "exit_code": ran["exit_code"],
                "attempt": state.get("attempt_number") or 1,
                "plan_id": state.get("plan_id"),
                "blocked": ran["blocked"],
                "evidence": ran["evidence"],
                "commands": ran["commands"],
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
                signoff = interrupt(
                    {
                        "gate": "uat_signoff",
                        "constraint": "No deploy edge past UAT.",
                        "target": target_of(state.get("answers")),
                    }
                )
                approved = isinstance(signoff, dict) and signoff.get("decision") == "approved"
                result["status"] = "pass" if approved else "fail"
                result["signoff"] = signoff if isinstance(signoff, dict) else {"raw": str(signoff)}
                update["approvals"] = {"uat": "approved" if approved else "rejected"}
            elif stage_name == "uat":
                update["approvals"] = {"uat": "rejected"}
            return update

        return node

    def quality_gate(state: PipelineState) -> dict:
        payload = {
            "attempts": state.get("attempt") or {},
            "pending": pending_stages(state),
            "failures": failed_stages(state),
            "blocked": blocked_stages(state),
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
            update["diagnosis"] = diagnosis.summary
        return update

    def report(state: PipelineState) -> dict:
        status = conclude(state)
        shown = dict(state)
        shown["trace"] = [*(state.get("trace") or []), "report"]
        return {
            "status": status,
            "report": render_report(shown, status),
            "trace": ["report"],
            "hops": 1,
            "env_state": {"past_uat": False},
        }

    builder = StateGraph(PipelineState)
    builder.add_node("checkout", checkout)
    builder.add_node("discover_ecosystem", discover_ecosystem)
    builder.add_node("clarify_with_user", clarify_with_user)
    builder.add_node("plan_pipeline", plan_pipeline)
    builder.add_node("approve_pipeline", approve_pipeline)
    builder.add_node("dispatch_stages", dispatch_stages)
    builder.add_node("quality_gate", quality_gate)
    builder.add_node("report", report)
    for name in STAGE_NAMES:
        builder.add_node(name, make_runner(name))
        builder.add_edge(name, "quality_gate")
    builder.add_edge(START, "checkout")
    builder.add_edge("checkout", "discover_ecosystem")
    builder.add_conditional_edges(
        "discover_ecosystem",
        route_after_discover,
        ["plan_pipeline", "clarify_with_user"],
    )
    builder.add_edge("clarify_with_user", "plan_pipeline")
    builder.add_conditional_edges(
        "plan_pipeline",
        route_after_plan,
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
        [*STAGE_NAMES, "quality_gate", "report"],
    )
    builder.add_conditional_edges(
        "quality_gate",
        route_after_gate,
        ["dispatch_stages", "plan_pipeline", "report"],
    )
    builder.add_edge("report", END)
    return builder.compile(checkpointer=checkpointer)
