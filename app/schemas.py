from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchOrdersArgs(ToolArgs):
    customer_email: str | None = Field(
        None,
        description="Customer email address to look up.",
    )
    order_id: int | None = Field(
        None,
        description="Specific order id to look up.",
    )
    order_status: Literal["pending", "shipped", "delivered", "refunded"] | None = Field(
        None,
        description="Filter orders by status.",
    )


class SearchProductsArgs(ToolArgs):
    product_category: str | None = Field(
        None,
        description="Product category, e.g. Electronics, Accessories, Furniture.",
    )
    product_name_fuzzy: str | None = Field(
        None,
        description="Keyword to search for in product names. Use only a word that appears in a product name.",
    )
    in_stock_only: bool = Field(
        False,
        description="If true, return products currently in stock.",
    )


class SearchKnowledgeBaseArgs(ToolArgs):
    query: str = Field(
        ...,
        description="Search keywords describing what the customer is asking about.",
    )


class AgentRequest(BaseModel):
    query: str = Field(..., min_length=1)
    iterations: int = Field(2, ge=1, le=25)
    system_instructions: str | None = Field(
        None,
        description="Optional override. Leave empty to use the prompt file.",
    )
    include_trace: bool = Field(
        True,
        description="Return the tool-call trace for debugging.",
    )

    @field_validator("system_instructions")
    @classmethod
    def drop_placeholder(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned or cleaned.lower() == "string":
            return None
        return cleaned


class AgentResponse(BaseModel):
    answer: str
    route: str
    tool_trace: list[dict[str, Any]] | None = None


class AgentState(AgentRequest):
    """Graph state. The request payload is the input the machine runs on."""

    route: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    tool_result: str = ""
    answer: str = ""
    trace: list[dict[str, Any]] = Field(default_factory=list)


def model_tool_output_schema(name: str, description: str, args: type[ToolArgs]) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": args.model_json_schema(),
    }


TOOL_OUTPUT_SPECS: dict[type[ToolArgs], tuple[str, str]] = {
    SearchOrdersArgs: (
        "search_orders",
        "Search for orders in the database by customer email, order id, and order status.",
    ),
    SearchProductsArgs: (
        "search_products",
        "Search for products by product category, keyword, and whether or not the product is in stock.",
    ),
    SearchKnowledgeBaseArgs: (
        "search_knowledge_base",
        "Search the FAQ or support policy docs via keywords from the user query.",
    ),
}

tool_output_possibilities_for_model = [
    model_tool_output_schema(name, description, args)
    for args, (name, description) in TOOL_OUTPUT_SPECS.items()
]
