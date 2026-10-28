# Agentic CI/CD orchestrator — presenter runbook

Run this from the repo root in the `tcs_practice_env` conda environment. The demo does not provision AWS and does not call AgentCore, OpenSearch, ECR, ECS, or S3.

## Command

Open the UI and answer there. Nothing in this repo fills in ecosystem answers, pipeline approval, or UAT sign-off.

```bash
docker compose up --build
```

Then open `http://127.0.0.1:8000/#/orchestrator`. The process stays up until you stop it. A push does this for you and prints a public tunnel URL in the Actions log.

`OPENAI_API_KEY` is read from this repo's `.env`, the same way the support app loads it. `OPENAI_MODEL` is Astra. Jev reads `TYPESAFE_API_KEY` and calls `https://api.typesafe.ai`. `TYPESAFE_MODEL` overrides the Jev model name; the default is `jev-latest`.

`python -m orchestrator.demo` does not run a session and does not submit answers.

## What you should say is happening

One LangGraph supervisor walks a freshly checked-out copy of this repo. Every node reads and writes one `PipelineState`. The checkpointer is local Sqlite, with `thread_id` equal to the run id. Aurora PostgreSQL is the production binding and is not connected.

Jev is not a keyword router and is not an OpenAI chat model. Each conditional decision is one `langchain_typesafe.TypeSafeClassifier` invocation. The classifier reads `TYPESAFE_API_KEY` from the environment and posts to `https://api.typesafe.ai/v1/systemone` with model `jev-latest` (or `TYPESAFE_MODEL`). Questions that share state go out in one request. A failed TypeSafe call is raised. The graph does not fall back to `ChatOpenAI` for routing.

The three Jev edges:

- **clarify** — `Noul` (`needs_human`: are there open questions a human must answer?) and `Choice` (`action`: `skip` or `ask`) in the same request. The Choice label is the edge.
- **rank** — `Choice` whose criteria are the candidate pipeline ids Astra proposed. Jev returns exactly one id.
- **quality gate** — `Choice` among `pass`, `remediate`, `escalate`, and `skip`.

`Score` is not used. These edges are categorical, not a spectrum. The response model name, request id, choice, confidence, and noul probability are stored on `jev_decisions` and written into `var/report.md` and `var/report.json`.

Astra (`gpt-6-astra`, whatever `OPENAI_MODEL` is) writes the ecosystem questions, proposes candidate stage DAGs, briefs a stage before it runs, and diagnoses a failure. That path is `ChatOpenAI.with_structured_output`. The designated workflow, or the fixed local commands, do the work. Astra does not invent shell commands.

`langchain-typesafe` 0.0.1a3 is the client (`TypeSafeClassifier`, `Noul`, `Choice`). It requires `langchain-core>=1.6.2`. This repo pins `langchain-core==1.6.9`. Docs: <https://docs.langchain.com/oss/python/integrations/providers/typesafe> and <https://docs.typesafe.ai/primitives/noul>.

## Nodes

1. **checkout** — treat this repo as the checkout. Record languages, build tools, and dependency manifests.
2. **discover_ecosystem** — scan the checkout, pull topic excerpts from the local BM25 index (`orchestrator/retrieval.py`, built in `checkout`), recall the newest episodes of earlier runs on this checkout (`orchestrator/episodes.py`), then Astra writes the questions with that context. graphify and OpenSearch are not queried. Jev then chooses `skip` or `ask`.
3. **clarify_with_user** — `interrupt()` when Jev says `ask` and questions are open (`ecosystem_qa`, then `follow_up_qa` for the second round). The UI is the only place those answers are submitted.
4. **follow_up** — Jev (`Noul` needs_more_context + `Choice` proceed/ask_more) decides whether one more round would change the plan; Astra writes at most three follow-up questions. Two rounds at most.
5. **plan_pipeline** — Astra proposes two to four DAGs from the stage registry: at least one deterministic candidate (fixed stages only) and, when the answers call for it, a stochastic candidate with generated stages. Jev ranks exactly one and may drop stages, then picks a runner for each generated stage from the runners readiness says could run. The graph does not add a stage that was not in that choice.
6. **generate_artifacts** — for each generated stage Astra completes the provider's skeletons (workflow, buildspec, script, Dockerfile, manifest); the code adds the env manifest (names only); the policy checks every file; Jev accepts, asks for one revision, or rejects. A rejected stage is dropped before approval.
7. **approve_pipeline** — `interrupt()` for pipeline approval. The person sees every candidate with Jev's probabilities, the runner plan, every generated artifact with its policy verdict, and the retrieval evidence. Approval is refused while any artifact has a policy error.
8. **dispatch_stages** — `Send()` to the approved stages whose dependencies passed (or were planned). Retries are capped at 2 per stage and cannot add a stage Jev did not approve.
9. **build, test, scanners, dast, package, container, release, deploy, uat** — the fixed stages, only the ones in the approved DAG. On GitHub, the stage dispatches its workflow. Locally it runs the fixed command list. Deploy refuses `production` and `prod`. UAT interrupts for sign-off. There is no production node.
10. **generated_stage** — every registry or custom stage runs here through its runner's provider: the policy checks the commands once more, a ready local or docker runner applies, and a cloud runner without credentials or without `ORCHESTRATOR_CLOUD_APPLY=1` records `planned` with the exact command and the missing names.
11. **quality_gate** — Jev `Choice` reads the evidence and returns `pass`, `skip`, `remediate`, or `escalate`; a `planned` stage is not a failure. Code then applies the caps: no infinite retry, no escalate when nothing failed, no path past UAT.
12. **report** — the A→Z text on stdout and in `var/report.md`, with the retrieval evidence and the recalled episodes. The same run is `var/report.json` (`run_id`, trace, Jev decisions, candidates, chosen pipeline, stage evidence, quality gate, approvals, retrieval, episodes, `past_uat: false`). The run is then recorded in the `episodes` table so the next run can recall it.

## Local versus deferred

| Piece | This demo | Later |
| --- | --- | --- |
| Graph runtime | in-process LangGraph 1.2 | Bedrock AgentCore microVM |
| Checkpointer | Sqlite (`langgraph-checkpoint-sqlite`) | Aurora PostgreSQL |
| Jev | `TypeSafeClassifier` calling `jev-latest` at `https://api.typesafe.ai` | same binding |
| Astra | `gpt-6-astra` | same binding, via `OPENAI_MODEL` |
| Scanners | ruff, pip-audit, local secret scan; Semgrep, Trivy, Snyk Open Source, and Gitleaks in scanners.yml | the workflows under `.github/workflows/` |
| DAST | `.github/workflows/dast.yml` smokes the Compose URL, then runs OWASP ZAP and StackHawk. It runs only when `dast` is in the approved DAG. | — |
| Package / image / release / deploy | plans and a local tarball in `dist/` | S3, ECR, ECS |
| Tool bus | subprocess | AgentCore Gateway MCP |

## Workflows

Only `orchestrator.yml` runs on push. Every other workflow is `workflow_dispatch`. After Jev's pipeline is approved, the session dispatches the workflows for those stages and no others. See `docs/SYSTEM_MAP.md`.

## GitHub Actions

`.github/workflows/orchestrator.yml` runs on `push`. It starts Docker Compose, opens a Cloudflare quick tunnel, prints the public URL, and waits up to 60 minutes for the UI session. It does not submit answers. It does not call AWS.

## Dashboard

The support UI stays on `#/support`. The orchestrator session is the default page and `#/orchestrator`.

`docker compose up` serves both at `http://127.0.0.1:8000/`. For frontend-only development:

```bash
cd frontend
npm install
npm run dev
```

The session screen asks Astra's questions and, after the run, lists only the approved stages.

## Tests

```bash
conda activate tcs_practice_env
pytest -q
ruff check app mcp_server orchestrator tests scripts
```

Tests that need a human answer post it through the session HTTP API, with a fake Astra and a fake Jev so they do not call the network. That helper is test code. CI does not use it.
