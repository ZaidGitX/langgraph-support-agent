# 15 — Autopilot profile, webhooks, poller, workspaces (Sprint 4, Phase 10)

A push should start and finish a run without a person at the question and approval gates,
on any git host, on a checkout the orchestrator makes itself. UAT sign-off stays human.

## `orchestrator.yaml` and `orchestrator/profile.py`

`RunProfile` (pydantic, `extra="forbid"`): `platform` (a registry platform), `tier`
(`local|dev|staging|uat`, never a forbidden target), `answers.by_id`, `answers.by_topic`,
`autopilot {answer_questions, approve, min_confidence, require_policy_clean,
require_review_accept}`, `remotes {branches, webhook_secret_name, poll_interval_seconds}`.
`load(root)` returns `None` without a file and raises `ProfileError` on an invalid one (the
session then runs with autopilot off and shows the error).

Phase 12 (2026-11-04, live run 4): the profile also decides where a push goes. `config`
(`dict[str, str]`) carries registry names of kind `config` only (`AWS_REGION: us-east-1`); a
secret kind or an unknown name is a `ProfileError`. `dispatch_for(platform)` is `platform`
for `CLOUD_PLATFORMS = (jenkins, aws, gcp, azure)` and `github` otherwise; `workflow_env(profile)`
is what the push workflow exports before `docker compose up` (`ORCHESTRATOR_PROFILE_PLATFORM`,
`ORCHESTRATOR_PROFILE_TIER`, `ORCHESTRATOR_DISPATCH`, `ORCHESTRATOR_CLOUD_APPLY`, the config
names); `scripts/profile_env.py` prints the same lines with the standard library (the runner
has no PyYAML or pydantic; `tests/test_profile_env.py` keeps the two in step and a forbidden
tier exits the step). `apply_config(profile, env)` sets a config name only where the
environment is unset or empty (Compose passes every placeholder as `""`). The committed
profile says `platform: aws`, `tier: dev`, with the ECS answers (create the cluster and the
service from the generated stack when absent). The workflow layers `docker-compose.ci.yml`
(the pushed checkout at `/workspace`, `ORCHESTRATOR_REPO_ROOT`; `app/main.py` hands it to
`SessionService`) and makes `var/` writable for the image's `appuser`.

`answer_for(profile, question)`: `by_id` → `env_platform`/`env_target` defaults from the
profile → first `by_topic` keyword found in the id or prompt → `None`.
`approval(profile, view)` → `(ok, reason)`: approve on, no artifact policy error, every
`artifact_review` verdict `accept`, `rank_confidence(jev_decisions) >= min_confidence` (an
unreported confidence counts as code-guarded, which only the injected doubles produce), no
forbidden target among the stages, and, for a platform in `CLOUD_PLATFORMS` when the run
wanted a suite (`view.suite` non-empty, i.e. platform dispatch), `view.suite.status == "ok"`
(reason `suite: …` otherwise; a local-dispatch run never wants one). The session turns that `suite:` reason into one
automatic rejection with the reason as feedback (`_auto_rejected` per run), so the run plans
again instead of running a host fallback that nobody asked for (live run 6); the second miss
waits for a person.

## Session (`orchestrator/session.py`)

- `start(repo_root=None, trigger=None)`: a run that arrives while another is active is queued
  (`view.queue` lists the waiting triggers) and starts when the active run ends
  (`_start_next` from the run thread). `view.trigger` records `source`
  (`manual|autostart|webhook|poll`), `repo`, `ref`, `sha`, `received_at`.
- `_load_profile()` at every start; `view.autopilot` = `{enabled, profile, platform, tier,
  approve, answer_questions, min_confidence, config[], events[]}`. The profile's config names
  are exported (`apply_config`) even when `ORCHESTRATOR_AUTOPILOT=0` disables the answers.
- `_autopilot(payload)` runs after every interrupt is published: question gates → answers
  through `_resume_for` (same validation as the HTTP route) or a `human` event naming the
  unmatched ids; `pipeline_approval` → `{"decision": "approved", "by": "autopilot"}` when
  `profile.approval` and the policy check agree, else a `human` event with the reason;
  `uat_signoff` → always `human`. Events: `{at, gate, action answered|approved|human, detail}`.
- `graph.approve_pipeline` records `approvals.pipeline_by` (`human` or `autopilot`).

## Webhook (`app/webhooks.py`)

`POST /api/orchestrator/hooks/push` (202). `ORCHESTRATOR_WEBHOOK_SECRET` unset → 503; no
valid signature → 401. Accepted signatures: `X-Hub-Signature-256: sha256=<hmac>` (GitHub),
`X-Gitlab-Token` (equal to the secret), `X-Gitea-Signature` / `X-Gogs-Signature` (hex HMAC),
`X-Hub-Signature: sha256=…` (Bitbucket), `X-Orchestrator-Signature` (generic, used by
`scripts/git-post-receive.sample`). `ping` and non-push events are acknowledged and skipped.
`parse_push` reads the GitHub/Gitea, GitLab, Bitbucket, or generic `{clone_url, ref, sha,
repo}` shape; a deleted ref (all-zero sha) is a 400. The profile's `remotes.branches` of the
service checkout filters branches. The dashboard token does not apply: the signature is the
credential.

## Workspaces (`orchestrator/workspace.py`)

`prepare(url, ref, sha)` → `{path, url, branch, sha, action cloned|fetched}` under
`var/workspaces/<slug>` (`ORCHESTRATOR_WORKSPACES` overrides). Shallow (`--depth 50`,
`--no-tags`), detached at the pushed commit. `git_env()`: `GIT_TERMINAL_PROMPT=0`; with
`GIT_REMOTE_TOKEN` an askpass helper (`var/workspaces/.askpass.sh`, 0700) answers the
Username prompt with `GIT_REMOTE_USER` and the password prompt with the token, so the token
is never on an argv or in `.git/config`; with `SSH_KEY_PATH` a `GIT_SSH_COMMAND`. Output is
redacted before it is kept or raised.

## Poller (`orchestrator/watch.py`)

`check_once(service, url, branches, seen)`: `git ls-remote --heads`, start a run per moved
watched branch (the first poll only records, so a restart never replays the tip), persist
`seen` in `var/workspaces/.watch.json`. `RemoteWatcher` thread started by `app/main.py` when
`ORCHESTRATOR_WATCH_REMOTE` is set (interval from `ORCHESTRATOR_WATCH_INTERVAL` or the
profile).

## UI

The hero shows the trigger (`source`, branch, short sha), "Autopilot on (n gates answered,
UAT stays human)", and the number of pushes waiting; Results list every autopilot event.
Phase 11 moves these into the timeline.

## Tests

`tests/test_autopilot.py`: profile answers and validation, approval rules, a full autopilot
run that waits only at UAT and records `pipeline_by: autopilot`, an unmatched question, low
confidence, a policy error, and a queued second push. `tests/test_webhooks.py`: workspace
clone then fetch from a local bare repository, a signed GitHub push that clones and starts a
run (401 unsigned, 401 bad signature, ping, queued second push, non-push event), 503 without
a secret, branch filter, clone failure 502, a GitLab-signed body of the wrong shape, and the
poller starting only for a new commit.

## Invariant wording

Human gates stay `interrupt()`s. The ecosystem, follow-up, and approval gates may be answered
by the committed profile through the session; `uat_signoff` is answered only through the UI.
