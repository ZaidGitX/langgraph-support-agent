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

Sprint 4 turned the screen into a shell that stays in place for the whole run: a stepper (Connect → Questions → Plan → Approve → Run → UAT → Results), one action card for whatever the person can do right now, and five tabs. Overview shows the live run board (tiles per dispatch wave that spin while a stage runs, then show the result and how long it took) or the results summary; Plan the candidates, stages, and platform plan; Artifacts the generated files with their policy verdicts; Evidence the test and scan tiles, retrieval excerpts, episodes, and Jev decisions; History the timeline of every event, including the commands the runner printed and every autopilot decision. The page listens to a server-sent-events feed and falls back to polling; toasts announce gate changes.

The Sprint 3 session screen walks the phases in order:

- **idle / starting** — the readiness panel (CLIs and credential names on this host, `apply` or `plan` per provider, the orchestrator toggles) and the episodes Astra will recall. Nothing here shows a value.
- **questions**, then **follow-up questions (round 2)** — Astra's questions; the follow-up form shows Jev's rationale for asking again. Every answer is typed here.
- **running** — "Astra is reading the checkout", then the retrieval panel with the excerpts Astra read.
- **approval** — the candidates (mode badge, environment, stage chain with runner chips, Jev's probability bar, the chosen outline), the stages with runner, provider, environment, and kind chips, every generated artifact with its policy badge, secrets referenced by name, and the bounded content (the full text loads on request), and the policy summary. Approve is disabled while any artifact has a policy error; Reject is always available.
- **uat** — sign-off, unchanged.
- **results** — summary (status, Jev route, approved stages, planned stages), the tests, scans, and planned tiles (a planned tile is amber and names the variables to set), the stages, the artifacts read-only, the retrieval evidence, the recorded episodes, every Jev decision (the runner edge lists one line per stage; the artifact review shows its verdict), and Astra's diagnosis.

Results list only the approved stages. Reloading on any phase shows the same screen, because the poll reads server state.

## Connections (Sprint 4)

The idle screen and the Connections button open one card per platform. Each card shows the CLI, the verified state, and which names are set and from where. Two ways to connect:

- **Device-code login.** For GitHub (`gh auth login --web`), Azure (`az login --use-device-code`), AWS (`aws login --remote --profile …`, needs `AWS_PROFILE`: open the URL, sign in as an IAM user or the root user, paste the authorization code back; Identity Center users run `aws sso login --profile …` in a terminal), and Google Cloud (`gcloud auth login --no-launch-browser`, paste the verification code back). The CLI runs on the orchestrator host; the page shows the URL and the one-time code; the vendor CLI keeps the resulting credentials in its own store. The Docker image carries all four CLIs (about 3 GB; `docker build --build-arg WITH_CLOUD_CLIS=0 .` gives the slim image, where every cloud runner stays in plan mode).
- **Vault form.** For token-style platforms (Jenkins, SSH, Kubernetes, scanner services, static AWS or GCP keys). Values are encrypted on the host (`var/vault/secrets.enc`, key `ORCHESTRATOR_VAULT_KEY` or `var/vault/.key`), exported to the running service at once, and never displayed again. Forget removes one.

Verify runs the platform's whoami command and the card turns `apply` when it passes, even when the static key names are unset. Nothing in a response, a log, a report, or a prompt ever carries a value.

Set the `ORCHESTRATOR_UI_TOKEN` repository secret before a push-triggered demo: the tunnel URL is public, and with the token set the dashboard asks for it once per tab before it accepts any change.

## Platform dispatch (Sprint 4)

Set `ORCHESTRATOR_DISPATCH=platform` and answer the platform question with `jenkins`, `aws`, `gcp`, or `azure`. The fixed stages then run on that platform instead of the host or GitHub Actions:

1. After the candidates are ranked, Astra translates the fixed workflows into the platform's own files (the suite): a `ci/pipeline.Jenkinsfile` with one stage per approved stage and `deploy/jenkins-job.xml`; one `ci/buildspec-<stage>.yml` per stage with `deploy/codebuild-project.json` and, when deploy is a stage, `deploy/ecs-stack.yaml`; one `ci/cloudbuild.yaml`; or one `ci/azure-pipelines.yml`. The policy checks every file and Jev reviews the set. A rejected suite drops the platform and the fixed stages fall back to the host.
2. The approval screen shows the suite under the **pipeline** tab of Generated artifacts and a **Platform** panel: which file runs each stage, the provisioning commands (upload the source zip to S3, create the ECR repository and the CodeBuild project; create the Jenkins job), and the secret names that will be injected into the platform's store (Secrets Manager, Jenkins credentials, GitHub secrets, Google Secret Manager, an Azure variable group). Names only.
3. Once approved, provisioning and injection run before the first wave: in plan mode they are recorded as `planned`; with the platform connected and `ORCHESTRATOR_CLOUD_APPLY=1` they apply, and a failed apply stops the run through the quality gate.
4. Each fixed stage then triggers its suite file: `aws codebuild start-build --buildspec-override`, Jenkins `buildWithParameters` with `STAGE=<name>`, `gcloud builds submit --substitutions _STAGE=<name>`, `az pipelines run --parameters stage=<name>`. Without credentials the stage records `planned` with the exact command. `uat` always stays on the host as the human gate.

## Autopilot and pushes (Sprint 4)

`orchestrator.yaml` at the checkout root is the run profile. With it, a push can start and finish a run without a person at the question and approval gates:

- **Questions.** Each of Astra's questions is answered from `answers.by_id`, then the platform and tier defaults, then `answers.by_topic` keywords matched against the id and prompt. One unmatched question and the gate waits for a person, as before.
- **Approval.** Autopilot approves when no artifact has a policy error, every artifact review accepted, and Jev's rank confidence is at least `autopilot.min_confidence`. Otherwise the approval gate waits for a person. The report records `approvals.pipeline_by: autopilot`.
- **UAT.** Never automatic. The run waits at `uat_signoff` until someone signs off in the dashboard.

Set `ORCHESTRATOR_AUTOPILOT=0` to ignore the profile for a run you want to walk through by hand (the offline rehearsal does this for its manual scenarios and keeps one autopilot scenario).

The dashboard shows what triggered the run, how many gates autopilot answered, every autopilot decision under Results, and how many pushes are waiting.

**A push goes where the profile says (live run 4).** `platform` and `tier` in `orchestrator.yaml` are not only answers: the push workflow's first step runs `scripts/profile_env.py` (standard library only) and exports `ORCHESTRATOR_DISPATCH=platform` and `ORCHESTRATOR_CLOUD_APPLY=1` for `jenkins`, `aws`, `gcp`, or `azure` (`github` and `0` for `local`), plus the profile's `config:` block (registry names of kind `config` only, such as `AWS_REGION`; a secret in that block is skipped). The session exports the same config names where the host leaves them empty, so `docker compose up` on a laptop behaves like the runner. The workflow layers `docker-compose.ci.yml` so the orchestrator works on the pushed checkout (mounted at `/workspace`, `ORCHESTRATOR_REPO_ROOT`) and not on the image's copy of the app. Credentials never come from a file: Connections → github → **Sync secrets to GitHub** (or `python -m orchestrator.connect sync-github`) copies every value present on the host whose name is marked `github_secret` in the registry into the repository's Actions secrets, the value on stdin and names only in the response; the workflow passes those secrets through to Compose. `ORCHESTRATOR_UI_TOKEN` is one of them: the tunnel URL is public, so keep one in the vault and the dashboard asks for it once per tab. The job allows 120 minutes and the session 90.

Three ways a push reaches the orchestrator:

1. **GitHub Actions** (`orchestrator.yml`): checkout, profile step, Compose, tunnel, wait.
2. **Webhook.** Point the host at `POST <orchestrator>/api/orchestrator/hooks/push` with the `ORCHESTRATOR_WEBHOOK_SECRET` value as the HMAC secret (GitHub, Gitea, Bitbucket) or the token (GitLab). The orchestrator clones the pushed commit into `var/workspaces/<slug>` (private HTTPS remotes need `GIT_REMOTE_USER` and `GIT_REMOTE_TOKEN` in the vault; SSH remotes `SSH_KEY_PATH`) and starts the run there. A push that arrives while a run is active waits in the queue. `remotes.branches` in the profile filters branches. For a bare remote on any host, install `scripts/git-post-receive.sample`.
3. **Polling.** Set `ORCHESTRATOR_WATCH_REMOTE` (and `ORCHESTRATOR_WATCH_INTERVAL`) when the host cannot reach the orchestrator; the service runs `git ls-remote` on the interval and starts a run when a watched branch moves.

## AWS live run (Sprint 4, phase 12)

The platform proof with a real account. Every apply is a decision; nothing below runs on its own.

1. **Connect.** In the dashboard, Connections → aws: the console login (`aws login --remote`, paste the authorization code back) with `AWS_PROFILE` set, or the static key names in the vault. Verify shows `apply`. Set the config names `AWS_REGION`, `AWS_S3_BUCKET`, `AWS_ECR_REPOSITORY`, `AWS_CODEBUILD_PROJECT`.
2. **Nothing to bootstrap by hand.** On the first apply the orchestrator itself, with the vault credentials, reads the account id, derives any unset name (`langgraph-orchestrator-<account>` for the bucket, the app name for the ECR repository, `langgraph-support-app-orchestrator` for the project), creates the bucket (public access blocked), the repository, and the scoped CodeBuild service role (`<project>-codebuild-role`: its logs, the bucket, the repository, `langgraph-support-app/*` secrets, and the CloudFormation, ECS, EC2, log-group, and task-role calls a Fargate deploy of the generated stack needs, all scoped to names prefixed with the app name), then creates or updates the project. A role you configure in `AWS_CODEBUILD_ROLE_ARN` under another name is left alone. `scripts/aws_bootstrap.sh` remains as a manual fallback that prints the same policy.
3. **Run.** `ORCHESTRATOR_DISPATCH=platform ORCHESTRATOR_CLOUD_APPLY=1`, platform `aws`, tier `dev`. After Jev's choice Astra, as the orchestrator, assigns every stage (and the suite) a sub-agent: a model from `ORCHESTRATOR_ASTRA_WORKER_MODELS` (default `gpt-6.1-terra,gpt-6.1-sol`, the orchestrator model is always an option) and a reasoning effort; the Platform panel and the report list the assignments, and each stage's worker writes its files, briefs it, reads its findings, and diagnoses its failure at that model and effort. After approval the orchestrator uploads the source zip (the checkout plus every generated file of the run, suite first) to `s3://<bucket>/orchestrator/<run>/source.zip`, creates the CodeBuild project from the generated document, then starts one CodeBuild build per stage bound to CodeBuild, fixed or generated, with its generated buildspec and polls it. When the suite cannot be generated, every stage bound to the platform runs on the host instead and the approval screen says so. One Astra call may take `ORCHESTRATOR_ASTRA_TIMEOUT` seconds (default 300). A stage file binds a secret only when the stage needs it and the name has a value on the orchestrator host (never the AWS keys themselves: inside CodeBuild the service role is the identity); before the first build the orchestrator creates or updates `langgraph-support-app/NAME` in Secrets Manager for exactly the names the files bind, and a failed injection stops the run. `deploy` only creates a CloudFormation change set (`--no-execute-changeset`); executing it is a person's call. UAT waits in the dashboard. Speed (after live run 2): Astra writes the generated stages and the suite concurrently (`ORCHESTRATOR_ASTRA_CONCURRENCY`, default 4) at reasoning effort `ORCHESTRATOR_ASTRA_REASONING` (default `low`; a model that rejects the field is retried without it), every CodeBuild build caches pip and npm in the bucket (`orchestrator/cache`) and is stopped by the orchestrator after 20 minutes (`timeoutInMinutes` 20 in the project), and `ORCHESTRATOR_AUTOSTART=1` starts the checkout and the questions as soon as the server is up, so the dashboard shows the run from its first node.
4. **Tear down.** `scripts/aws_teardown.sh` prints what a run created (the project, its log group, the uploads); `--apply` removes them, `--all` also the bootstrap resources.

Cost: CodeBuild `BUILD_GENERAL1_SMALL` minutes for each stage build (a few cents per run), S3 and CloudWatch Logs at cents, no ECS unless a deploy stage creates the stack (then roughly five to ten cents an hour until `scripts/aws_teardown.sh --apply` removes it).

### Live run 1 (2026-11-03): rejected at the approval gate, nothing applied

Bootstrap applied once in the user's account (us-east-1): the source bucket, the ECR repository,
and the CodeBuild service role. The run (`ORCHESTRATOR_DISPATCH=platform ORCHESTRATOR_CLOUD_APPLY=1
ORCHESTRATOR_AUTOPILOT=0`, answers dev / aws) reached the approval gate in about 25 minutes:
Astra's five questions, Jev `proceed` (no follow-up), the stochastic candidate ranked at 0.75,
and five generated stage sets (frontend_build, build_image, container_scan, sbom, tag_release)
written and accepted with the policy clean. Three defects stopped it there, so the pipeline was
rejected and no AWS call was made (spend 0):

1. The suite failed before any apply: OpenAI strict structured output refused `PipelineSuite`
   because `stage_files` was a free-form mapping. The fixed `build` and `test` stages fell back
   to the host as designed, and nothing would have provisioned the CodeBuild project.
2. `infra_plan` was dropped after two 120 s client timeouts on its CloudFormation artifact.
3. The generated stages stayed bound to `aws_codebuild` and would have called `start-build` on a
   project that did not exist; the source upload would not have carried their scripts.

Fixed before run 2: `PipelineSuite` no longer carries the mapping (the file per stage is derived
from the written paths; `tests/test_astra_schemas.py` checks every Astra model against strict
mode); `ORCHESTRATOR_ASTRA_TIMEOUT` (default 300 s) bounds a call; the source zip packs every
generated file of the run (`suite.provision_bundle`); a stage bound to the platform falls back to
the host together with the fixed stages whenever the suite is absent; the AWS connection uses
`aws login --remote` (the console flow) instead of `aws sso login`.

The first attempt at run 2 stopped at the questions gate before any plan: Astra recalled run 1
and asked three questions without `env_platform` or `env_target`, the orchestrator reads only
the current run's answers, so the plan would have routed to the local host, and the answer
endpoint refused the extra ids. Fixed before the next attempt: an episode keeps what the
person answered for the environment (`answered`), `clarify_with_user` reuses the newest
episode's platform and tier when Astra skipped those questions (shown as a `recall` note in
the timeline), `POST /session/answer` accepts `env_target` and `env_platform` even when not
asked (validated), and the question prompt says when the two may be skipped.

### Live run 2 (2026-11-03, 19:22–20:05 CST): stopped on exhausted OpenAI credits, nothing applied

Verify passed (the IAM user, mode `apply`). Astra recalled run 1 and asked three questions;
the platform and tier were posted alongside them. Jev ranked `registry_free_aws_dev_image_delivery`
(0.80): seven generated stages on CodeBuild plus the fixed build and test. Astra wrote six stage
sets (23 files, every review `accept`, policy clean) in 40 minutes, the last one after a
30-minute stretch of client retries. Then the OpenAI account ran out of credits: the seventh
stage (`post_deploy_smoke`) and the platform suite both failed with `insufficient_quota`, the
host fallback moved every stage to the local runner and said so on the approval screen, and
the run was rejected at the gate as the out-of-credits rule requires. No AWS call was made.
Hardened afterwards: a quota error stops the run at once instead of generating on, and the
OpenAI client retries once, not twice, so a failing call cannot hold a stage for half an hour.
Run 3 needs credits and a new card.

### Live run 3 (2026-11-04, 08:49–09:10 CST, run 66bb3af8): provisioning failed on a 268-character project description, no build started

Started from server start-up (`ORCHESTRATOR_AUTOSTART=1`): questions 10 s after the first node,
answers posted 46 s later (platform and tier with them), Jev `proceed` without a follow-up,
plan `aws_dev_scanned_release` chosen 70 s after the answers (nine generated stages, three
fixed stages on CodeBuild, UAT local). Astra, as the orchestrator, assigned the workers:
`gpt-6.1-terra` at minimal effort for the fixed stages, `gpt-6.1-sol` at medium or high for
the generated ones, `gpt-6-astra` at high for the suite. Generation ran on four threads and
took 10 minutes (the high-effort workers); every review `accept`, policy clean, suite `ok`.
Rejected at the gate for two file defects: the suite had turned `privilegedMode` off (the
image stages run docker) and two buildspecs set `AWS_REGION` to an empty string (the
generated-stage skeleton carried `$REGION`, which has no value at that point). The re-plan
kept the route (`aws_codebuild_preserved_route`, one more stage), regenerated in 3.4 minutes,
and was approved at 09:09. Provisioning then bootstrapped the account itself (bucket,
repository, and role already there; role policy set), uploaded `source.zip` (1.3 MiB, 256
checkout files plus 16 generated), confirmed the repository, and failed on
`create-project`: "Max description length is 255" (the template sentence added that morning
plus Astra's own sentence). The run escalated; no build started; AWS spend: one upload and
one `put-role-policy`. Fixed the same hour: the project document is hardened in code
(`privilegedMode` forced on, the description cut to 255), the generated-stage skeleton no
longer sets `AWS_REGION` (the project supplies it) and an empty one is dropped in code, the
worker prompt carries the five-minute budget, and `tests/test_hardening.py` pins all of it.
The next attempt needs a new card.

## Rehearsal (offline, before the live run)

```bash
/opt/anaconda3/envs/tcs_practice_env/bin/python scripts/rehearse_demo.py
```

No key and no network. The script drives three sessions through the same HTTP API the dashboard uses, with the deterministic doubles from `tests/` in place of Astra and Jev: `local` (a stochastic route whose generated `container_scan` executes on the local runner and prints `generated-ok`), `cloud_plan` (a `deploy_dev` stage on AWS CodeBuild that records `planned` with the missing names and still passes the gate), and `recall` (a second run that recalls the first episode before the questions). It plants two fake credential values first and fails if either appears in readiness, a view, a report, or a generated file. Output: `var/rehearsal/<timestamp>/` with `view-<phase>.json` per scenario, `report.md`, `report.json`, `artifacts.json`, `episodes.json`, and `summary.md`. The last rehearsal on 2026-10-28 passed all nine checks in under one second per scenario.

## Demo variants

Rehearse A, then B, then C once `gh auth refresh -h github.com` has a valid token.

- **A. Local processes.** `ORCHESTRATOR_AUTOSTART=1 ORCHESTRATOR_DISPATCH=local uvicorn app.main:app --port 8000`, then `cd frontend && npm run dev` and open `http://127.0.0.1:5173/#/orchestrator`. The docker runner applies (the daemon is on the host); cloud runners plan unless their names are set.
- **B. Compose.** `docker compose up --build`, then `http://127.0.0.1:8000/#/orchestrator`. Inside the container the docker runner plans unless the socket line in `docker-compose.yml` is uncommented.
- **C. Push.** The Actions log prints the tunnel URL; `ORCHESTRATOR_DISPATCH=github`; fixed stages dispatch their workflows; generated workflows dispatch only with `ORCHESTRATOR_WRITE_ARTIFACTS=1` and a token that can push the `orchestrator/generated/<run_id>` branch.

## What to say, per screen

1. **Readiness.** "The orchestrator knows which CLIs and which credential names exist. The AWS CLI is present, the credentials are not, so AWS routes will plan, not apply."
2. **Questions.** "Astra read the checkout with retrieval; it asks about platform and runner and says what is missing."
3. **Follow-up.** "Jev decided one more round was worth it."
4. **Candidates.** "A deterministic route and a stochastic route, with Jev's probabilities. The stochastic route adds a container scan and a dev deploy on CodeBuild."
5. **Artifacts.** "Astra wrote this script, this buildspec, this env manifest, and this doc. The policy checked them: pinned actions, placeholders only, no production."
6. **Approve, stages run.** "The scan ran here for real; the CodeBuild deploy is planned with the exact command and the variables it needs."
7. **UAT sign-off, results.** "Findings, next steps, retrieval evidence, and the recorded episode."
8. **Second run.** "Astra recalls the first episode and proposes with that lesson."

## Tests

```bash
conda activate tcs_practice_env
pytest -q
ruff check app mcp_server orchestrator tests scripts
```

Tests that need a human answer post it through the session HTTP API, with a fake Astra and a fake Jev so they do not call the network. That helper is test code. CI does not use it.
