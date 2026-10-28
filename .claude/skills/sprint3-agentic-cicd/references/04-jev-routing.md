# 04 — Jev routing edges (Phase 5)

Jev remains `TypeSafeJev` in `orchestrator/llm.py`: each decision is one
`TypeSafeClassifier.invoke({"state": payload, "questions": {...}})` using only `Choice` and
`Noul`. A failed call raises. No `Score`. No ChatOpenAI fallback. Every call is stamped with
`_stamp(...)` and recorded through `jev_event` so the UI shows model, primitives, choice,
probabilities, and request id.

Sprint 3 adds three edges and extends two.

## New: `follow_up(payload) -> FollowUpDecision`

```python
questions = {
    "needs_more_context": Noul(
        instructions="Do the answers plus the retrieved excerpts still leave the runner, platform, scan policy, or deploy target ambiguous for this checkout?",
        criteria=NoulCriteria(
            true="A stage, runner, provider, or environment choice cannot be made without another answer.",
            false="The answers and the retrieved excerpts are enough to plan the pipeline.",
        ),
    ),
    "action": Choice(
        instructions="Choose ask_more only when a short follow-up round would change the pipeline. Otherwise choose proceed.",
        criteria={
            "proceed": "Plan now with the answers and retrieved context.",
            "ask_more": "Ask up to three follow-up questions before planning.",
        },
    ),
}
```
Rationale string pattern: `TypeSafe Choice action=… confidence=…; Noul needs_more_context=…; model=…; request_id=…`.
Code guard: `ask_more` at `clarify_rounds >= MAX_CLARIFY_ROUNDS` becomes `proceed` with
`| guardrail: clarify rounds exhausted` appended.

## New: `runner(payload) -> RunnerDecision`

One request, one `Choice` per stage, keyed `runner:<stage>`. The criteria dict is built
from readiness so Jev sees facts, not opinions:

```python
for stage in payload["stages"]:
    options = {}
    for runner in stage["runner_options"]:          # from registry.runners_for(platform) ∩ template.default_runners
        info = payload["runners_available"][runner]  # {"cli": bool, "missing": [names], "mode": "apply"|"plan"}
        options[runner] = (
            f"{runner}: cli {'present' if info['cli'] else 'absent'}; "
            f"credentials {'complete' if not info['missing'] else 'missing ' + ', '.join(info['missing'])}; "
            f"would run in {info['mode']} mode. "
            + ("Matches the platform the person chose. " if runner in payload["platform_runners"] else "")
        )
    questions[f"runner:{stage['name']}"] = Choice(
        instructions="Choose the runner for this stage. Prefer a runner that runs now over plan-only, unless the person chose that platform explicitly.",
        criteria=options,
    )
```
Parse: `runners[stage] = response.choices[f"runner:{stage}"].choice`. Code guards: `uat → local`;
fixed stages with a workflow → `github_actions` when `ORCHESTRATOR_DISPATCH=github`, else
`local`; a runner outside `RUNNERS` raises.

## New: `artifact_review(payload) -> ArtifactReview`

```python
"verdict": Choice(
    instructions="Review the policy result for the generated artifacts of one stage.",
    criteria={
        "accept": "Policy reported no errors. Warnings only, or none.",
        "revise": "Policy reported fixable errors (secret literal, unpinned action, missing permissions, missing set -euo pipefail) and a revision is still available.",
        "reject": "Policy reported a production reference, a destructive command, a path outside the allowed directories, or revisions are exhausted.",
    },
)
```
Payload: `{"stage", "policy": {"ok", "findings": [...]}, "revisions_left": int, "artifact_paths": [...]}`.
Code guards: `accept` with `ok == False` becomes `revise` (or `reject` when no revisions
left); `revise` with `revisions_left == 0` becomes `reject`.

## Extended: `rank`

Criteria text per candidate becomes:
```
"{summary} Mode: {mode}. Environment: {tier} on {platform}. Stages: {names}. Readiness: {readiness_note}. "
"Prefer a pipeline that builds, tests, and stops at uat. Prefer a runnable route over a plan-only route "
"unless the person chose that platform. There is no production stage."
```
`readiness_note` is `"runs now"` or `"plan only: missing NAME, NAME"`. Instructions add:
"A stochastic candidate is acceptable when its generated stages serve the answers."

## Extended: `gate`

`pass` criterion becomes: "Every required stage passed, was an explained skip, or is planned
because credentials for its provider are missing." Payload adds `planned`. Guardrails treat
`planned` as not failed (`08-policy-guardrails.md`).

## `jev_event` kinds and the UI

Kinds now: `clarify`, `follow_up`, `rank`, `runner`, `artifact_review`, `gate`. The
decision panel in the UI renders each with `Edge · <kind>` and the choice label; `runner`
events show one line per stage. `router_line()` joins them as today.

## Tests (`tests/test_orchestrator.py`)

Mirror the existing `_Classifier` pattern:
- `test_jev_follow_up_uses_noul_and_choice`: criteria keys `{proceed, ask_more}`; Noul key
  `needs_more_context`.
- `test_jev_runner_asks_one_choice_per_stage`: two stages → two `runner:*` questions, each
  `type == "choice"`; the parsed `runners` dict maps both; `uat` forced to `local`.
- `test_jev_artifact_review_guard`: `accept` with `ok=False` → `revise`; `revise` with zero
  revisions → `reject`.
- `test_jev_rank_criteria_mention_mode_and_readiness`.
- `FakeJev` additions: `follow_up` (ask_more on round 1 if payload has `retrieved`, else
  proceed), `runner` (first option), `artifact_review` (accept when `policy.ok`, else revise,
  else reject).
