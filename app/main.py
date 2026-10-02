from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, tools
from .graph import run_agent
from .schemas import AgentRequest, AgentResponse

app = FastAPI(title="LangGraph Support Agent", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DIST_DIR = config.PROJECT_DIR / "frontend" / "dist"
OrderStatus = Literal["pending", "shipped", "delivered", "refunded"]


@app.get("/status")
def status() -> dict[str, Any]:
    return tools.status_snapshot()


@app.post("/ask", response_model=AgentResponse)
async def ask_agent(request: AgentRequest) -> AgentResponse:
    if not config.get_openai_api_key():
        raise HTTPException(
            status_code=400,
            detail=(
                "OPENAI_API_KEY is not set. Add it to langgraph_support_app/.env "
                "or export it before launching uvicorn."
            ),
        )
    try:
        result = await run_agent(request)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return AgentResponse(**result)


@app.get("/orders")
def orders(
    customer_email: str | None = None,
    order_id: int | None = None,
    order_status: OrderStatus | None = None,
) -> Any:
    return tools.search_orders(
        customer_email=customer_email,
        order_id=order_id,
        order_status=order_status,
    )


@app.get("/products")
def products(
    product_category: str | None = None,
    product_name_fuzzy: str | None = None,
    in_stock_only: bool = False,
) -> Any:
    return tools.search_products(
        product_category=product_category,
        product_name_fuzzy=product_name_fuzzy,
        in_stock_only=in_stock_only,
    )


@app.get("/knowledge-base/search")
def knowledge_base_search(query: str = Query(..., min_length=1)) -> Any:
    return tools.search_knowledge_base(query)


@app.get("/")
def root() -> Any:
    index = DIST_DIR / "index.html"
    if index.is_file():
        return FileResponse(index)
    return {"message": "Runtime agent API is running."}


_assets = DIST_DIR / "assets"
if _assets.is_dir():
    app.mount("/assets", StaticFiles(directory=_assets), name="assets")
