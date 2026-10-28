"""Shared graph driver for tests.

The web UI is the only place a human answers ecosystem questions, pipeline
approval, and UAT sign-off. This module does not submit those answers.
"""

from __future__ import annotations

import json

from langgraph.types import Command

from .graph import interrupt_payload
from .llm import scrub
from .runner import reset_printer, set_printer


def drive(graph, state: dict, resume_for, *, echo: bool = True) -> dict:
    run_id = state["run_id"]
    thread = {"configurable": {"thread_id": run_id}, "recursion_limit": 160}
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


def main() -> int:
    print("The orchestrator session runs in the web UI.", flush=True)
    print("Open /#/orchestrator and answer the ecosystem questions there.", flush=True)
    print("This command does not submit ecosystem answers, pipeline approval, or UAT sign-off.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
