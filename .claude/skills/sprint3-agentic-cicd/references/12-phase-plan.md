# 12 — Phase plan with file lists and acceptance

Work one phase at a time. Each phase: read its reference, write schemas/tests first, implement,
run `scripts/run_sprint3_checks.sh`, update docs, commit. `scripts/sprint3_progress.py`
tells you which phase the checkout is in. Never skip a phase's tests to reach the demo
faster; the demo depends on them.

## Phase 0 — Preflight (≤ 10 minutes)

- Run `scripts/preflight.py --brief` and `scripts/sprint3_progress.py`.
- Run the baseline: `$PY -m pytest -q` (55 passed) and `ruff check`.
- Read `00-baseline-architecture.md`. Confirm `git status` is clean or understand what is
  dirty.
- If the `.claude/skills` hooks are not installed (`preflight` says so), tell the user to run
  `python3 .claude/skills/sprint3-agentic-cicd/scripts/install_hooks.py` once (Claude Code
  frontmatter hooks already apply while this skill is invoked).

## Phase 1 — Registry and schema (ref `01`)

Create: `orchestrator/registry.py`, `tests/test_registry.py`.
Modify: `orchestrator/schemas.py` (v2 models), `orchestrator/state.py` (constants, fields,
helpers), `orchestrator/graph.py` (`normalize_stages` v2, `TOOL_BINDINGS` fallback,
`_deps_met` accepts `planned`), `orchestrator/catalog.py` (generated → `[]`),
`docs/SYSTEM_MAP.md` (registry row).
Accept: all Sprint 2 tests pass unchanged; `test_registry.py` passes.
Interim rules that phase 5 lifts: `plan_pipeline` drops non-fixed stages
(`allow_generated=False`), `uat` is required only with `context={"require_uat": True}`,
`MAX_HOPS` stays 96.
Commit: `Sprint 3 phase 1: stage registry, StageSpec v2, state fields`.
Working in a copy of the repo? Run the gate with
`bash <skill>/scripts/run_sprint3_checks.sh --root <copy>` so `var/` and the stamp land there.

## Phase 2 — Retrieval and episodes (ref `05`, prompts in `03`)

Create: `orchestrator/retrieval.py`, `orchestrator/episodes.py`, `tests/test_retrieval.py`,
`tests/test_episodes.py`.
Modify: `orchestrator/graph.py` (`checkout` builds the index; `discover_ecosystem` adds
`retrieved` + `episodes`; `plan_pipeline` payload; `report` records the episode;
`machine_report`/`render_report` sections), `orchestrator/llm.py` (`QUESTION_SYSTEM_V3`,
`PLAN_SYSTEM_V3`, temperature plumbing), `orchestrator/scan.py` (`retrieval` note says the
index was used), `.gitignore` (`var/` already covers the cache), `docs/SYSTEM_MAP.md`.
Accept: tests pass; a fake run's report has `Retrieval` and `Episodes` sections; second
fake run on the same tmp root recalls the first.
Commit: `Sprint 3 phase 2: retrieval over the checkout and episodic memory`.

## Phase 3 — Credentials, readiness, redaction, policy (refs `07`, `08`)

Create: `orchestrator/credentials.py`, `orchestrator/credentials.json` (copy of the skill
asset), `orchestrator/redaction.py`, `orchestrator/policy.py`, `tests/test_credentials.py`,
`tests/test_policy.py`.
Modify: `orchestrator/llm.py` (`scrub` → redaction), `orchestrator/runner.py`
(`redact_secrets` → redaction), `orchestrator/local_ops.py` (patterns), `orchestrator/graph.py`
(`checkout` stores readiness), `app/orchestrator_api.py` (`GET /readiness`), `.env.example`
(regenerated), `docker-compose.yml` (pass-through block + commented socket),
`.github/workflows/orchestrator.yml` (secrets block), `README.md` (placeholders section).
Accept: tests pass; `GET /api/orchestrator/readiness` returns names only; `.env.example`
matches the registry.
Commit: `Sprint 3 phase 3: credential registry, readiness, shared redaction, policy`.

## Phase 4 — Providers and runners (ref `06`)

Create: `orchestrator/providers/{__init__,base,registry,local,docker,github_actions,jenkins,aws,gcp,azure,ssh,kubernetes}.py`,
`orchestrator/scanners.json` (copy of the skill asset), `tests/test_providers.py`.
Modify: `orchestrator/dispatch.py` (`execute_stage_v2`, generalized `run_workflow`),
`orchestrator/catalog.py` (`scanner_commands`), `orchestrator/findings.py` (catalog-driven
extractors + generic), `docs/SYSTEM_MAP.md`.
Use `scripts/scaffold_provider.py <runner>` to create each provider module and its test
skeleton; then fill in the commands from the runner matrix.
Accept: `test_providers.py` passes with no credentials; every provider plans; `local` runs
an echo script.
Commit: `Sprint 3 phase 4: provider protocol, runners, scanner catalog`.

## Phase 5 — Astra generation, Jev edges, graph nodes (refs `02`, `03`, `04`)

Create: `orchestrator/generator.py`, `tests/test_generator.py`.
Modify: `orchestrator/llm.py` (new Astra methods and prompts; new Jev methods),
`orchestrator/graph.py` (`follow_up`, `generate_artifacts`, `generated_stage`, routing,
approval payload, `pipeline_view`), `orchestrator/session.py` (view keys, `follow_up`
phase, approval refusal on policy errors), `orchestrator/guardrails.py` (`planned`),
`tests/test_orchestrator.py` (doubles + end-to-end cases), `tests/test_session_api.py`
(follow-up, artifacts, episodes endpoints), `docs/SYSTEM_MAP.md`, `docs/ORCHESTRATOR_RUNBOOK.md`.
Accept: end-to-end fake runs listed in `10-testing-and-demo.md` pass; `test_no_edge_past_uat`
still passes.
Commit: `Sprint 3 phase 5: generated stages, follow-up round, artifact review`.

## Phase 6 — UI, API, docs (ref `09`)

Create: `frontend/src/components/{Readiness,Candidates,ArtifactViewer,RetrievalPanel,Episodes,Chips}.jsx`.
Modify: `frontend/src/Dashboard.jsx`, `frontend/src/App.css`, `app/orchestrator_api.py`
(artifacts and episodes endpoints), `README.md`, `docs/*`.
Accept: `npm run build` passes; manual walk-through in `09` done against a fake-driven
session (`ORCHESTRATOR_AUTOSTART=1` with real keys, or the test doubles through a small
`scripts/dev_session.py` that injects `FakeAstra`/`FakeJev`; that helper must not contain
`env_target` and must not live in `orchestrator/demo.py`).
Commit: `Sprint 3 phase 6: readiness, candidates, artifacts, retrieval, episodes in the UI`.

## Phase 7 — Demo rehearsal (ref `10`)

- Run variant A end to end with real keys; then variant B; then C if `gh` is refreshed.
- Record one run with a planned cloud stage and one with a docker scan that executed.
- Update `frontend/public/samples/live-run.json` with a redacted copy of the new report
  (optional, documentation only).
- Fill the acceptance checklist in `10`; fix anything red.
Commit: `Sprint 3 phase 7: demo rehearsal and docs`.

## Resuming after a break

1. `git log --oneline | head`, `git status --short`.
2. `scripts/sprint3_progress.py` → current phase and failing checks.
3. `scripts/run_sprint3_checks.sh` → fix red before adding anything.
4. Continue with the next phase above.

## Decisions already made (do not re-open without the user)

- Providers call CLIs through subprocess; no cloud SDKs.
- Generated workflows live at `.github/workflows/gen-<stage>.yml` and dispatch from a
  branch `orchestrator/generated/<run_id>`.
- Retrieval is BM25 by default; embeddings are opt-in.
- Episodes live in the checkpointer's SQLite file.
- `planned` is a status, not a failure.
- Policy is regex-based and shared with the hooks.
- The demo default is local/docker runners with cloud routes in plan mode; a live cloud
  stage is a user decision because it needs their credentials.
