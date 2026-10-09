"""Presenter entry point for one scripted local CI/CD run.

    python -m orchestrator.demo
"""

from __future__ import annotations

import json

from langgraph.types import Command

from app import config

from .checkpoints import open_checkpointer
from .graph import build_graph, interrupt_payload, machine_report
from .llm import scrub
from .runner import reset_printer, set_printer
from .schemas import ClarifyDecision, GateDecision, PlanProposal, RankDecision
from .state import initial_state


def demo_resume(payload: dict) -> dict:
    gate = payload.get("gate")
    if gate == "ecosystem_qa":
        return {
            "env_target": "local",
            "test_depth": "unit",
            "release_policy": "Stop at UAT. Do not deploy to production.",
        }
    if gate == "pipeline_approval":
        return {"decision": "approved", "approver": "demo"}
    if gate == "uat_signoff":
        return {"decision": "approved", "signer": "demo"}
    return {"decision": "approved"}


def drive(graph, state: dict, resume_for, *, echo: bool = True) -> dict:
    run_id = state["run_id"]
    thread = {"configurable": {"thread_id": run_id}, "recursion_limit": 80}
    pending: dict | Command = state
    seen_trace = 0
    seen_decisions = 0

    def _print(message: str) -> None:
        if echo:
            print(message, flush=True)

    token = set_printer(_print)
    try:
        for _ in range(12):
            graph.invoke(pending, thread)
            snapshot = graph.get_state(thread)
            values = snapshot.values
            if echo:
                for node in (values.get("trace") or [])[seen_trace:]:
                    print(f"[node] {node}", flush=True)
                for item in (values.get("jev_decisions") or [])[seen_decisions:]:
                    print(
                        scrub(
                            f"[jev] {item['schema']} via {item.get('client')} "
                            f"model={item.get('model')} primitives={item.get('primitives')} "
                            f"choice={item.get('choice')} noul={item.get('noul')}"
                        ),
                        flush=True,
                    )
                    applied = item.get("applied")
                    if applied and applied != item["decision"]:
                        print(scrub(f"[guardrail] {json.dumps(applied, default=str)}"), flush=True)
            seen_trace = len(values.get("trace") or [])
            seen_decisions = len(values.get("jev_decisions") or [])
            payload = interrupt_payload(graph, thread)
            if payload is not None:
                resume = resume_for(payload)
                if echo:
                    print(f"[interrupt] {payload.get('gate')}", flush=True)
                    print(f"[resume] {json.dumps(resume)}", flush=True)
                pending = Command(resume=resume)
                continue
            if not snapshot.next:
                return values
            raise RuntimeError(f"graph paused at {snapshot.next} without an interrupt")
        raise RuntimeError("stopped after 12 interrupt resumes")
    finally:
        reset_printer(token)


def _print_schemas() -> None:
    print("Jev client langchain_typesafe.TypeSafeClassifier", flush=True)
    print("Jev endpoint https://api.typesafe.ai/v1/systemone", flush=True)
    print("clarify: Noul needs_human + Choice skip|ask", flush=True)
    print("rank: Choice among candidate pipeline ids", flush=True)
    print("gate: Choice pass|remediate|escalate|skip", flush=True)
    print("Astra structured-output schemas", flush=True)
    for schema in (ClarifyDecision, RankDecision, GateDecision, PlanProposal):
        fields = list(schema.model_json_schema().get("properties", {}))
        print(f"  {schema.__name__}: {', '.join(fields)}", flush=True)


def main() -> int:
    if not config.get_openai_api_key():
        print(
            "OPENAI_API_KEY is not set. Add it to langgraph_support_app/.env "
            "or export it before running the orchestrator demo."
        )
        return 1
    if not config.get_typesafe_api_key():
        print(
            "TYPESAFE_API_KEY is not set. Add it to langgraph_support_app/.env "
            "or export it before running the orchestrator demo."
        )
        return 1
    root = config.PROJECT_DIR
    var = root / "var"
    var.mkdir(parents=True, exist_ok=True)
    state = initial_state(str(root))
    print(f"run_id {state['run_id']}", flush=True)
    print(f"Jev model {config.jev_model()}", flush=True)
    print(f"Astra model {config.astra_model()}", flush=True)
    _print_schemas()
    db_path = str(var / "orchestrator.sqlite")
    with open_checkpointer(db_path) as checkpointer:
        graph = build_graph(checkpointer)
        values = drive(graph, state, demo_resume, echo=True)
    report = scrub(values.get("report") or "")
    values = dict(values)
    values["report"] = report
    document = json.loads(scrub(json.dumps(machine_report(values), default=str)))
    document["past_uat"] = False
    print("\n" + report, flush=True)
    report_path = var / "report.md"
    json_path = var / "report.json"
    report_path.write_text(report, encoding="utf-8")
    json_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"report file {report_path}", flush=True)
    print(f"report json {json_path}", flush=True)
    print(f"checkpointer {db_path}", flush=True)
    return 0 if values.get("status") == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
