# 02 — Graph and session changes (Phases 2, 3, 5)

The supervisor stays one `StateGraph(PipelineState)` compiled once with a checkpointer.
LangGraph facts this design relies on (true for langgraph 1.2.10, used in Sprint 2, and for
1.2.18, the pin since the Snyk fix of 2026-10-29):
`Send()` may target the same node several times in one super-step (map-reduce), a node reached
by `Send` may call `interrupt()` (the `uat` node does), node names are fixed at compile time,
and `Command(resume=...)` resumes the pending interrupt on the same `thread_id`.

## Node order (Sprint 3)

```
START → checkout → discover_ecosystem ─┬─(ask)─→ clarify_with_user ─┐
                                       └─(skip)────────────────────┴→ follow_up ─┬─(ask_more)─→ clarify_with_user (round 2)
                                                                                   └─(proceed)──→ plan_pipeline
plan_pipeline ─→ generate_artifacts ─→ approve_pipeline ─┬─(approved)→ dispatch_stages
                                                          ├─(rejected, plan attempts left)→ plan_pipeline
                                                          └─(else)→ report
dispatch_stages ─Send→ {build,test,scanners,dast,package,container,release,deploy,uat} | generated_stage ─→ quality_gate
quality_gate ─→ dispatch_stages | plan_pipeline | report → END
```

New nodes: `follow_up`, `generate_artifacts`, `generated_stage`. New gate: `follow_up_qa`.
`test_no_edge_past_uat` still holds: the only edge out of `uat` is to `quality_gate`.

## Node contracts

### `checkout`
Adds, beside the Sprint 2 profile:
- `readiness = credentials.readiness(providers.detect_all())` → state `readiness` (names and
  booleans only; see `07-credentials-env-mcp.md`).
- `retrieval.build_index(root)` → state `retrieval = {"index_id", "files": n, "chunks": n,
  "retriever": "bm25" | "bm25+openai"}`. Cached under `var/retrieval/<tree-hash>.json`.
- `env_state` gains `platform`, `cloud_apply` (from `ORCHESTRATOR_CLOUD_APPLY == "1"`).

### `discover_ecosystem`
- `ecosystem = scan.discover(root)` as today, plus `ecosystem["retrieved"] =
  retrieval.context_for(root, topics=["build", "test", "deploy", "docker", "env", "workflows", "secrets"])`
  (per topic: up to 4 excerpts `{path, lines, text ≤ 600 chars}`), and
  `ecosystem["episodes"] = episodes.recall(root, limit=3)`.
- `questions_from_astra(planner, ecosystem, answers, readiness)`: the payload now carries
  `retrieved`, `episodes`, `readiness`, and `stage_menu` (registry names + summaries). The
  prompt asks Astra to use the canonical ids `env_target`, `env_platform`, `runner_preference`,
  `test_depth`, `scan_policy`, `release_policy` when those topics are open, so `target_of`
  and `platform_of` keep working.
- Jev `clarify` unchanged.

### `clarify_with_user`
Unchanged, plus returns `{"clarify_rounds": 1}`. Gate id is `ecosystem_qa` on round 1 and
`follow_up_qa` on later rounds (the node reads `state["clarify_rounds"]`).

### `follow_up` (new)
```python
def follow_up(state):
    rounds = int(state.get("clarify_rounds") or 0)
    payload = {"answers": state["answers"], "retrieved": ecosystem["retrieved"],
               "readiness": state["readiness"], "rounds": rounds, "max_rounds": MAX_CLARIFY_ROUNDS}
    decision = router.follow_up(payload)                      # Jev: Noul + Choice
    if decision.action == "ask_more" and rounds < MAX_CLARIFY_ROUNDS:
        proposed = planner.follow_up(payload)                  # Astra: ≤3 questions, ids not yet answered
        questions = [... same cleaning as questions_from_astra ...]
        if questions:
            return {"open_questions": questions, "follow_up_decision": decision.model_dump(),
                    "jev_decisions": [jev_event(router, "follow_up", "FollowUpDecision", decision)],
                    "trace": ["follow_up"], "hops": 1}
    return {"open_questions": [], "follow_up_decision": decision.model_dump(), ...}
```
Routing: `route_after_follow_up` → `clarify_with_user` when `open_questions` is non-empty and
`clarify_rounds < MAX_CLARIFY_ROUNDS`, else `plan_pipeline`. The guard in code wins over the
Jev choice (a Jev `ask_more` at the cap proceeds).

### `plan_pipeline`
- Payload to `planner.propose` adds `retrieved`, `episodes`, `readiness`, `stage_menu`,
  `platform`, `runners_available` (readiness-filtered), `temperature` is not in the payload
  (it is an `OpenAIAstra` setting).
- Validate names with `registry.validate_stage_name` (drop invalid stages, record them in
  `pipeline["dropped_stages"]`).
- `router.rank(rank_payload)` where each candidate carries `mode`, `environment`, and a
  `readiness_note` ("runs now" | "plan only: missing AWS_ACCESS_KEY_ID, …").
- `router.runner({"stages": [...], "runners_available": ..., "platform": ...})` → `runner_plan`;
  code enforces `uat → local`, fixed stages keep `github_actions` when
  `ORCHESTRATOR_DISPATCH=github` and a workflow exists, else `local`.
- `pipeline` dict adds `mode`, `environment`, `runner_plan`, `dropped_stages`.
- Fixed stages: `commands_for` as today. Generated stages: `commands = []`.
- If Jev picks an id that does not exist → escalate as today.

### `generate_artifacts` (new)
Runs once per plan (skips when the pipeline has no generated stage).
```python
for stage in pipeline["stages"]:
    if stage["kind"] != "generated": continue
    context = generator.context_for(state, stage)            # repo, retrieved, answers, readiness, template skeleton, placeholders
    artifact_set = planner.generate_artifacts(context)        # Astra, structured
    result = policy.check_artifact_set(artifact_set, stage)   # code
    review = router.artifact_review({"stage": stage["name"], "policy": result.model_dump(), "revisions_left": ...})
    if review.verdict == "revise" and revisions_left:
        artifact_set = planner.generate_artifacts({**context, "policy_findings": result.findings})
        result = policy.check_artifact_set(artifact_set, stage); revisions_left -= 1
    if review.verdict == "reject" or not result.ok:
        drop the stage (never uat), record policy findings, note in trace; continue
    records = generator.write(run_id, stage, artifact_set)    # var/generated/<run>/<stage>/
    stage["commands"] = providers.for_runner(stage["runner"]).commands_for(stage, records)
    updates: generated_artifacts[stage] = records; policy += findings; artifact_review[stage] = {...}
```
A dropped stage is removed from `pipeline["stages"]` and its dependants re-point to its
dependencies (reuse `normalize_stages`). If dropping leaves only `uat`, the gate escalates
with reason `"no runnable stage after artifact review"`.

### `approve_pipeline`
Interrupt payload:
```json
{"gate": "pipeline_approval",
 "pipeline": pipeline_view(pipeline),
 "candidates": [{"id", "summary", "mode", "environment", "stages": [names], "probability"}],
 "runner_plan": {...},
 "artifacts": {"<stage>": [{"path", "kind", "purpose", "secrets_referenced", "policy", "content": "<= 20000 chars"}]},
 "policy": [...],
 "readiness": {...},
 "retrieval_evidence": [{"path", "lines", "topic"}]}
```
`pipeline_view` adds `runner`, `provider`, `environment`, `kind`, `family` per stage.

### `dispatch_stages` / `route_dispatch`
Unchanged except the `Send` target:
```python
target = name if name in STAGE_NAMES else "generated_stage"
sends.append(Send(target, {"dispatched_stage": name, "spec": stage, "commands": stage["commands"],
                           "artifacts": state["generated_artifacts"].get(name, []), "attempt_number": ...,
                           "plan_id": ..., "repo": ..., "answers": ..., "readiness": ..., "purpose": ...}))
```

### `generated_stage` (new)
Same body as `make_runner` with `stage_name = state["dispatched_stage"]`, except execution:
`ran = dispatch.execute_stage_v2(spec, commands, cwd, answers, readiness, artifacts)`.
`ran["status"]` may be `planned`; `ran["plan"]` (dict) is kept on the result row. The result
row adds `runner`, `provider`, `environment`, `kind`, `plan`. Briefs, advice, and redaction as
today. `generated_stage` never handles `uat`.

### `quality_gate`
Payload adds `planned: planned_stages(state)`. `guardrails.apply_guardrails` treats
`planned` as not failed. Jev criteria for `pass` mention planned stages (`04-jev-routing.md`).

### `report`
- `episodes.record(state, status)` before rendering.
- `machine_report` adds: `readiness`, `candidates` (with `mode`, `probability`), `runner_plan`,
  `generated_artifacts` (records, no content), `policy`, `retrieval` (index stats + evidence
  list), `episodes` (recalled), `planned_stages`.
- `render_report` adds sections `Readiness`, `Runner plan`, `Generated artifacts`, `Policy`,
  `Retrieval`, `Episodes`.
- Optional: `planner.write_docs(...)` → `var/generated/<run>/docs/deploy-<platform>-<tier>.md`.

## Session view additions (`orchestrator/session.py`)

`_empty_view()` adds: `candidates: []`, `artifacts: {}`, `policy: []`, `readiness: {}`,
`runner_plan: {}`, `retrieval: {}`, `episodes: []`, `clarify_round: 0`, `follow_up: {}`.
Phase map adds `follow_up_qa → "follow_up"`. `submit()` accepts the `follow_up` phase with the
same validation as `questions` (`expected = "follow_up_qa"`). `_publish_interrupt` copies the
new payload keys into the view. `_stage_board` rows add `runner, provider, environment, kind,
plan`.

## Budget

In phase 5 raise `MAX_HOPS` to 128 and, in the same change, `recursion_limit` from 120 to
160 in `session.py`, `demo.py`, and the tests' `_thread()` helper. The hop budget must stay
below the recursion limit, otherwise LangGraph raises `GraphRecursionError` before the
guardrail can escalate. A session loop may resume up to 12 interrupts (unchanged, enough
for 2 question rounds + approval + uat + retries).
