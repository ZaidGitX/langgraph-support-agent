from __future__ import annotations

import ast
import json
import logging
import re
import sys
from typing import Any, Literal, TypedDict

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from . import config, tools

logger = logging.getLogger("support_agent")

Route = Literal["orders", "products", "knowledge", "mcp", "general"]
_STATUSES = ("pending", "shipped", "delivered", "refunded")
_CATEGORIES = ("electronics", "accessories", "furniture")
_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_ORDER_ID = re.compile(r"\b(\d{3,})\b")
_PRODUCT_STOPWORDS = {
    "a",
    "an",
    "any",
    "are",
    "catalog",
    "do",
    "have",
    "how",
    "in",
    "is",
    "me",
    "much",
    "of",
    "price",
    "product",
    "products",
    "show",
    "stock",
    "the",
    "there",
    "what",
    "you",
}


class AgentState(TypedDict, total=False):
    query: str
    route: Route
    tool_name: str
    arguments: dict[str, Any]
    tool_result: str
    answer: str
    trace: list[dict[str, Any]]


def classify(query: str) -> Route:
    """Pick one route. MCP phrases win, then orders, products, and policy."""
    text = query.lower()
    if _is_mcp(text):
        return "mcp"
    if _is_orders(text):
        return "orders"
    if _is_products(text):
        return "products"
    if any(word in text for word in ("return", "refund", "shipping", "warranty", "policy", "faq", "pricing")):
        return "knowledge"
    return "general"


def _is_mcp(text: str) -> bool:
    if any(
        phrase in text
        for phrase in (
            "refund estimate",
            "estimate a refund",
            "estimate the refund",
            "estimate refund",
            "shipping eta",
            "delivery eta",
            "when will it arrive",
            "when will my order arrive",
        )
    ):
        return True
    if re.search(r"\beta\b", text):
        return True
    return "plan" in text and any(
        word in text for word in ("benefit", "include", "includes", "included", "free", "pro", "enterprise")
    )


def _is_orders(text: str) -> bool:
    if "@" in text or re.search(r"\border\b", text):
        return True
    return any(status in text for status in _STATUSES)


def _is_products(text: str) -> bool:
    return any(
        hint in text
        for hint in ("product", "stock", "catalog", "electronics", "accessories", "furniture", "price")
    )


def select_mcp_tool(query: str) -> tuple[str, dict[str, Any]]:
    text = query.lower()
    if "plan" in text:
        plan = "free"
        for name in ("enterprise", "pro", "free"):
            if name in text:
                plan = name
                break
        return "plan_benefits", {"plan_type": plan}

    if any(phrase in text for phrase in ("eta", "when will", "arrive")):
        status = "shipped"
        for name in _STATUSES:
            if name in text:
                status = name
                break
        return "shipping_eta", {"status": status}

    return "estimate_refund", _refund_arguments(query)


def _refund_arguments(query: str) -> dict[str, Any]:
    days_match = re.search(r"(\d+)\s*days?", query, re.IGNORECASE)
    days = int(days_match.group(1)) if days_match else None
    total = None
    for number in re.findall(r"\d+(?:\.\d+)?", query):
        value = float(number)
        if days is not None and value == float(days):
            continue
        total = value
        break
    if total is None or days is None:
        return {}
    return {"order_total": total, "days_since_delivery": days}


def _order_arguments(query: str) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    email = _EMAIL.search(query)
    if email:
        arguments["customer_email"] = email.group(0)
    order_id = _ORDER_ID.search(query)
    if order_id:
        arguments["order_id"] = int(order_id.group(1))
    for status in _STATUSES:
        if status in query.lower():
            arguments["order_status"] = status
            break
    return arguments


def _product_arguments(query: str) -> dict[str, Any]:
    text = query.lower()
    arguments: dict[str, Any] = {"in_stock_only": "in stock" in text}
    for category in _CATEGORIES:
        if category in text:
            arguments["product_category"] = category.capitalize()
            break
    words = [
        word
        for word in re.findall(r"[a-z0-9]+", text)
        if word not in _PRODUCT_STOPWORDS and word not in _CATEGORIES
    ]
    if words:
        arguments["product_name_fuzzy"] = " ".join(words[:4])
    return arguments


def load_mcp_connection() -> dict[str, Any]:
    raw = json.loads(config.MCP_CONFIG_FILE.read_text(encoding="utf-8"))
    server = raw["mcpServers"]["support_tools"]
    command = server["command"]
    if command == "python":
        command = sys.executable
    args = []
    for arg in server["args"]:
        path = config.PROJECT_DIR / arg
        args.append(str(path))
    return {
        "support_tools": {
            "transport": "stdio",
            "command": command,
            "args": args,
            "cwd": str(config.PROJECT_DIR),
        }
    }


def _parse_structured(text: str) -> Any | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None


def _coerce_tool_result(result: Any) -> dict[str, Any]:
    """Turn an MCP tool payload into a plain dict.

    LangChain sometimes returns a stringified list of content blocks instead of
    the tool's JSON object.
    """
    if isinstance(result, dict):
        text = result.get("text")
        if isinstance(text, str) and "type" in result:
            return _coerce_tool_result(text)
        return result

    if isinstance(result, list):
        texts = [
            item["text"]
            for item in result
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        if texts:
            return _coerce_tool_result("\n".join(texts))
        return {"result": result}

    if isinstance(result, str):
        stripped = result.strip()
        parsed = _parse_structured(stripped) if stripped.startswith(("{", "[")) else None
        if isinstance(parsed, (dict, list)):
            return _coerce_tool_result(parsed)
        return {"result": result}

    return {"result": str(result)}


async def call_mcp_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Spawn the FastMCP script over stdio and call one tool."""
    if not arguments:
        return {"error": f"{name} needs arguments parsed from the question."}
    try:
        client = MultiServerMCPClient(load_mcp_connection())
        available = await client.get_tools(server_name="support_tools")
        selected = next((tool for tool in available if tool.name == name), None)
        if selected is None:
            return {
                "error": f"MCP tool {name!r} is not available.",
                "available": [tool.name for tool in available],
            }
        return _coerce_tool_result(await selected.ainvoke(arguments))
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.warning("MCP tool %s failed: %s", name, exc)
        return {"error": f"MCP tool call failed: {exc}"}


def _trace(route: str, tool_name: str, arguments: dict[str, Any], result: Any) -> dict[str, Any]:
    preview = result if isinstance(result, str) else json.dumps(result)
    return {
        "route": route,
        "tool_name": tool_name,
        "arguments": arguments,
        "output_preview": preview[:2000],
    }


def _tool_update(route: str, tool_name: str, arguments: dict[str, Any], result: Any) -> dict[str, Any]:
    encoded = result if isinstance(result, str) else json.dumps(result)
    return {
        "tool_name": tool_name,
        "arguments": arguments,
        "tool_result": encoded,
        "trace": [_trace(route, tool_name, arguments, encoded)],
    }


def classify_node(state: AgentState) -> dict[str, Any]:
    return {"route": classify(state["query"])}


def orders_node(state: AgentState) -> dict[str, Any]:
    arguments = _order_arguments(state["query"])
    result = tools.search_orders(**arguments)
    return _tool_update("orders", "search_orders", arguments, result)


def products_node(state: AgentState) -> dict[str, Any]:
    arguments = _product_arguments(state["query"])
    result = tools.search_products(**arguments)
    return _tool_update("products", "search_products", arguments, result)


def knowledge_node(state: AgentState) -> dict[str, Any]:
    arguments = {"query": state["query"]}
    result = tools.search_knowledge_base(state["query"])
    return _tool_update("knowledge", "search_knowledge_base", arguments, result)


async def mcp_node(state: AgentState) -> dict[str, Any]:
    tool_name, arguments = select_mcp_tool(state["query"])
    result = await call_mcp_tool(tool_name, arguments)
    return _tool_update("mcp", tool_name, arguments, result)


def general_node(state: AgentState) -> dict[str, Any]:
    return _tool_update("general", "none", {}, "No lookup was required for this question.")


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or block.get("content") or ""
                if text:
                    parts.append(str(text))
            else:
                text = getattr(block, "text", "")
                if text:
                    parts.append(str(text))
        return "".join(parts).strip()
    return str(content).strip()


async def compose_answer(query: str, route: str, tool_result: str) -> str:
    api_key = config.get_openai_api_key()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Add it to langgraph_support_app/.env "
            "or export it before launching uvicorn."
        )

    model = ChatOpenAI(
        model=config.openai_model(),
        api_key=api_key,
        use_responses_api=True,
    )
    prompt = (
        f"{tools.get_instructions()}\n\n"
        f"The router selected the {route} path.\n"
        f"Tool result:\n{tool_result or 'No tool was called.'}\n\n"
        "Answer the customer in plain language. Use only the tool result for store facts. "
        "If the route is orders, cite the order database. "
        "If the route is products, cite the product catalog. "
        "If the route is knowledge, cite the document name. "
        "If the route is mcp, cite the support tool, not the order database. "
        "If the tool result is empty or an error, say what you could not look up."
    )
    message = await model.ainvoke(
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": query},
        ]
    )
    return _message_text(message) or "The model returned an empty answer."


async def respond_node(state: AgentState) -> dict[str, Any]:
    answer = await compose_answer(
        query=state["query"],
        route=state.get("route", "general"),
        tool_result=state.get("tool_result", ""),
    )
    return {"answer": answer}


def _route(state: AgentState) -> str:
    return state.get("route") or "general"


def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("classify", classify_node)
    builder.add_node("orders", orders_node)
    builder.add_node("products", products_node)
    builder.add_node("knowledge", knowledge_node)
    builder.add_node("mcp", mcp_node)
    builder.add_node("general", general_node)
    builder.add_node("respond", respond_node)
    builder.add_edge(START, "classify")
    builder.add_conditional_edges(
        "classify",
        _route,
        {
            "orders": "orders",
            "products": "products",
            "knowledge": "knowledge",
            "mcp": "mcp",
            "general": "general",
        },
    )
    for node in ("orders", "products", "knowledge", "mcp", "general"):
        builder.add_edge(node, "respond")
    builder.add_edge("respond", END)
    return builder.compile()


graph = build_graph()


async def run_agent(query: str) -> dict[str, Any]:
    result = await graph.ainvoke({"query": query, "trace": []})
    return {
        "answer": result.get("answer") or "",
        "route": result.get("route") or "general",
        "tool_trace": result.get("trace") or [],
    }
