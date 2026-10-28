# 00 — Baseline architecture (Sprint 2, as shipped)

Read this before changing anything. It is a digest of `docs/SYSTEM_MAP.md`,
`docs/ORCHESTRATOR_RUNBOOK.md`, and the code as of commit `7ad46f7`.

## One push, under the hood

1. Push → `.github/workflows/orchestrator.yml` (the only `push:` workflow). It runs
   `docker compose up -d --build --wait`, opens a `cloudflared` quick tunnel, registers the URL
   on `POST /api/orchestrator/public-url`, then `scripts/wait_for_session.py` polls until the
   results screen was viewed (10 min grace, 60 min timeout). It never submits answers.
2. `app/main.py` lifespan creates one `SessionService` and starts it when
   `ORCHESTRATOR_AUTOSTART=1`. The React UI polls `GET /api/orchestrator/session` every 2 s.
3. Graph nodes, in order: `checkout` → `discover_ecosystem` → (`clarify_with_user`) →
   `plan_pipeline` → `approve_pipeline` → `dispatch_stages` → approved stage nodes →
   `quality_gate` → (`dispatch_stages` | `plan_pipeline` | `report`).
4. Stage nodes exist for exactly `STAGE_NAMES = build, test, scanners, dast, package, container,
   release, deploy, uat`. `dispatch_stages` fans out with `Send(name, {...})` to the ready ones.
5. On GitHub (`ORCHESTRATOR_DISPATCH=github`) a stage runs `gh workflow run <file> --ref
   <branch>`, finds the run, watches it, reads job steps and the log. Locally it runs the fixed
   argv list from `catalog.commands_for`.
6. `findings.py` extracts tool lines for semgrep, pytest, trivy, snyk, codeql, sonar, zap,
   stackhawk and marks `needs_astra` only when a finding exists or the step failed. Astra then
   writes `next_step` through `apply_astra_advice`.
7. `quality_gate` is a Jev `Choice` (`pass|skip|remediate|escalate`) wrapped by
   `guardrails.apply_guardrails` (budget, blocked, retry cap 2, no retry of unapproved stage,
   no escalate when nothing failed, no pass when something failed).
8. `report` writes the text report; `session._publish_results` builds the JSON the UI reads
   (`visible_report` hides stages that were not approved).

## Who decides what

| Piece | Role | Binding |
| --- | --- | --- |
| Astra | questions, candidate DAGs, stage brief, check advice, diagnosis | `OpenAIAstra` → `ChatOpenAI(model=OPENAI_MODEL, use_responses_api=True).with_structured_output(schema)` via `invoke_structured` (2 tries) |
| Jev | clarify (`Noul needs_human` + `Choice action`), rank (`Choice pipeline`), gate (`Choice route`) | `TypeSafeJev` → `TypeSafeClassifier(model=TYPESAFE_MODEL).invoke({"state":…, "questions":{…}})`; `last_call` is stamped into `jev_decisions` |
| Person | answers, approves pipeline, signs UAT | `POST /api/orchestrator/session/answer` |
| Workflows / catalog | deterministic work | `dispatch.py`, `catalog.py`, `local_ops.py`, `runner.py` |

## PipelineState (orchestrator/state.py)

```
run_id, repo{root, languages, build_tools, dependency_signals}, ecosystem, open_questions,
answers (merge), pipeline, stage_results (append), env_state (merge), approvals (merge),
attempt (merge), trace (append), jev_decisions (append), candidates, clarify_decision, gate,
report, status, hops (add), started_at, ready_stages, diagnosis, stop_at, held_seconds (add)
```

Constants: `MAX_ATTEMPTS=2`, `MAX_PLAN_ATTEMPTS=2`, `MAX_HOPS=96`, `MAX_SECONDS=3000`,
`ALLOWED_TARGETS=(local, dev, staging, uat)`, `FORBIDDEN_TARGETS=(production, prod)`.
Helpers: `latest_results`, `failed_stages`, `blocked_stages`, `pending_stages`,
`budget_reason`, `target_of(answers)` (reads `answers["env_target"]`).

Stage result row shape (one per attempt, appended):
`{stage, status, exit_code, attempt, plan_id, blocked, evidence, commands[], checks[], brief, watch_for, signoff?}`
where `checks[]` items are `{name, status, job, tool?, findings?, next_step?, needs_astra?, astra?}`.

## Schemas (orchestrator/schemas.py)

`StageName` Literal of the nine names; `EcosystemQuestion{id,prompt,reason}`; `QuestionSet`
(≤6); `StageBrief{summary,watch_for}`; `ClarifyDecision{action: skip|ask, rationale}`;
`StageSpec{name, depends_on, purpose}`; `CandidatePipeline{id, summary, stages≥1}`;
`PlanProposal{candidates 2..3}`; `RankDecision{chosen_id, ordered_ids, drop_stages, rationale}`;
`GateDecision{route, retry, retry_stages, replan, rationale}`; `Diagnosis`; `CheckAdvice`,
`CheckAdviceSet`.

## Session view (orchestrator/session.py `_empty_view`)

`run_id, phase (idle|starting|running|questions|approval|uat|results|error), status, questions,
pipeline, stages[], report, error, gate, has_answers, jev_decisions, quality_gate,
results_viewed, public_url`. Interrupt payloads map `gate` → phase:
`ecosystem_qa→questions`, `pipeline_approval→approval`, `uat_signoff→uat`.
`submit(body)` validates per phase (`answers` dict for questions; `decision` for the others).

## Files you will touch most

| File | Lines | Notes |
| --- | --- | --- |
| `orchestrator/graph.py` | 862 | `build_graph(checkpointer, *, jev, astra, commands)`; `normalize_stages`; `route_dispatch`; `machine_report`; `render_report` |
| `orchestrator/llm.py` | 388 | prompts (`PLAN_SYSTEM`, `QUESTION_SYSTEM`, `BRIEF_SYSTEM`, `DIAGNOSE_SYSTEM`, `ADVISE_SYSTEM`), `scrub`, `TypeSafeJev`, `OpenAIAstra` |
| `orchestrator/dispatch.py` | 341 | `STAGE_WORKFLOWS`, `run_workflow`, `execute_stage`, checks from jobs/commands |
| `orchestrator/catalog.py` | 166 | fixed argv per stage; `_scanner()` records skip when a binary is absent |
| `orchestrator/session.py` | 319 | thread + condition variable; `_publish_interrupt`, `_publish_results` |
| `orchestrator/findings.py` | 245 | extractors by tool key |
| `frontend/src/Dashboard.jsx` | 515 | phases, `CheckBoard`, `StageList` |
| `frontend/src/App.css` | 1032 | theme vars `--bg --surface --plane --ink --muted --line --accent --accent-ink --ok --fail --amber --code-bg` |

## Tests that pin behavior (do not break; extend)

- `tests/test_orchestrator.py`: `FakeAstra`/`FakeJev` doubles; sqlite resume; retry cap;
  `test_no_edge_past_uat` (checks graph edges from `uat` go only to `quality_gate` and the
  `GateDecision.route` enum has no `production`); TypeSafe Noul/Choice shapes; no ChatOpenAI
  fallback.
- `tests/test_pipeline_rules.py`: `test_catalog_commands_are_fixed` (build labels
  `compileall, ruff, pip-audit, secrets`), `test_only_the_session_workflow_runs_on_push`
  (every `.github/workflows/*.yml` except `orchestrator.yml` is `workflow_dispatch` only and has
  no `\n  push:` or `pull_request`), scanners workflow pins, `test_demo_module_does_not_script_answers`
  (the string `env_target` must not appear in `orchestrator/demo.py`).
- `tests/test_session_api.py`: HTTP gate validation, short pipeline does not dispatch
  undesignated stages, sqlite pause/resume.
- `tests/test_app.py`: support agent; untouched by this sprint.

## Environment facts

- `.env` is loaded by `app/config.py` (`setdefault`, this repo only). Keys today:
  `OPENAI_API_KEY, OPENAI_MODEL, TYPESAFE_API_KEY, TYPESAFE_MODEL, SONAR_TOKEN,
  SONAR_ORGANIZATION, SONAR_PROJECT_KEY, HAWK_API_KEY, HAWK_APP_ID, SNYK_TOKEN`.
- `docker-compose.yml` passes `OPENAI_*`, `TYPESAFE_*`, `GITHUB_*`, `ORCHESTRATOR_DISPATCH`,
  `ORCHESTRATOR_AUTOSTART`, `ORCHESTRATOR_DB=/app/var/orchestrator.sqlite`.
- The Docker image copies `.github` so the container can read workflow files; `gh` is installed
  in the image.
- The frontend is built in the image (`frontend/dist`) and served by FastAPI at `/`.
