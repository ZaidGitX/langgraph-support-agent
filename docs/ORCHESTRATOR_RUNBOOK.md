# Agentic CI/CD orchestrator — presenter runbook

Run this from the repo root in the `tcs_practice_env` conda environment. The demo does not provision AWS and does not call AgentCore, OpenSearch, ECR, ECS, or S3.

## Command

```bash
conda activate tcs_practice_env
cd langgraph_support_app
python -m orchestrator.demo
```

Exit code 0 means the run status is `passed`. The same text is written to `var/report.md`. The Sqlite checkpoint is `var/orchestrator.sqlite`.

`OPENAI_API_KEY` is read from this repo's `.env`, the same way the support app loads it. `OPENAI_MODEL` is Astra. Jev reads `TYPESAFE_API_KEY` and calls `https://api.typesafe.ai`. `TYPESAFE_MODEL` overrides the Jev model name; the default is `jev-latest`.

The command is non-interactive. It still pauses the graph with `interrupt()` and resumes with scripted answers:

- ecosystem: target `local`, test depth `unit`, release policy stops at UAT
- pipeline approval: `approved`
- UAT sign-off: `approved`

## What you should say is happening

One LangGraph supervisor walks a freshly checked-out copy of this repo. Every node reads and writes one `PipelineState`. The checkpointer is local Sqlite, with `thread_id` equal to the run id. Aurora PostgreSQL is the production binding and is not connected.

Jev is not a keyword router and is not an OpenAI chat model. Each conditional decision is one `langchain_typesafe.TypeSafeClassifier` invocation. The classifier reads `TYPESAFE_API_KEY` from the environment and posts to `https://api.typesafe.ai/v1/systemone` with model `jev-latest` (or `TYPESAFE_MODEL`). Questions that share state go out in one request. A failed TypeSafe call is raised. The graph does not fall back to `ChatOpenAI` for routing.

The three Jev edges:

- **clarify** — `Noul` (`needs_human`: are there open questions a human must answer?) and `Choice` (`action`: `skip` or `ask`) in the same request. The Choice label is the edge.
- **rank** — `Choice` whose criteria are the candidate pipeline ids Astra proposed. Jev returns exactly one id.
- **quality gate** — `Choice` among `pass`, `remediate`, `escalate`, and `skip`.

`Score` is not used. These edges are categorical, not a spectrum. The response model name, request id, choice, confidence, and noul probability are stored on `jev_decisions` and written into `var/report.md` and `var/report.json`.

Astra (`gpt-6-astra`, whatever `OPENAI_MODEL` is) only proposes candidate stage DAGs and, if something fails, writes a diagnosis. That path is still `ChatOpenAI.with_structured_output`. The shell runs the commands. The model does not invent them.

`langchain-typesafe` 0.0.1a3 is the client (`TypeSafeClassifier`, `Noul`, `Choice`). It requires `langchain-core>=1.6.2`. This repo pins `langchain-core==1.6.9`. Docs: <https://docs.langchain.com/oss/python/integrations/providers/typesafe> and <https://docs.typesafe.ai/primitives/noul>.

## Nodes

1. **checkout** — treat this repo as the checkout. Record languages, build tools, and dependency manifests.
2. **discover_ecosystem** — find entry points and the gaps a scan cannot close (target, test depth, release policy). Retrieval is a local file scan. graphify and OpenSearch are not queried. Jev then chooses `skip` or `ask`.
3. **clarify_with_user** — `interrupt()` when Jev says `ask` and questions are open. Skipped when there is nothing to ask.
4. **plan_pipeline** — Astra proposes two or three DAGs. Jev ranks exactly one and may drop stages. UAT is put back if a ranking removes it, because the sign-off gate is mandatory.
5. **approve_pipeline** — `interrupt()` for pipeline approval.
6. **dispatch_stages** — `Send()` to the stages whose dependencies passed. Retries are capped at 2 per stage. The run stops at 48 hops or 300 seconds.
7. **build, test, package, container, release, deploy, uat** — shell commands. Build compiles, runs ruff, pip-audit through `scripts/audit_gate.py`, and a redacted secret scan. Semgrep, Trivy, and Gitleaks run here when they are on `PATH`; otherwise the stage records a skip and points at `.github/workflows/scanners.yml`. Test runs `pytest -q`. Package, container, release, deploy, and UAT call `python -m orchestrator.local_ops`. Deploy refuses `production` and `prod`. Nothing is pushed to a registry or a cluster. UAT interrupts for sign-off.
8. **quality_gate** — Jev `Choice` reads the evidence and returns `pass`, `skip`, `remediate`, or `escalate`. Code then applies the caps: no infinite retry, no escalate when nothing failed, no path past UAT.
9. **report** — the A→Z text on stdout and in `var/report.md`. The same run is `var/report.json` (`run_id`, trace, Jev decisions, candidates, chosen pipeline, stage evidence, quality gate, approvals, `past_uat: false`).

## Local versus deferred

| Piece | This demo | Later |
| --- | --- | --- |
| Graph runtime | in-process LangGraph 1.2 | Bedrock AgentCore microVM |
| Checkpointer | Sqlite (`langgraph-checkpoint-sqlite`) | Aurora PostgreSQL |
| Jev | `TypeSafeClassifier` calling `jev-latest` at `https://api.typesafe.ai` | same binding |
| Astra | `gpt-6-astra` | same binding, via `OPENAI_MODEL` |
| Scanners | ruff, pip-audit, local secret scan; Semgrep/Trivy/Gitleaks when installed | the workflows under `.github/workflows/` |
| DAST | `.github/workflows/dast.yml` curls `/status` because FastAPI is a real HTTP surface. ZAP is not installed, so it is not a stage. | a dedicated DAST runner |
| Package / image / release / deploy | plans and a local tarball in `dist/` | S3, ECR, ECS |
| Tool bus | subprocess | AgentCore Gateway MCP |

## Workflows

- `build.yml` — compileall and ruff
- `test.yml` — `pytest -q`
- `package.yml` — `python -m orchestrator.local_ops package`
- `scanners.yml` — Semgrep, pip-audit, Trivy, Gitleaks
- `dast.yml` — start uvicorn and curl `/status` and `/openapi.json`
- `release.yml`, `deploy.yml`, `uat.yml` — `workflow_dispatch` only. Deploy's environment choice is local, dev, staging, or uat. The job exits 2 if the target is production.

## GitHub Actions

`.github/workflows/orchestrator.yml` runs on `push` and `pull_request`. It installs `requirements-dev.txt` and runs `python -m orchestrator.demo` with `TYPESAFE_API_KEY` and `OPENAI_API_KEY` from repository secrets. The job uploads `var/report.md` and `var/report.json`. It does not call AWS or Bedrock AgentCore. The stage commands stay the ones in the other workflow files.

## Dashboard

The support UI stays on `#/support`. The orchestrator dashboard is `#/orchestrator`.

```bash
cd frontend
npm install
npm run dev
```

Open `http://127.0.0.1:5173/#/orchestrator`. It loads `frontend/public/samples/live-run.json`, the report from the push-triggered Actions run. **Load var/report.json** reads a local demo report while `npm run dev` is running. A file picker loads any other `report.json`. The LangSmith panel shows this run's graph trace and TypeSafe request ids. The CloudWatch panel states that AWS was not called and lists the stage evidence instead.

## Tests

```bash
pytest -q tests/test_orchestrator.py tests/test_app.py
ruff check app mcp_server orchestrator tests scripts
```

The orchestrator tests cover the stage-result append reducer, skip-clarify when nothing is open, Sqlite interrupt resume, the retry cap, and the absence of a production edge. They inject a fake classifier with the TypeSafe result shape, so they do not call TypeSafe or OpenAI. The demo command above is the live model run. Exit code 0 means status `passed`. The same command is what Actions runs.
