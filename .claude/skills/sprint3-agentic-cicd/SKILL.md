---
name: sprint3-agentic-cicd
description: >
  Single source of truth for Sprint 3 of the langgraph_support_app agentic CI/CD orchestrator
  (Astra, Jev, LangGraph). Use it for ANY orchestrator work: new stage names or families,
  stochastic or episodic DAG proposals, follow-up questions, Astra-generated workflows, scripts,
  Dockerfiles, env manifests and deploy docs, RAG over the checkout, Jev routing edges, providers
  and runners (local, Docker, GitHub Actions, Jenkins, AWS CodeBuild/ECS, GCP Cloud Build/Run,
  Azure Pipelines, SSH, Kubernetes), scanner catalogs, credential placeholders and readiness,
  policy guardrails, the session dashboard and API, tests, docs, and the live demo. Trigger it
  whenever the user mentions sprint 3, Astra, Jev, DAGs, stages, candidates, providers, runners,
  scanners, deploy targets, cloud environments, or the orchestrator UI, or touches orchestrator/,
  app/orchestrator_api.py, frontend/src/Dashboard.jsx, .github/workflows, or docker-compose.yml,
  even without saying "sprint 3".
argument-hint: "[phase or component, e.g. phase-1, providers, retrieval, ui, demo]"
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*), Bash(bash ${CLAUDE_SKILL_DIR}/scripts/*), Bash(/opt/anaconda3/envs/tcs_practice_env/bin/python *), Bash(git status *), Bash(git diff *), Bash(git log *)
hooks:
  PreToolUse:
    - matcher: Bash
      hooks:
        - type: command
          command: python3 "${CLAUDE_PROJECT_DIR}/.claude/skills/sprint3-agentic-cicd/hooks/guard_bash.py"
          timeout: 10
  PostToolUse:
    - matcher: Edit|Write|MultiEdit
      hooks:
        - type: command
          command: python3 "${CLAUDE_PROJECT_DIR}/.claude/skills/sprint3-agentic-cicd/hooks/post_edit_check.py"
          timeout: 30
  Stop:
    - hooks:
        - type: command
          command: python3 "${CLAUDE_PROJECT_DIR}/.claude/skills/sprint3-agentic-cicd/hooks/stop_checklist.py"
          timeout: 20
---

# Sprint 3 — adaptive, platform-agnostic agentic CI/CD

You are implementing Sprint 3 of the orchestrator in this repository. Sprint 2 shipped a
deterministic supervisor: Astra writes questions and proposes DAGs built only from nine fixed
stage names, Jev (TypeSafe `Choice`/`Noul`) picks one, and each stage runs a hardcoded GitHub
Actions workflow or a fixed local argv. Sprint 3 keeps every safety property of that system and
makes the planning **stochastic and episodic**, the stage vocabulary **open**, the artifacts
**generated**, and the execution **platform and environment agnostic**.

Treat this file as the hub. The `references/` files are the specification; the `scripts/` are
tools you run; the `assets/` are catalogs and templates you load into the code; the `hooks/`
run automatically in Claude Code and Cursor. Argument given to this skill: `$ARGUMENTS`
(a phase or component name, or empty for the full sprint).

## Definition of done (the three functional requirements)

1. **Open stage space, stochastic and episodic planning.** `orchestrator/registry.py` defines
   stage families and ~30 named stage templates; `StageSpec.name` accepts any registry name or a
   validated custom slug. Astra proposes 2–4 candidates per run: at least one `deterministic`
   candidate (fixed catalog stages only) and at least one `stochastic` candidate that contains
   generated stages. Astra can ask one round of follow-up questions after reading retrieval
   context (Jev decides `proceed|ask_more`). Astra generates the artifacts a generated stage
   needs (workflow YAML, shell script, Dockerfile, env manifest, doc) and the person sees them
   before approval. Each run is recorded as an episode and the next run recalls prior episodes.
   Astra performs retrieval (RAG) over the checkout; retrieval hits and scan findings both reach
   Astra's prompts.
2. **Platform and environment agnostic.** Every stage carries a `runner` and an `environment`.
   Providers implement one protocol (`detect`, `render`, `execute(mode=plan|apply)`) for local,
   docker, github_actions, jenkins, aws_codebuild, gcp_cloudbuild, azure_pipelines, ssh_remote,
   and kubernetes. Scanners come from a catalog, not from code. A provider whose credentials
   are missing runs in `plan` mode and records status `planned` with the names of the missing
   variables. Astra can write new Dockerfiles, build/test scripts, env manifests, and deploy
   docs for any target.
3. **Credential placeholders and cloud interfaces.** `orchestrator/credentials.py` is the single
   registry of every environment variable a provider needs (names only, never values). The
   `.env.example`, `docker-compose.yml`, `orchestrator.yml` secrets block, readiness panel, and
   redaction all derive from it. Cloud CLIs (`aws`, `gcloud`, `az`, `gh`, `docker`, `kubectl`)
   and MCP servers (`aws-mcp`, GitHub) are used through adapters with a plan/apply split.

The sprint is demo-ready when `references/10-testing-and-demo.md` acceptance checklist is green.

## Invariants you must keep (they are tested)

- No production edge. `production`/`prod` stay in `FORBIDDEN_TARGETS`; `uat` is always the last
  stage; nothing runs after UAT sign-off. Generated artifacts that mention a production target
  fail the policy check.
- Jev routes, Astra reasons. Every conditional edge is one `TypeSafeClassifier` call with
  `Choice`/`Noul` (never `Score`, never a ChatOpenAI fallback). Astra never chooses an edge.
- A stage runs only if it is in the approved pipeline. Retries cap at 2 per stage and cannot add
  a stage. The DAG stays acyclic. Human gates (`ecosystem_qa`, `follow_up_qa`,
  `pipeline_approval`, `uat_signoff`) are LangGraph `interrupt()`s answered only through the UI.
- Only `.github/workflows/orchestrator.yml` runs on push. Every other workflow, including
  generated ones, is `workflow_dispatch` only and lives directly in `.github/workflows/`
  (GitHub ignores subfolders).
- Secrets by reference only. Generated files reference `${{ secrets.NAME }}`, `$NAME`, or
  `${NAME}`. No value is ever written to a file, a log, state, or a prompt. `scrub()` and
  `redact()` cover every name in the credential registry.
- Generated commands pass `orchestrator/policy.py` before they are shown for approval and again
  before execution. Cloud `apply` operations need `ORCHESTRATOR_CLOUD_APPLY=1`; otherwise the
  provider runs `plan`.
- Tests never call the network. Use the fake Astra/Jev doubles and `plan` mode.

## Start here (every session)

```bash
python3 .claude/skills/sprint3-agentic-cicd/scripts/preflight.py --brief
python3 .claude/skills/sprint3-agentic-cicd/scripts/sprint3_progress.py
```

The project interpreter is `/opt/anaconda3/envs/tcs_practice_env/bin/python` (Python 3.14,
LangGraph 1.2.10, langchain-core 1.6.9, langchain-typesafe 0.0.1a3, PyYAML, sqlite-vec,
numpy, tiktoken, pytest 9, ruff 0.16). Use it for every `pytest`, `ruff`, and `python -m`
call; the system `python3` lacks the packages. Run the full check after each phase:

```bash
bash .claude/skills/sprint3-agentic-cicd/scripts/run_sprint3_checks.sh
```

Read in this order, then only the reference that matches your task:
`references/00-baseline-architecture.md` (what exists and why), `references/12-phase-plan.md`
(where the sprint stands and what to do next), then the component references below.

## Architecture delta at a glance

| Area | Sprint 2 | Sprint 3 | Spec |
| --- | --- | --- | --- |
| Stage vocabulary | 9 literal names | families + ~30 templates + custom slugs | `01-state-schema-and-registry.md` |
| State | fixed fields | + retrieval, episodes, readiness, runner_plan, generated_artifacts, policy, clarify_rounds | `01-…` |
| Graph | linear plan → approve → fixed nodes | + `follow_up`, `generate_artifacts`, generic `generated_stage` node via `Send`, episodes recorded at report | `02-graph-and-session.md` |
| Astra | questions, DAGs, briefs, advice, diagnosis | + follow-ups, v2 proposals with temperature, artifact generation, deploy docs | `03-astra-prompts-and-generation.md` |
| Jev | clarify, rank, gate | + follow_up, runner (per stage), artifact_review; gate learns `planned` | `04-jev-routing.md` |
| Retrieval | file scan only | BM25 + optional embeddings over chunks; episodic SQLite memory | `05-retrieval-and-episodes.md` |
| Execution | `gh workflow run` or fixed argv | provider protocol + runner matrix, plan/apply, scanner catalog, generic findings | `06-providers-runners.md` |
| Credentials | 10 env names | registry-driven placeholders, readiness, redaction, MCP manifest | `07-credentials-env-mcp.md` |
| Safety | guardrails.py | + policy.py for generated commands/artifacts, artifact review gate | `08-policy-guardrails.md` |
| UI/API | questions, approval, results | + readiness, candidates compare, artifact viewer, follow-ups, retrieval evidence, episodes, runner chips, planned tiles | `09-ui-and-api.md` |
| Verification | 55 tests | + registry/policy/retrieval/providers/generator/episodes/follow-up tests, demo script | `10-testing-and-demo.md` |

## Phase plan (work in this order, one commit per phase)

| Phase | Deliverable | Done when |
| --- | --- | --- |
| 0 | Preflight, baseline green, references read | `preflight.py` shows the env python and 55 tests pass |
| 1 | `registry.py`, `StageSpec` v2, new state fields, `normalize_stages` accepts registry/custom names | existing tests still pass; `tests/test_registry.py` passes |
| 2 | `retrieval.py` (deterministic BM25, optional embeddings) + `episodes.py`; wired into `discover_ecosystem` and `plan_pipeline` prompts | `tests/test_retrieval.py`, `tests/test_episodes.py` pass; report shows retrieval evidence |
| 3 | `credentials.py` registry + readiness in `checkout`; `redaction.py` shared by `llm.scrub` and `runner.redact`; `policy.py` | `tests/test_policy.py`, `tests/test_credentials.py` pass; `.env.example` regenerated from the registry |
| 4 | `providers/` package (base protocol, local, docker, github_actions, jenkins, aws, gcp, azure, ssh, kubernetes), scanner catalog, `dispatch.execute_stage` routes by runner, `findings` registry | `tests/test_providers.py` passes in plan mode with no credentials |
| 5 | Astra v2 methods, Jev new edges, graph nodes `follow_up`, `generate_artifacts`, `generated_stage`; approval payload carries artifacts + policy; session view fields | `tests/test_orchestrator.py` new cases pass (generated stage runs an echo script; follow-up round; no prod) |
| 6 | UI components, API endpoints, compose/env/docs updates | `npm run build` passes; manual UI walk-through per `09-ui-and-api.md` |
| 7 | Demo rehearsal: local run end to end, cloud stages in plan mode, second run recalls the episode | `10-testing-and-demo.md` checklist green; `docs/SYSTEM_MAP.md` and `ORCHESTRATOR_RUNBOOK.md` updated |

`scripts/sprint3_progress.py` infers the current phase from the files and tests present. If
the user names a phase or component in `$ARGUMENTS`, start there but verify earlier phases'
tests still pass first.

## Working rules for this codebase

- **Extend, do not fork.** Keep `STAGE_NAMES`, `catalog.commands_for`, `guardrails.py`, the
  session HTTP contract, and the fixed workflows working exactly as today. Add the new behavior
  beside them. Tests in `tests/test_pipeline_rules.py` pin the fixed behavior on purpose.
- **Schemas first.** Add or change Pydantic models in `orchestrator/schemas.py` before touching
  prompts or nodes. Astra models still have no `answer` field and no raw `argv` field for fixed
  stages; generated stages carry `GeneratedArtifact` objects that the policy checks.
- **Deterministic tests, stochastic runtime.** Temperature and sampling live only in
  `OpenAIAstra`; every fake double is deterministic. Retrieval uses BM25 by default so tests
  need no network or key.
- **Plan mode is a first-class outcome.** `planned` is a status next to `pass`, `fail`,
  `skipped`, `blocked`. It satisfies dependencies, does not fail the gate, and is rendered as an
  amber tile that lists the missing variable names.
- **Write generated files under `var/generated/<run_id>/<stage>/` first.** Materialize into the
  repo only after approval and only when `ORCHESTRATOR_WRITE_ARTIFACTS=1`. For GitHub Actions
  dispatch of a generated workflow, commit it on a branch `orchestrator/generated/<run_id>`
  and dispatch with `--ref` (see `06-providers-runners.md`).
- **Redact before store.** Any string that came from a subprocess, a provider, or a model goes
  through `redaction.redact()` before it enters state, the report, or the UI.
- **UI reuses the theme.** New panels use the CSS variables and classes in
  `frontend/src/App.css` (`--accent`, `--amber`, `--fail`, `--ok`, `.panel`, `.mark`,
  `.card-note`, `.stage-rail`). Split new panels into their own components.
- **Docs are part of the change.** Update `docs/SYSTEM_MAP.md` tables and
  `docs/ORCHESTRATOR_RUNBOOK.md` when you add a module, a node, an endpoint, or an env name.
- **Commit per phase** with a message that names the phase; never commit `.env`, `var/`, or
  `dist/`.
- **Ask the user only for decisions that change scope** (for example which cloud to demo
  live). Everything else in this skill is already decided.

## Tooling available on this machine

| Tool | State | Use |
| --- | --- | --- |
| `aws` CLI 2.38 | installed, **no credentials** (`aws sts get-caller-identity` → NoCredentials) | AWS provider in plan mode until `AWS_*` placeholders are filled; `aws login` for the user |
| `aws-mcp` MCP server | configured in Claude Code and Cursor (`uvx mcp-proxy-for-aws@latest …`) | read-only discovery: `aws___search_documentation`, `aws___list_regions`; never apply from a coding agent |
| GitHub MCP server | configured in Cursor (`ghcr.io/github/github-mcp-server`) | read repo/workflow runs while developing; `gh` CLI 2.103 is installed but its keyring token is invalid (`gh auth refresh -h github.com`) |
| `docker` 29 / compose v5 | installed; daemon socket needs the user's session | docker runner, `docker compose up --build` for the demo |
| `gcloud`, `az`, `jenkins` | **not installed** | providers must detect absence and stay in plan mode; `11-tooling-matrix.md` has install hints |
| `kubectl` | installed | kubernetes runner (plan mode unless `KUBECONFIG`) |
| `hawk` 6.6 (StackHawk CLI) | installed | DAST provider local option; needs `HAWK_API_KEY` + `HAWK_APP_ID` |
| `semgrep`, `trivy`, `gitleaks`, `snyk`, `codeql`, `zap` | not installed | scanner catalog records skip + CI alternative; the scanners workflow still runs them |
| `node` 26 / `npm` 11 | installed | `cd frontend && npm run build` |
| `uvx` 0.13 | installed | MCP proxies, optional tools (`uvx actionlint`-style one-offs are fine) |

Full matrix and install commands: `references/11-tooling-matrix.md`.

## Demo in one paragraph

Push or `docker compose up --build`; open `/#/orchestrator`. Readiness shows which CLIs and
credential names are present. Astra asks the ecosystem questions, now including platform and
runner; after answers it may ask one follow-up round. The candidates panel shows a
deterministic route and a stochastic route with generated stages, each with Jev's
probabilities. The approval gate shows every generated artifact and its policy verdict.
Approve: local and docker stages execute for real; cloud stages run in plan mode and render
amber `planned` tiles naming the missing variables; UAT waits for sign-off; the results board
lists findings, next steps, retrieval evidence, and the recorded episode. A second run shows
Astra recalling the first episode. Script: `scripts/demo_walkthrough.sh`.

## Reference index

- `references/00-baseline-architecture.md` — Sprint 2 digest: nodes, state, files, tests that pin behavior.
- `references/01-state-schema-and-registry.md` — registry, StageSpec v2, state fields, statuses.
- `references/02-graph-and-session.md` — new nodes, Send routing, interrupts, session view.
- `references/03-astra-prompts-and-generation.md` — prompts, schemas, generation and materialization.
- `references/04-jev-routing.md` — TypeSafe edges, criteria text, event records.
- `references/05-retrieval-and-episodes.md` — chunking, BM25, embeddings, episodic memory.
- `references/06-providers-runners.md` — provider protocol, runner matrix, per-provider commands.
- `references/07-credentials-env-mcp.md` — credential registry, readiness, redaction, MCP manifest.
- `references/08-policy-guardrails.md` — policy rules, artifact review, approval payload.
- `references/09-ui-and-api.md` — endpoints, panels, CSS, phases.
- `references/10-testing-and-demo.md` — test plan, fakes, acceptance checklist, demo script.
- `references/11-tooling-matrix.md` — CLIs, SDKs, MCP servers, install hints, dry-run rules.
- `references/12-phase-plan.md` — per-phase file lists and acceptance detail.
- `assets/` — `credentials.json`, `scanners.json`, `mcp_servers.json`, `templates/*.tpl`.
- `scripts/` — `preflight.py`, `sprint3_progress.py`, `policy_check.py`, `validate_workflow.py`,
  `run_sprint3_checks.sh`, `install_hooks.py`, `scaffold_provider.py`, `demo_walkthrough.sh`.
- `hooks/` — `guard_bash.py`, `post_edit_check.py`, `session_context.py`, `stop_checklist.py`,
  `prompt_context.py`, `claude-hooks.json`, `cursor-hooks.json` (install with
  `python3 scripts/install_hooks.py`).
