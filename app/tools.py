from __future__ import annotations

import json
import sqlite3
from threading import Lock
from typing import Any

from . import config

_lock = Lock()
_conn = sqlite3.connect(":memory:", check_same_thread=False)
_conn.row_factory = sqlite3.Row
_knowledge: dict[str, str] = {}
_instructions = ""
_warnings: list[str] = []


def _create_schema() -> None:
    _conn.executescript(
        """
        DROP TABLE IF EXISTS orders;
        DROP TABLE IF EXISTS products;
        DROP TABLE IF EXISTS customers;

        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            plan_type TEXT NOT NULL CHECK(plan_type IN ('free', 'pro', 'enterprise')),
            created_at TEXT NOT NULL
        );

        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            price REAL NOT NULL,
            in_stock INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            total_price REAL NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending', 'shipped', 'delivered', 'refunded')),
            created_at TEXT NOT NULL,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );
        """
    )
    _conn.commit()


def load_resources() -> None:
    """Load the sample store, policy docs, and support prompt."""
    global _knowledge, _instructions

    _warnings.clear()
    _create_schema()

    if not config.DATA_FILE.exists():
        _warnings.append(f"Data file not found: {config.DATA_FILE}")
        data: dict[str, list] = {"customers": [], "products": [], "orders": []}
    else:
        try:
            data = json.loads(config.DATA_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _warnings.append(f"Could not read data file {config.DATA_FILE}: {exc}")
            data = {"customers": [], "products": [], "orders": []}

    with _lock:
        for customer in data.get("customers", []):
            _conn.execute(
                """
                INSERT INTO customers (id, name, email, plan_type, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    customer["id"],
                    customer["name"],
                    customer["email"],
                    customer["plan_type"],
                    customer["created_at"],
                ),
            )

        for product in data.get("products", []):
            _conn.execute(
                """
                INSERT INTO products (id, name, category, price, in_stock)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    product["id"],
                    product["name"],
                    product["category"],
                    product["price"],
                    int(product.get("in_stock", 1)),
                ),
            )

        for order in data.get("orders", []):
            _conn.execute(
                """
                INSERT INTO orders
                    (id, customer_id, product_name, quantity, total_price, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order["id"],
                    order["customer_id"],
                    order["product_name"],
                    order["quantity"],
                    order["total_price"],
                    order["status"],
                    order["created_at"],
                ),
            )

        _conn.commit()

    if config.KNOWLEDGE_BASE_DIR.exists():
        _knowledge = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(config.KNOWLEDGE_BASE_DIR.glob("*.md"))
        }
    else:
        _knowledge = {}
        _warnings.append(f"Knowledge base directory not found: {config.KNOWLEDGE_BASE_DIR}")

    if config.INSTRUCTIONS_FILE.exists():
        _instructions = config.INSTRUCTIONS_FILE.read_text(encoding="utf-8").strip()
    else:
        _instructions = (
            "You are a customer-support agent. "
            "Use the tool result to answer questions about products, orders, and support policies."
        )
        _warnings.append(f"Instructions file not found: {config.INSTRUCTIONS_FILE}")


def get_instructions() -> str:
    return _instructions


def warnings() -> list[str]:
    return list(_warnings)


def knowledge_files() -> list[str]:
    return sorted(_knowledge)


def search_orders(
    customer_email: str | None = None,
    order_id: int | None = None,
    order_status: str | None = None,
) -> list[dict[str, Any]] | dict[str, str]:
    query = """
        SELECT
            o.id AS order_id,
            c.name,
            c.email AS customer_email,
            o.product_name,
            o.quantity,
            o.total_price,
            o.status AS order_status,
            o.created_at
        FROM orders AS o
        JOIN customers AS c ON o.customer_id = c.id
        WHERE 1 = 1
    """
    params: list[Any] = []

    if customer_email:
        query += " AND LOWER(c.email) = LOWER(?)"
        params.append(customer_email)

    if order_id is not None:
        query += " AND o.id = ?"
        params.append(order_id)

    if order_status:
        query += " AND o.status = ?"
        params.append(order_status)

    query += " ORDER BY o.created_at DESC"

    with _lock:
        rows = _conn.execute(query, params).fetchall()

    results = [dict(row) for row in rows]
    if not results:
        return {"message": "No orders matched the query."}
    return results


def search_products(
    product_category: str | None = None,
    product_name_fuzzy: str | None = None,
    in_stock_only: bool = False,
) -> list[dict[str, Any]] | dict[str, str]:
    query = """
        SELECT
            p.id AS product_id,
            p.name AS product_name,
            p.category AS product_category,
            p.in_stock AS product_stock_status,
            p.price AS product_price
        FROM products AS p
        WHERE 1 = 1
    """
    params: list[Any] = []

    if product_category:
        query += " AND LOWER(p.category) = LOWER(?)"
        params.append(product_category)

    if product_name_fuzzy:
        query += " AND LOWER(p.name) LIKE LOWER(?)"
        params.append(f"%{product_name_fuzzy}%")

    if in_stock_only:
        query += " AND p.in_stock = 1"

    query += " ORDER BY p.price ASC"

    with _lock:
        rows = _conn.execute(query, params).fetchall()

    results = [dict(row) for row in rows]
    if not results:
        return {"message": "No products matched the query."}
    return results


def search_knowledge_base(query: str) -> list[dict[str, Any]] | dict[str, str]:
    keywords = [word for word in query.lower().split() if word]
    if not keywords:
        return {"message": "No searchable keywords were provided."}

    matches: list[dict[str, Any]] = []
    for filename, content in _knowledge.items():
        lowered = content.lower()
        matched = sorted({word for word in keywords if word in lowered})
        if not matched:
            continue
        matches.append(
            {
                "document": filename,
                "matched_keywords": matched,
                "relevance": len(matched) / len(keywords),
                "content": content,
            }
        )

    matches.sort(key=lambda item: item["relevance"], reverse=True)
    if not matches:
        return {"message": "The chosen keywords did not match any knowledge-base document."}
    return matches


def status_snapshot() -> dict[str, Any]:
    with _lock:
        counts = {
            "customers": _conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
            "products": _conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],
            "orders": _conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
            "knowledge_base_documents": len(_knowledge),
        }
        categories = [
            row["category"]
            for row in _conn.execute(
                "SELECT DISTINCT category FROM products ORDER BY category"
            ).fetchall()
        ]

    return {
        "model": config.openai_model(),
        "api_key_configured": bool(config.get_openai_api_key()),
        "counts": counts,
        "product_categories": categories,
        "knowledge_base_files": knowledge_files(),
        "warnings": warnings(),
        "mcp_config": str(config.MCP_CONFIG_FILE),
        "mcp_config_exists": config.MCP_CONFIG_FILE.exists(),
    }


load_resources()
