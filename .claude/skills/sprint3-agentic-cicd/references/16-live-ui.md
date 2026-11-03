# 16 — Live dashboard (Sprint 4, Phase 11)

Before this phase the view changed only at interrupts and at the end. Now the session streams
the graph node by node, publishes after every node, and the dashboard shows the run as it
happens.

## Session (`orchestrator/session.py`)

- `_run` uses `graph.stream(pending, thread, stream_mode="updates")`; each `{node: update}`
  chunk goes to `_on_node`, which emits events and calls `_publish_progress(values)` with the
  checkpointed state. The runner's `note()` printer is captured for the run
  (`set_printer(self._note)`), so the argv a stage runs becomes a `note` event, redacted.
- View keys: `seq` (bumped on every publish), `events[]` (max 500, each
  `{seq, at, t (seconds since start), kind, name, detail, …}`), `running_stages[]`,
  `timeline[]` (`{node, at, t}`), `started_at`.
- Event kinds: `node` (a non-stage node finished), `wave` (dispatch: `stages[]`), `stage`
  (`status`, `runner`), `jev` (`decision`), `plan` (chosen id), `artifacts`, `gate`
  (`phase`, `questions[]`), `autopilot` (`action`), `note`, `report`, `done`.
- `_stage_board(values, running)` marks a dispatched stage without a result as `running`.
  `_publish_interrupt` / `_publish_results` clear `running_stages` and bump `seq`.
- `events_since(since, limit)` and `view_seq()` back the API.

## API

| Route | Returns |
| --- | --- |
| `GET /session/events?since=N&limit=M` | `{seq, phase, run_id, events[], more}` with `seq > N`, oldest first |
| `GET /session/stream?max=K` | `text/event-stream`: `retry: 2000`, then one `event: change` frame (`{seq, run_id, phase, status, gate, running_stages, trace_length}`) per published view, a `: ping` comment every 15 s; `max` ends the stream after K frames (tests) |

The stream is read-only, so the dashboard token does not apply.

## Frontend

- `useSession.js`: initial fetch; `EventSource("/api/orchestrator/session/stream")`; on
  `change` refetch the view and `GET /session/events?since=<last seq>`; events reset when the
  run id changes; polling every 2 s while the stream is down (`live` = `sse` | `polling`),
  every 10 s as a safety net while it is up.
- `Dashboard.jsx`: header (hero, `Stepper`, trigger, autopilot, queue, live indicator,
  Connections button) → `TokenPrompt` → `Toasts` → the **action card** for the current gate
  (questions form, approval with the policy badge and the disabled-on-error Approve, UAT
  sign-off, run again on results or error) → tabs with badges (running count, candidates,
  artifacts, policy findings, events) → the tab panel.
  - Overview: idle → Connections + Episodes; approval → Candidates, Stages, ArtifactViewer,
    PlatformPlan, Policy; running/uat → `RunBoard`; results → summary grid (with "Approved
    by"), `RunBoard` as it ran, CheckBoard, diagnosis.
  - Plan: Candidates, StageList, PlatformPlan, follow-up decision.
  - Artifacts: ArtifactViewer (read-only outside approval), PolicySummary.
  - Evidence: CheckBoard (results), RetrievalPanel, Episodes, Jev decisions.
  - History: `Timeline` (filters All / Stages / Jev / Gates / Commands), autopilot events, trace.
- `Stepper.jsx` (`stationFor(session)`), `RunBoard.jsx` (waves from `wave` events, depth for
  undispatched stages, spinner + elapsed from the wave and stage event timestamps, detail
  drawer), `Timeline.jsx`, `Toasts.jsx`. Styles appended to `App.css` (stepper, tabs, run
  board, spinner, timeline, toasts, live dot, action card, mobile breakpoints).

## Tests and checks

`tests/test_live.py`: with a 0.9 s `build` command and the autopilot profile, the view shows
`build` as running with a `wave` event, then the full set of event kinds, timeline nodes in
order, `events_since` paging; the events endpoint (422 on `since=-1`), paging with `more`,
and one SSE frame before and after the run reaches UAT. The SSR smoke renders the dashboard
from captured running, questions, approval, UAT, and results views. `npm run build` is part
of the gate.
