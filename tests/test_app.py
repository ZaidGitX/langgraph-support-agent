from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import config
from app.graph import classify, load_mcp_connection, select_mcp_tool
from app.main import app
from app.schemas import SearchProductsArgs, model_tool_output_schema
from app.tools import search_knowledge_base, search_orders, search_products
from mcp_server.server import estimate_refund, plan_benefits, shipping_eta
from scripts.audit_gate import fixable_vulnerabilities

client = TestClient(app)


def test_search_orders_by_email():
    results = search_orders(customer_email="alice@example.com")
    assert isinstance(results, list)
    assert {row["order_id"] for row in results} == {1001, 1002}


def test_search_products_in_stock_excludes_webcam():
    results = search_products(in_stock_only=True)
    names = [row["product_name"] for row in results]
    assert "Wireless Mouse" in names
    assert "Webcam HD 1080p" not in names


def test_search_knowledge_base_matches_return_policy():
    results = search_knowledge_base("return policy")
    assert isinstance(results, list)
    assert results[0]["document"] == "return_policy.md"


def test_classify_routes():
    assert classify("Where is the order for alice@example.com?") == "orders"
    assert classify("Is the keyboard in stock?") == "products"
    assert classify("What is your return policy?") == "knowledge"
    assert classify("Estimate a refund for 40 delivered 45 days ago") == "mcp"
    assert classify("What does the pro plan include?") == "mcp"
    assert classify("What are the paid plans?") == "mcp"
    assert classify("Find me relevant gaming products") == "products"
    assert classify("Hello there") == "general"


def test_select_mcp_tool_parses_refund():
    name, arguments = select_mcp_tool("Estimate a refund for 40 delivered 45 days ago")
    assert name == "estimate_refund"
    assert arguments == {"order_total": 40.0, "days_since_delivery": 45}


def test_paid_plans_select_both_paid_tiers():
    name, arguments = select_mcp_tool("What are the paid plans?")
    assert name == "plan_benefits"
    assert arguments == {"plan_type": "paid"}
    result = plan_benefits("paid")
    tiers = {plan["plan_type"] for plan in result["plans"]}
    assert tiers == {"pro", "enterprise"}
    assert plan_benefits("Pro")["price"] == "$29/month"
    pro_name, pro_arguments = select_mcp_tool("What does the pro plan include?")
    assert pro_name == "plan_benefits"
    assert pro_arguments == {"plan_type": "pro"}
    all_name, all_arguments = select_mcp_tool("What are your pricing plans?")
    assert all_name == "plan_benefits"
    assert all_arguments == {"plan_type": "all"}


def test_refund_policy_boundaries():
    assert estimate_refund(40, 30)["policy"] == "full"
    assert estimate_refund(40, 45) == {
        "order_total": 40.0,
        "days_since_delivery": 45,
        "refund_amount": 20.0,
        "policy": "half",
    }
    assert estimate_refund(40, 61)["policy"] == "none"
    assert "error" in estimate_refund(-1, 10)


def test_shipping_eta_and_plan_benefits():
    assert shipping_eta("Shipped")["eta"] == "2-3 business days"
    assert "error" in shipping_eta("lost")
    assert plan_benefits("Pro")["price"] == "$29/month"
    assert "error" in plan_benefits("gold")


def test_client_config_points_at_one_script():
    raw = json.loads((config.PROJECT_DIR / "mcp_server" / "client_config.json").read_text())
    server = raw["mcpServers"]["support_tools"]
    assert server["command"] == "python"
    assert server["args"] == ["mcp_server/server.py"]
    connection = load_mcp_connection()["support_tools"]
    assert connection["command"] == sys.executable
    assert Path(connection["args"][0]).is_file()


def test_openapi_lists_lookup_routes():
    paths = client.get("/openapi.json").json()["paths"]
    for path in ("/orders", "/products", "/knowledge-base/search", "/status", "/ask"):
        assert path in paths


def test_status_counts():
    body = client.get("/status").json()
    assert body["counts"]["customers"] == 8
    assert body["counts"]["orders"] == 10
    assert body["counts"]["products"] == 10


def test_ask_requires_api_key(monkeypatch):
    monkeypatch.setattr("app.main.config.get_openai_api_key", lambda: "")
    response = client.post("/ask", json={"query": "Where is the order for alice@example.com?"})
    assert response.status_code == 400


def test_product_tool_schema_comes_from_pydantic():
    payload = model_tool_output_schema(
        "search_products",
        "Search products.",
        SearchProductsArgs,
    )
    assert payload["parameters"]["properties"]["product_category"]["description"].startswith(
        "Product category"
    )
    with pytest.raises(ValidationError):
        SearchProductsArgs(not_a_field=True)


def test_electronics_args_include_mouse_and_keyboard():
    args = SearchProductsArgs(product_category="Electronics")
    results = search_products(**args.model_dump())
    names = {row["product_name"] for row in results}
    assert {"Wireless Mouse", "Mechanical Keyboard"} <= names


def test_ask_order_route_uses_mocked_model(monkeypatch):
    async def fake_compose(query: str, route: str, tool_result: str, system_instructions=None) -> str:
        return f"{route}:{tool_result}"

    async def fake_fill(schema, state, hint=""):
        return schema(customer_email="alice@example.com")

    monkeypatch.setattr("app.main.config.get_openai_api_key", lambda: "test-key")
    monkeypatch.setattr("app.graph.compose_answer", fake_compose)
    monkeypatch.setattr("app.graph.fill_tool_args", fake_fill)
    response = client.post("/ask", json={"query": "Where is the order for alice@example.com?"})
    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "orders"
    assert body["tool_trace"][0]["tool_name"] == "search_orders"
    assert "alice@example.com" in body["answer"]


def test_ask_gaming_products_uses_structured_category(monkeypatch):
    async def fake_compose(query: str, route: str, tool_result: str, system_instructions=None) -> str:
        return tool_result

    async def fake_fill(schema, state, hint=""):
        assert schema is SearchProductsArgs
        assert "gaming" in state.query.lower()
        return SearchProductsArgs(product_category="Electronics")

    monkeypatch.setattr("app.main.config.get_openai_api_key", lambda: "test-key")
    monkeypatch.setattr("app.graph.compose_answer", fake_compose)
    monkeypatch.setattr("app.graph.fill_tool_args", fake_fill)
    response = client.post("/ask", json={"query": "Find me relevant gaming products"})
    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "products"
    assert "Wireless Mouse" in body["answer"]
    assert "Mechanical Keyboard" in body["answer"]
    assert body["tool_trace"][0]["arguments"]["product_category"] == "Electronics"
    assert body["tool_trace"][0]["arguments"]["product_name_fuzzy"] is None


def test_ask_paid_plans_uses_mcp(monkeypatch):
    async def fake_compose(query: str, route: str, tool_result: str, system_instructions=None) -> str:
        return tool_result

    monkeypatch.setattr("app.main.config.get_openai_api_key", lambda: "test-key")
    monkeypatch.setattr("app.graph.compose_answer", fake_compose)
    response = client.post("/ask", json={"query": "What are the paid plans?"})
    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "mcp"
    assert body["tool_trace"][0]["tool_name"] == "plan_benefits"
    assert body["tool_trace"][0]["arguments"]["plan_type"] == "paid"
    assert "29" in body["answer"]
    assert "enterprise" in body["answer"].lower()


def test_ask_refund_routes_through_mcp(monkeypatch):
    async def fake_compose(query: str, route: str, tool_result: str, system_instructions=None) -> str:
        return f"{route}:{tool_result}"

    monkeypatch.setattr("app.main.config.get_openai_api_key", lambda: "test-key")
    monkeypatch.setattr("app.graph.compose_answer", fake_compose)
    response = client.post(
        "/ask",
        json={"query": "Estimate a refund for 40 delivered 45 days ago"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "mcp"
    assert body["tool_trace"][0]["tool_name"] == "estimate_refund"
    assert "refund_amount" in body["answer"]
    assert "20.0" in body["answer"]


def test_audit_gate_flags_fixable_vulnerability():
    report = {
        "dependencies": [
            {
                "name": "demo",
                "vulns": [{"id": "PYSEC-1", "fix_versions": ["1.2.3"]}],
            }
        ]
    }
    assert fixable_vulnerabilities(report) == ["demo PYSEC-1 fix: 1.2.3"]
    assert fixable_vulnerabilities({"dependencies": []}) == []
