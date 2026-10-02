from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
RESOURCE_DIR = PROJECT_DIR / "resources"
DATA_FILE = RESOURCE_DIR / "data" / "customer_support_sample.json"
KNOWLEDGE_BASE_DIR = RESOURCE_DIR / "data" / "knowledge_base"
INSTRUCTIONS_FILE = RESOURCE_DIR / "prompts" / "customer_support_agent_instructions.txt"
MCP_CONFIG_FILE = PROJECT_DIR / "mcp_server" / "client_config.json"


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def load_env_files() -> None:
    """Load this app's .env only. Never read the parent practice folder."""
    _load_env_file(PROJECT_DIR / ".env")


def get_openai_api_key() -> str:
    load_env_files()
    return os.getenv("OPENAI_API_KEY", "").strip()


def openai_model() -> str:
    load_env_files()
    return os.getenv("OPENAI_MODEL", "gpt-6-astra").strip() or "gpt-6-astra"
