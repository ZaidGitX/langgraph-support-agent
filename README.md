# Support agent

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
