from __future__ import annotations

import ast
import json
import logging
import re
import sys
from typing import Any, Literal

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from . import config, tools
from .schemas import (
    TOOL_OUTPUT_SPECS,
    AgentRequest,
    AgentState,
    SearchKnowledgeBaseArgs,
    SearchOrdersArgs,
    SearchProductsArgs,
    ToolArgs,
    model_tool_output_schema,
)

logger = logging.getLogger("support_agent")

Route = Literal["orders", "products", "knowledge", "mcp", "general"]
_STATUSES = ("pending", "shipped", "delivered", "refunded")


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


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


def _is_plan_question(text: str) -> bool:
    if re.search(r"\b(plans?|subscription|membership)\b", text):
        return True
    if not _has_word(text, "pricing"):
        return False
    product_names = (
        "keyboard",
        "mouse",
        "webcam",
        "chair",
        "desk",
        "headphone",
        "headphones",
        "charger",
        "hub",
        "stand",
    )
    return not any(_has_word(text, name) for name in product_names)


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
    return _is_plan_question(text)


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
    if _is_plan_question(text):
        named = {name for name in ("enterprise", "pro", "free") if _has_word(text, name)}
        if len(named) == 1:
            plan_type = next(iter(named))
        elif named == {"pro", "enterprise"} or (_has_word(text, "paid") and "free" not in named):
            plan_type = "paid"
        else:
            plan_type = "all"
        return "plan_benefits", {"plan_type": plan_type}

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


_FILL_GUIDE = {
    SearchOrdersArgs: (
        "Extract only an email, order id, or status the customer actually gave. "
        "Leave a field empty when the question does not contain it."
    ),
    SearchProductsArgs: (
        "Catalog categories are Electronics, Accessories, and Furniture. "
        "Set product_name_fuzzy only when that exact word appears in a product name. "
        "For a theme such as gaming, set product_category to Electronics and leave "
        "product_name_fuzzy empty so Wireless Mouse and Mechanical Keyboard are included."
    ),
    SearchKnowledgeBaseArgs: (
        "Set query to a few keywords that would appear in a policy document, "
        "such as return, shipping, warranty, or pricing."
    ),
}


def _missed(result: Any) -> bool:
    return isinstance(result, dict) and isinstance(result.get("message"), str)


async def fill_tool_args(schema: type[ToolArgs], state: AgentState, hint: str = "") -> ToolArgs:
    """Ask the model to fill the Pydantic tool schema for this question."""
    name, description = TOOL_OUTPUT_SPECS[schema]
    schema_payload = model_tool_output_schema(name, description, schema)
    model = ChatOpenAI(
        model=config.openai_model(),
        api_key=config.get_openai_api_key(),
        use_responses_api=True,
    ).with_structured_output(schema)
    prompt = (
        "Fill only the tool arguments described by this schema:\n"
        f"{json.dumps(schema_payload)}\n\n"
        f"Catalog: {tools.catalog_for_prompt()}\n"
        f"{_FILL_GUIDE[schema]}\n"
        f"{hint}".strip()
    )
    filled = await model.ainvoke(
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": state.query},
        ]
    )
    if isinstance(filled, schema):
        return filled
    return schema.model_validate(filled)


async def _lookup_with_schema(
    state: AgentState,
    schema: type[ToolArgs],
    search,
    route: str,
) -> dict[str, Any]:
    tool_name = TOOL_OUTPUT_SPECS[schema][0]
    hint = ""
    args: ToolArgs | None = None
    result: Any = {"message": "No lookup was run."}
    attempts = max(1, min(state.iterations, 2))
    for _ in range(attempts):
        args = await fill_tool_args(schema, state, hint)
        payload = args.model_dump()
        if schema is SearchKnowledgeBaseArgs:
            result = search(payload["query"])
        else:
            result = search(**payload)
        if not _missed(result):
            break
        hint = (
            f"These arguments matched nothing: {payload}. "
            "Broaden the search. Do not repeat a keyword that is not in the catalog."
        )
    assert args is not None
    return _tool_update(route, tool_name, args.model_dump(), result)


def classify_node(state: AgentState) -> dict[str, Any]:
    return {"route": classify(state.query)}


async def orders_node(state: AgentState) -> dict[str, Any]:
    return await _lookup_with_schema(state, SearchOrdersArgs, tools.search_orders, "orders")


async def products_node(state: AgentState) -> dict[str, Any]:
    return await _lookup_with_schema(state, SearchProductsArgs, tools.search_products, "products")


async def knowledge_node(state: AgentState) -> dict[str, Any]:
    return await _lookup_with_schema(
        state,
        SearchKnowledgeBaseArgs,
        tools.search_knowledge_base,
        "knowledge",
    )


async def mcp_node(state: AgentState) -> dict[str, Any]:
    tool_name, arguments = select_mcp_tool(state.query)
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


async def compose_answer(
    query: str,
    route: str,
    tool_result: str,
    system_instructions: str | None = None,
) -> str:
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
    instructions = system_instructions or tools.get_instructions()
    prompt = (
        f"{instructions}\n\n"
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
        query=state.query,
        route=state.route or "general",
        tool_result=state.tool_result,
        system_instructions=state.system_instructions,
    )
    return {"answer": answer}


def _route(state: AgentState) -> str:
    return state.route or "general"


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


async def run_agent(request: AgentRequest) -> dict[str, Any]:
    result = await graph.ainvoke(AgentState.model_validate(request.model_dump()))
    trace = result.get("trace") or []
    return {
        "answer": result.get("answer") or "",
        "route": result.get("route") or "general",
        "tool_trace": trace if request.include_trace else None,
    }
