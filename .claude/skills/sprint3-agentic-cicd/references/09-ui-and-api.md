# 09 — Session UI and API (Phase 6)

The React dashboard stays the only place a person answers, approves, and signs off. Sprint 3
adds visibility (readiness, candidates, artifacts, retrieval, episodes) and one new gate
(`follow_up_qa`). Keep the dark engineering theme and the 2 s poll.

## API (`app/orchestrator_api.py`)

| Method and path | Returns | Notes |
| --- | --- | --- |
| `GET /api/orchestrator/readiness` | `credentials.readiness(providers.detect_all())` | works before a session starts; names only |
| `GET /api/orchestrator/session` | the view (now with the new keys) | unchanged contract plus new keys |
| `POST /api/orchestrator/session/answer` | view | `gate` may be `ecosystem_qa`, `follow_up_qa`, `pipeline_approval`, `uat_signoff`; `AnswerBody` unchanged (`extra="forbid"`) |
| `GET /api/orchestrator/session/artifacts` | `{stage: [record + content ≤ 20 000 chars]}` | content read from `var/generated/<run>/<stage>/<path>` and redacted |
| `GET /api/orchestrator/session/artifacts/{stage}/{index}` | `text/plain` full content | 404 when unknown; redacted |
| `GET /api/orchestrator/episodes?limit=5` | `episodes.recall(repo_root, limit)` | — |
| `POST /api/orchestrator/session/viewed`, `POST /api/orchestrator/public-url` | unchanged | — |

`session.submit` refuses `pipeline_approval` + `approved` when any artifact has
`policy.ok == false` (`ValueError` → HTTP 400 with the reason).

## View keys the UI reads

`phase` adds `follow_up`. New keys: `candidates[]` (`id, summary, mode, environment,
stages[names], probability, chosen`), `runner_plan{stage: {runner, provider, mode}}`,
`artifacts{stage: [records]}`, `policy[]`, `readiness{}`, `retrieval{index_id, files,
chunks, retriever, evidence[]}`, `episodes[]`, `clarify_round`, `follow_up{action,
rationale}`. Stage rows (`stages[]` and `report.chosen_pipeline.stages[]`) add `runner,
provider, environment, kind, family, plan`.

## Components (split `Dashboard.jsx`)

Create `frontend/src/components/` and move the pieces out of `Dashboard.jsx`; the file
stays the phase router and the data fetcher.

| Component | Shows | Data |
| --- | --- | --- |
| `Readiness.jsx` | one card per provider: CLI present/absent, credentials present count, missing names as chips, mode `apply`/`plan`; a toggles row (dispatch, cloud apply, write artifacts, embeddings, mcp) | `GET /api/orchestrator/readiness` on mount, then `session.readiness` once a run exists |
| `Candidates.jsx` | one card per candidate: id, `mode` badge (deterministic = neutral, stochastic = accent), environment chips, stage chain with runner chips, Jev probability bar, `chosen` outline | `session.candidates`, rank decision `choice.pipeline.probabilities` |
| `ArtifactViewer.jsx` | tabs per generated stage; per artifact: path, kind badge, purpose, policy badge (`ok` / `n warnings` / `n errors`), secrets-referenced chips, collapsible `<pre>` with the content (lazy fetch of the full text) | `session.artifacts`, `GET …/artifacts/{stage}/{index}` |
| `RetrievalPanel.jsx` | "Astra read these excerpts": grouped by topic, `path:lines`; index stats line (`files`, `chunks`, `retriever`) | `session.retrieval` |
| `Episodes.jsx` | prior runs: status mark, chosen id + mode, stage chain, lesson sentence, finished time | `GET /api/orchestrator/episodes` on mount; `report.episodes` on results |
| `Chips.jsx` | `RunnerChip`, `EnvChip`, `ModeBadge`, `PolicyBadge` | props |
| `StageList` (existing) | adds runner/provider/env chips per stop and a `planned` note with the missing names | stage rows |
| `CheckBoard` (existing) | includes every stage whose `family` is `test`, `scan`, `verify`, or name `dast`; `planned` tiles render amber with the next step "Set NAME, NAME and run again" | `report.stage_evidence` |
| Jev decisions (existing) | renders kinds `follow_up`, `runner` (one line per stage), `artifact_review` | `jev_decisions` |

`toneOf()` maps `planned` → `warn` (amber). Keep `Mark` as the single status renderer.

## Phase screens

- **idle / starting**: hero + `Readiness` (so the audience sees what is configured before
  Astra speaks).
- **questions**: as today; prompts may mention missing credentials.
- **follow_up**: same form component with heading "Follow-up questions (round
  `clarify_round`)" and the Jev rationale as the note.
- **running**: "Astra is reading the checkout" → after answers: "Astra proposes, Jev ranks,
  Jev picks runners, Astra writes artifacts, policy checks them". Show `RetrievalPanel` once
  `retrieval` exists.
- **approval**: summary line, `Candidates`, `StageList` with runner chips, `ArtifactViewer`,
  policy summary. The Approve button is disabled with an explanation when any artifact has
  policy errors; Reject is always available.
- **uat**: unchanged.
- **results**: summary grid (status, Jev route, approved stages) + `CheckBoard` +
  `StageList` + `ArtifactViewer` (read-only) + `RetrievalPanel` + `Episodes` + Jev decisions
  + diagnosis. Planned tiles list the missing names.
- **error**: unchanged.

## CSS (append to `frontend/src/App.css`, reuse the variables)

```
.chip { display:inline-flex; align-items:center; gap:.35rem; padding:.1rem .5rem; border:1px solid var(--line); border-radius:999px; font-size:.75rem; color:var(--muted); background:var(--plane); }
.chip-runner { color: var(--accent-ink); border-color: var(--accent); }
.chip-env { color: var(--ink); }
.badge-mode { font-size:.7rem; letter-spacing:.04em; text-transform:uppercase; padding:.15rem .5rem; border-radius:4px; }
.badge-mode-deterministic { background: var(--plane); color: var(--muted); border:1px solid var(--line); }
.badge-mode-stochastic { background: color-mix(in srgb, var(--accent) 18%, transparent); color: var(--accent-ink); border:1px solid var(--accent); }
.readiness-grid, .candidate-grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap:.75rem; }
.readiness-card, .candidate-card { background: var(--surface); border:1px solid var(--line); border-radius:8px; padding:.75rem; }
.candidate-card.chosen { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent) inset; }
.prob-bar { height:6px; background: var(--plane); border-radius:3px; overflow:hidden; }
.prob-bar > span { display:block; height:100%; background: var(--accent); }
.artifact-tabs { display:flex; gap:.5rem; flex-wrap:wrap; margin-bottom:.5rem; }
.artifact { border:1px solid var(--line); border-radius:8px; padding:.6rem .75rem; margin-bottom:.5rem; background: var(--surface); }
.artifact pre { background: var(--code-bg); max-height: 320px; overflow:auto; }
.policy-badge { font-size:.72rem; padding:.1rem .45rem; border-radius:4px; }
.policy-badge.ok { color: var(--ok); border:1px solid var(--ok); }
.policy-badge.warn { color: var(--amber); border:1px solid var(--amber); }
.policy-badge.error { color: var(--fail); border:1px solid var(--fail); }
.retrieval-list li, .episode-list li { border-bottom:1px solid var(--line); padding:.35rem 0; }
```

## Build and run

- `cd frontend && npm run build` must pass (the Docker image runs it). `npm run dev` proxies
  `/api` to `127.0.0.1:8000` (vite.config.js is unchanged).
- Keep `document.title = "CI/CD orchestrator"`.
- No new npm dependencies. Plain React 19 and CSS are enough.

## Manual walk-through (Phase 6 acceptance)

1. Start the API with `ORCHESTRATOR_AUTOSTART=0`, open the UI: Readiness renders with
   `aws: cli present, plan` and `local: apply`.
2. Start a session (the idle screen posts automatically): questions appear; answer with
   `env_platform=docker`; a follow-up round may appear; approval shows two or more
   candidates, one `stochastic`, with runner chips and at least one generated artifact whose
   policy badge is `ok`.
3. Approve; results show the generated stage tile (pass for a local echo or a real scan),
   any `planned` tiles in amber, retrieval evidence, and the episode.
4. Reload the page on results: the view is identical (the poll reads server state).
