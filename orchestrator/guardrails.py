"""Hard dispatcher limits. These are not model opinions."""

from __future__ import annotations

from .schemas import GateDecision
from .state import (
    MAX_ATTEMPTS,
    MAX_PLAN_ATTEMPTS,
    blocked_stages,
    budget_reason,
    failed_stages,
    pending_stages,
)


def _escalate(decision: GateDecision, reason: str) -> GateDecision:
    return decision.model_copy(
        update={
            "route": "escalate",
            "retry": False,
            "replan": False,
            "retry_stages": [],
            "rationale": f"{decision.rationale} | guardrail: {reason}",
        }
    )


def apply_guardrails(state: dict, decision: GateDecision) -> GateDecision:
    reason = budget_reason(state)
    if reason:
        return _escalate(decision, reason)
    if blocked_stages(state):
        return _escalate(decision, "blocked target has no retry path and no path past UAT")

    failures = failed_stages(state)
    if decision.route == "escalate" and not failures:
        return decision.model_copy(
            update={
                "route": "pass",
                "retry": False,
                "replan": False,
                "retry_stages": [],
                "rationale": (
                    f"{decision.rationale} | guardrail: nothing failed, "
                    "so the run is not escalated"
                ),
            }
        )
    if decision.route in {"pass", "skip"} and failures:
        decision = decision.model_copy(
            update={
                "route": "remediate",
                "retry": True,
                "retry_stages": failures,
                "rationale": f"{decision.rationale} | guardrail: evidence still has failures",
            }
        )

    if decision.route != "remediate":
        return decision

    if decision.replan:
        if int((state.get("attempt") or {}).get("plan", 0)) < MAX_PLAN_ATTEMPTS:
            return decision
        return _escalate(decision, "plan retries exhausted")

    wanted = decision.retry_stages or failures
    retryable = [
        name
        for name in wanted
        if int((state.get("attempt") or {}).get(name, 0)) < MAX_ATTEMPTS
    ]
    if decision.retry and retryable:
        return decision.model_copy(update={"retry": True, "retry_stages": retryable, "replan": False})
    if pending_stages(state) and not failures:
        return decision.model_copy(update={"route": "pass", "retry": False, "retry_stages": []})
    return _escalate(decision, "bounded retries exhausted")
