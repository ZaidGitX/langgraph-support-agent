from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("support_tools")

_PLAN_BENEFITS = {
    "free": {
        "plan_type": "free",
        "price": "$0",
        "benefits": [
            "100 API calls per month",
            "Email support with a 48-hour response time",
            "Standard product catalog",
        ],
    },
    "pro": {
        "plan_type": "pro",
        "price": "$29/month",
        "benefits": [
            "10,000 API calls per month",
            "Priority email and chat support with a 4-hour response time",
            "Full catalog, including early releases",
            "Bulk ordering with a 10% discount",
        ],
    },
    "enterprise": {
        "plan_type": "enterprise",
        "price": "Custom pricing",
        "benefits": [
            "Unlimited API calls",
            "Dedicated account manager",
            "24/7 phone, email, and chat support",
            "Volume pricing with up to a 30% discount",
        ],
    },
}

_SHIPPING_ETAS = {
    "pending": "5-7 business days",
    "shipped": "2-3 business days",
    "delivered": "Already delivered",
    "refunded": "This order was refunded, so there is no delivery estimate.",
}


@mcp.tool()
def estimate_refund(order_total: float, days_since_delivery: int) -> dict[str, Any]:
    """Estimate the refund for an order.

    Full refund when delivery was 30 days ago or sooner.
    Half refund when delivery was 31 to 60 days ago.
    No refund after 60 days.
    """
    try:
        total = float(order_total)
        days = int(days_since_delivery)
        if total < 0 or days < 0:
            return {"error": "order_total and days_since_delivery must be zero or greater."}

        if days <= 30:
            refund = total
            policy = "full"
        elif days <= 60:
            refund = round(total * 0.5, 2)
            policy = "half"
        else:
            refund = 0
            policy = "none"

        return {
            "order_total": total,
            "days_since_delivery": days,
            "refund_amount": refund,
            "policy": policy,
        }
    except (TypeError, ValueError) as exc:
        return {"error": f"Could not estimate the refund: {exc}"}


@mcp.tool()
def shipping_eta(status: str) -> dict[str, Any]:
    """Return a simple delivery estimate for an order status.

    Accepted statuses are pending, shipped, delivered, and refunded.
    """
    try:
        cleaned = status.strip().lower()
        message = _SHIPPING_ETAS.get(cleaned)
        if message is None:
            return {
                "error": f"Unknown status {status!r}.",
                "allowed_statuses": sorted(_SHIPPING_ETAS),
            }
        return {"status": cleaned, "eta": message}
    except (TypeError, ValueError, AttributeError) as exc:
        return {"error": f"Could not estimate shipping: {exc}"}


@mcp.tool()
def plan_benefits(plan_type: str) -> dict[str, Any]:
    """Return benefits for a plan.

    plan_type may be free, pro, enterprise, paid, or all.
    paid returns the Pro and Enterprise plans.
    all returns Free, Pro, and Enterprise.
    """
    try:
        cleaned = plan_type.strip().lower()
        if cleaned in {"paid", "premium"}:
            return {
                "plan_type": "paid",
                "plans": [dict(_PLAN_BENEFITS["pro"]), dict(_PLAN_BENEFITS["enterprise"])],
            }
        if cleaned in {"all", "any"}:
            return {
                "plan_type": "all",
                "plans": [dict(plan) for plan in _PLAN_BENEFITS.values()],
            }
        benefits = _PLAN_BENEFITS.get(cleaned)
        if benefits is None:
            return {
                "error": f"Unknown plan {plan_type!r}.",
                "allowed_plans": ["free", "pro", "enterprise", "paid", "all"],
            }
        return dict(benefits)
    except (TypeError, ValueError, AttributeError) as exc:
        return {"error": f"Could not look up the plan: {exc}"}


if __name__ == "__main__":
    mcp.run(transport="stdio")
