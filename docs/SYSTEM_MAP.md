# System map

This repo is two products in one process: a customer-support agent and an agentic CI/CD orchestrator. A push starts only the session workflow. That workflow boots Docker, opens a public URL, and waits. Astra writes the questions and the candidate pipelines. Jev chooses one pipeline. A person answers in the UI. Only the stages in that approved pipeline run.

## One push, under the hood

1. A push to GitHub starts `.github/workflows/orchestrator.yml` and no other workflow.
2. The job runs `docker compose up -d --build --wait`. The image serves the React UI and the FastAPI process. SQLite is the named volume `orchestrator-sqlite`.
3. `cloudflared tunnel --url http://127.0.0.1:8000` prints a public `trycloudflare.com` URL. The job registers that URL on the API so a later DAST stage can scan the running app.
4. The API starts one LangGraph thread. Astra writes the ecosystem questions. The UI stays on that screen until the person submits an answer for every question.
5. Astra proposes two or three stage DAGs. Jev (`langchain_typesafe.TypeSafeClassifier`) chooses exactly one. The UI then asks the person to approve or reject it.
6. After approval, each stage in that DAG runs its designated workflow with `gh workflow run`, or the same fixed local commands when `ORCHESTRATOR_DISPATCH` is not `github`. A stage that was not in the approved DAG is not dispatched.
7. A failed stage may retry until the attempt cap of 2. A retry cannot add a stage Jev did not approve. The DAG is kept acyclic. There is no production edge and no path past UAT.
8. If UAT is in the approved pipeline, the UI waits for a human sign-off.
9. The results screen lists only the approved stages and the Jev route. For Semgrep, pytest, Trivy, Snyk, CodeQL, Sonar, ZAP, and StackHawk, each tile shows the status, the findings taken from that step's log, and the next step. Astra writes the next step when the extract contains a finding or the step failed. The support tab calls `/api/support/status` and `/api/support/ask` on that same public host. After the results screen has been shown, the tunnel stays up for 10 minutes so the support agent can still be used, then the Actions job exits. If 60 minutes pass first, the job fails and does not submit answers.

## Who decides what

| Piece | Role |
| --- | --- |
| Astra (`OPENAI_MODEL`, default `gpt-6-astra`) | Writes ecosystem questions, proposes candidate DAGs, writes a short stage brief before a stage runs, and diagnoses a failure. After test, scanners, or DAST, Astra writes the next step only when the extracted log contains a finding or the step failed. Astra does not invent a vulnerability that was not in that extract, does not invent shell commands, and does not choose the edge. |
| Jev (`langchain_typesafe.TypeSafeClassifier`, default `jev-latest` at `https://api.typesafe.ai/v1/systemone`) | Clarify (`Noul` + `Choice`), rank exactly one pipeline (`Choice`), and route the quality gate (`Choice`: pass, remediate, escalate, skip). |
| Person in the UI | Answers ecosystem questions, approves or rejects the pipeline, and signs off UAT. |
| Designated workflow, or the fixed local command list | Does the deterministic work. |

## Workflows, and when they run

| File | Starts on push | Runs when |
| --- | --- | --- |
| `.github/workflows/orchestrator.yml` | Yes | Every push. Builds Compose, opens the tunnel, waits for the UI. |
| `.github/workflows/build.yml` | No | `workflow_dispatch`, and only if Jev's approved DAG includes `build`. |
| `.github/workflows/test.yml` | No | Same, for `test`. |
| `.github/workflows/scanners.yml` | No | Same, for `scanners`. CodeQL, SonarQube Cloud, Semgrep, pip-audit, Trivy, Snyk Open Source, Gitleaks. |
| `.github/workflows/dast.yml` | No | Same, for `dast`. Smokes the Compose URL, then OWASP ZAP. StackHawk runs only when `HAWK_APP_ID` is an application UUID. |
| `.github/workflows/package.yml` | No | Same, for `package`. |
| `.github/workflows/release.yml` | No | Same, for `release`. |
| `.github/workflows/deploy.yml` | No | Same, for `deploy`. Targets are local, dev, staging, or uat. |
| `.github/workflows/uat.yml` | No | Same, for `uat`. No production job. |
| `.github/workflows/ci.yml` | No | `workflow_dispatch` only. The orchestrator does not dispatch it. |
| `.github/dependabot.yml` | No | Dependabot's own schedule. Not a pipeline stage. |

`container` has no workflow. When it is in the approved DAG it runs the local Dockerfile check.

## UI, API, graph

The browser hash `#/support` is the support agent. Any other hash, including the bare public URL, is the orchestrator session.

`POST /api/orchestrator/session` opens the thread. `GET /api/orchestrator/session` is the poll. `POST /api/orchestrator/session/answer` is the only way past a human gate. `POST /api/orchestrator/session/viewed` tells the Actions waiter that the results screen was shown. `POST /api/orchestrator/public-url` records the tunnel URL.

LangGraph nodes, in order: `checkout`, `discover_ecosystem`, `clarify_with_user` (interrupt), `plan_pipeline`, `approve_pipeline` (interrupt), `dispatch_stages`, then only the approved stage nodes, `quality_gate`, and `report`. Stage nodes that can exist are `build`, `test`, `scanners`, `dast`, `package`, `container`, `release`, `deploy`, and `uat`. A run uses the subset Jev selected and the person approved.

The checkpointer is SQLite (`langgraph-checkpoint-sqlite`), `thread_id` equal to the run id. Aurora PostgreSQL is the deferred production binding and is not connected. AWS, AgentCore, OpenSearch, ECR, ECS, and S3 are not called.

## Files

### Application API and support agent

| File | What it does | Connects to |
| --- | --- | --- |
| `app/__init__.py` | Marks the support API package. | Imported by the process. |
| `app/config.py` | Loads this repo's `.env` only. Exposes OpenAI and TypeSafe keys and the Astra and Jev model names. | Read by the support agent and by `orchestrator/llm.py`. |
| `app/main.py` | FastAPI app. Support routes at `/status` and `/ask`, and the same handlers at `/api/support/status` and `/api/support/ask`, plus orders, products, and knowledge search. Serves the built UI at `/`. Starts the orchestrator session when `ORCHESTRATOR_AUTOSTART=1`. | Mounts `app/orchestrator_api.py`. Serves `frontend/dist`. |
| `app/orchestrator_api.py` | Session HTTP API used by the React UI and by the tests. | Calls `orchestrator/session.py`. |
| `app/graph.py` | Support-agent LangGraph: classify the question, then orders, products, knowledge, or MCP. | Uses `app/tools.py`, `app/schemas.py`, and `mcp_server/`. Not the CI graph. |
| `app/schemas.py` | Pydantic models for the support agent request, response, and tool args. | Used by `app/graph.py` and `app/main.py`. |
| `app/tools.py` | Order, product, and knowledge-base lookups over the sample data. | Reads `resources/data/`. |

### Orchestrator

| File | What it does | Connects to |
| --- | --- | --- |
| `orchestrator/__init__.py` | Package marker. | — |
| `orchestrator/state.py` | `PipelineState`, stage names, attempt cap (2), hop cap, and the time budget. Human wait is not counted in the time budget. | Shared by every node. Checkpointed in SQLite. |
| `orchestrator/schemas.py` | Jev decision models and Astra models for questions, DAGs, briefs, and diagnosis. Astra models have no command field and no answer field. | Filled by `orchestrator/llm.py`. |
| `orchestrator/llm.py` | `TypeSafeJev` routes. `OpenAIAstra` writes questions, proposes DAGs, briefs a stage, advises on test and scan findings, and diagnoses a failure. Scrubs credentials before anything is stored. | Jev calls TypeSafe. Astra calls OpenAI. |
| `orchestrator/scan.py` | Reads the checkout: languages, manifests, entry points, gaps. Does not write the questions. | `discover_ecosystem` passes the facts to Astra. |
| `orchestrator/graph.py` | The supervisor graph, guardrailed routing, stage briefs, and the report. Does not add a stage that was not proposed. | Calls scan, catalog, dispatch, guardrails, and the checkpointer. |
| `orchestrator/guardrails.py` | Caps retries, refuses a retry of a stage that is not in the approved pipeline, and blocks a path past UAT. | Applied inside `quality_gate`. |
| `orchestrator/catalog.py` | Fixed argv for each stage. The model does not supply these. | Local execution. The matching workflow is the GitHub path. |
| `orchestrator/dispatch.py` | Maps an approved stage to one workflow file and runs `gh workflow run`, or runs the catalog commands locally. After a workflow finishes, it reads the job log. | Used by each stage node. DAST receives the public app URL. |
| `orchestrator/findings.py` | Reads Semgrep, pytest, Trivy, Snyk, CodeQL, Sonar, ZAP, and StackHawk lines out of that log. Marks a check for Astra only when the extract has a finding or the step failed. | Called from `dispatch.py`. Astra advice is applied in `graph.py`. |
| `orchestrator/runner.py` | Runs the fixed commands and redacts evidence. | Called from dispatch in local mode. |
| `orchestrator/local_ops.py` | Package, container check, release plan, gated deploy, UAT checklist, and the redacted secret scan. Refuses production. | Called by catalog commands and by the package, release, deploy, and UAT workflows. |
| `orchestrator/checkpoints.py` | Opens the SQLite checkpointer. | Session thread and tests. |
| `orchestrator/session.py` | One in-process session. Blocks on the human gates. Does not invent answers. | The HTTP API and the UI poll this. |
| `orchestrator/demo.py` | Test helper that drives a graph only when the caller supplies answers. The module itself submits none. | Tests import `drive`. The push workflow does not run it. |

### Support MCP and sample data

| File | What it does | Connects to |
| --- | --- | --- |
| `mcp_server/__init__.py` | Package marker. | — |
| `mcp_server/server.py` | MCP tools for refund estimates, shipping ETA, and plan benefits. | The support agent calls these through `app/graph.py`. |
| `mcp_server/client_config.json` | Local MCP client config. | Read by the support graph. |
| `resources/data/customer_support_sample.json` | Sample orders and products. | `app/tools.py`. |
| `resources/data/knowledge_base/faq.md` | FAQ article. | Knowledge search. |
| `resources/data/knowledge_base/pricing_plans.md` | Pricing article. | Knowledge search. |
| `resources/data/knowledge_base/return_policy.md` | Return-policy article. | Knowledge search. |
| `resources/data/knowledge_base/shipping_info.md` | Shipping article. | Knowledge search. |
| `resources/prompts/customer_support_agent_instructions.txt` | Support-agent instructions. | `app/graph.py`. |

### React UI

| File | What it does | Connects to |
| --- | --- | --- |
| `frontend/index.html` | HTML shell and the IBM Plex fonts. | Vite entry. |
| `frontend/src/main.jsx` | Mounts React. | `App.jsx`. |
| `frontend/src/App.jsx` | Hash router. `#/support` is the support agent. Everything else is the orchestrator session. | `Dashboard.jsx` and `SupportApp.jsx`. |
| `frontend/src/Dashboard.jsx` | Question screen, pipeline approval, UAT sign-off, and the results screen. Results list only approved stages. Test and scan tiles show status, findings, and the next step. | Polls `/api/orchestrator/session` and posts answers to the same API. |
| `frontend/src/SupportApp.jsx` | Existing support chat. | `GET /api/support/status` and `POST /api/support/ask` on the same host as the tunnel. |
| `frontend/src/App.css` | Support layout, plus the dark engineering theme for the orchestrator. | Both views. |
| `frontend/vite.config.js` | Dev server. Proxies `/api` and the support routes to port 8000. | Local `npm run dev` only. Docker serves the built files from FastAPI. |
| `frontend/package.json` | Frontend dependencies and scripts. | The Docker frontend stage. |
| `frontend/package-lock.json` | Locked npm tree. | `npm ci` in the image build. |
| `frontend/public/samples/live-run.json` | Checked-in example report from an earlier run. The session screen does not treat it as the live pipeline. | Static file. |

### Docker, tests, and repo metadata

| File | What it does | Connects to |
| --- | --- | --- |
| `Dockerfile` | Builds the React app, then a Python image with the API, orchestrator, tests, workflows, and the GitHub CLI. | `docker-compose.yml`. |
| `docker-compose.yml` | Service `orchestrator` on port 8000, SQLite volume, autostart. Local dispatch stays local unless `ORCHESTRATOR_DISPATCH=github`. | The push workflow and `docker compose up`. |
| `.dockerignore` | Keeps `.env`, git metadata, caches, and SQLite files out of the image. | Docker build. |
| `stackhawk.yml` | HawkScan config. `applicationId` and `host` come from the environment at scan time. No application id is committed. | `dast.yml`. |
| `requirements.txt` | Runtime Python dependencies, including LangGraph, LangChain, TypeSafe, and the SQLite checkpointer. | Image and workflows. |
| `requirements-dev.txt` | Pytest, httpx, ruff, and pip-audit, plus the runtime requirements. | Tests and the image, so a local `test` stage can run pytest. |
| `pytest.ini` | Pytest python path and test directory. | `pytest`. |
| `tests/conftest.py` | Turns off autostart and GitHub dispatch during tests. | Every test. |
| `tests/test_app.py` | Support API, routing, and MCP behavior. | `app/` and `mcp_server/`. |
| `tests/test_orchestrator.py` | Graph routing, SQLite resume, retry cap, Jev Choice/Noul shape, and no production edge. Uses fake Astra and Jev. | `orchestrator/`. |
| `tests/test_pipeline_rules.py` | A build+test pipeline does not dispatch package, scanners, dast, release, or deploy. Also catalog, scan, cycles, and the session waiter. | Dispatch and workflows. |
| `tests/test_session_api.py` | HTTP answers, SQLite pause and resume on the same thread, PipelineState round-trip, and an internal checkpoint on the same database file. | The session API. |
| `scripts/__init__.py` | Package marker. | — |
| `scripts/audit_gate.py` | Fails the build only for fixable pip-audit findings. | `build` locally and `scanners.yml`. |
| `scripts/wait_for_session.py` | Polls the session until the results screen was viewed, waits 10 minutes so the support tab can still call the API, or fails at 60 minutes. Never posts answers. | The push workflow. |
| `README.md` | How to run the support agent. | The support half of the app. |
| `docs/ORCHESTRATOR_RUNBOOK.md` | Presenter notes for the orchestrator. | Points at the UI session. |
| `docs/orchestrator-dashboard.png` | Earlier dashboard screenshot. | Documentation only. |
| `support_agent_graph.png` | Support-agent graph picture. | Documentation only. |
| `Agentic CI_CD Orchestrator.html` | Design note for the orchestrator. The running system is the code above, not this page. | Reference. |
| `.gitignore` | Ignores `.env`, `var/`, `frontend/dist/`, caches, and `dist/`. | Git. |
| `.env.example` | Names of the environment variables, with empty values. | Local setup. Not a secret store. |

Generated trees are not source: `frontend/dist/` (the image build), `frontend/node_modules/`, `var/` (SQLite and reports), `dist/` (local package and plan files), and `audit.json`. `.env` is gitignored and is not part of the map.
