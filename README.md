# Support agent - Sprint 2

A small customer-support app. LangGraph routes each question to an order lookup, a product lookup, the policy docs, or a FastMCP tool. FastAPI still exposes those lookups, and a React page sits on top.

The MCP server is one script, started over stdio the same way a desktop MCP config starts Python. It is not a separate port you have to leave running.

## Run locally

Use the existing `tcs_practice_env` conda environment:

```bash
conda activate tcs_practice_env
cd langgraph_support_app
pip install -r requirements-dev.txt
cp .env.example .env
```

Put your OpenAI key in `.env`. Do not commit that file.

```bash
uvicorn app.main:app --reload --port 8000
```

In another terminal, with Node installed:

```bash
cd frontend
npm install
npm run dev
```

Open http://127.0.0.1:5173. API docs stay at http://127.0.0.1:8000/docs.

`mcp_server/client_config.json` is the stdio launch config. The API resolves `mcp_server/server.py` from the project root. A desktop client such as Claude needs the absolute path to that script.

## Docker

```bash
docker compose up --build
```

Open http://127.0.0.1:8000. The image serves the built React page and the API together.

## Tests

```bash
pytest
ruff check app mcp_server tests scripts
```

GitHub Actions runs those checks, `pip-audit`, and `docker build` on every push. The image scan is a report. This repo does not deploy to a cloud host.

## Placeholders (Sprint 3)

`orchestrator/credentials.json` is the registry of every environment variable the orchestrator,
its providers, and its scanners may read. It holds names only. `.env.example`, the `environment`
block in `docker-compose.yml`, and the secrets block in `.github/workflows/orchestrator.yml` are
generated from it, and `GET /api/orchestrator/readiness` reports which names are set (never a
value). A provider whose names are missing runs in plan mode.

| Where you run | Where the values go |
| --- | --- |
| Locally | `.env` (copy `.env.example`; gitignored) |
| Docker Compose | the host environment; Compose passes each `NAME` through as `${NAME:-}` |
| A push to GitHub | repository secrets with the same names; an unset secret resolves to empty |

Cloud runners apply only when `ORCHESTRATOR_CLOUD_APPLY=1`; otherwise they record the exact
command they would run. Production is never a target.
