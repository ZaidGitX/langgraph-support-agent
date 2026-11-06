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

## Scan and test menu (Sprint 4)

The fixed GitHub workflows (`scanners.yml`, `dast.yml`, `test.yml`) are reference
implementations, not a step every run takes. Which scans and tests run is an answer:

- `scan_tools`: any of the scanner catalog by name (semgrep, pip-audit, snyk, trivy, gitleaks,
  sonar / SonarQube Cloud, codeql), `all` (the default when unanswered), or `none`.
- `dast_tools`: `zap`, `stackhawk`, both, or `none`.
- `scan_policy`: the severity that fails a run (`high and critical` by default; naming `medium`
  or `low` lowers the bar, `critical only` raises it).

`orchestrator.yaml` answers them for pushes (`answers.by_id.scan_tools`, `dast_tools`, and the
`scanner` / `dast` topics). `scanners.selection_from_answers` turns the text into catalog keys and
`selection_env` into the three values the contract scripts read: `SCAN_TOOLS`, `SCAN_SEVERITY`,
`DAST_TOOLS`. Every suite skeleton carries them in front of the call
(`SCAN_TOOLS="semgrep,pip-audit" SCAN_SEVERITY="HIGH,CRITICAL" bash ci/scanners.sh`) on Jenkins,
CodeBuild, Cloud Build, and Azure alike; `suite.assert_contract` puts the call back, selection
included, if the model drops it. `ci/scanners.sh` records every catalog tool as passed, failed,
skipped (not selected, credential not provided, time cap), or error and fails only on `failed`;
`ci/dast.sh` runs ZAP and StackHawk the same way against the deployed URL.

A provider that needs an account runs when its names are provided and is skipped otherwise:
Snyk (`SNYK_TOKEN`), SonarQube Cloud (`SONAR_TOKEN`, `SONAR_ORGANIZATION`, `SONAR_PROJECT_KEY`),
StackHawk (`HAWK_API_KEY`, `HAWK_APP_ID`). Add them on the Connections page or in `.env`; the
orchestrator injects the ones a suite references into the platform's store, by name.

Astra sees the same menu: `scan_selection` and `scanner_catalog` (image, argv per stage, env
names, `skip_without`, and the severity rule of every tool, names only) are in the suite context
and in the per-stage context of scan, verify, and test stages, so a generated stage for any
provider in the catalog, or a platform-native scan step, is written from the catalog rather than
from the GitHub workflow text.

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

### Live run 4 (2026-11-04, 17:33–19:06 UTC, workflow run 41878484209, run 19c8e9ab): started by the push alone, lost the platform suite to a timeout, ended at the human approval gate

The first run nobody started by hand. `git push` of the phase-12 commits (c6daab6, 566eff5) started `orchestrator.yml`; the profile step exported `ORCHESTRATOR_DISPATCH=platform`, `ORCHESTRATOR_CLOUD_APPLY=1`, and `AWS_REGION`; Compose built the image and started the app on the pushed checkout (`/workspace`); the tunnel came up and AWS readiness in the runner read `apply` with no missing name (the five repository secrets had been synced from the vault minutes earlier, after the first sync attempt found that `gh secret set` has no `--body-file` flag).

| Step | When (UTC) |
| --- | --- |
| Runner picked up the job (GitHub Actions was in a degraded-availability incident from 17:40) | 17:33 |
| App up, checkout and retrieval done | 17:35:40 |
| Autopilot answered the five questions from `orchestrator.yaml` (aws, dev, release, scan, test depth) | 17:35:51 |
| Jev: proceed; ranked `generated_ecs_dev_delivery` (1.00); runners `aws_codebuild` for the seven generated stages; Astra assigned twelve workers | 17:36:56 |
| Generation finished: 23 files | 17:57:57 |
| Autopilot handed the approval to a person ("an artifact has a policy error") | 17:57:58 |
| The 90-minute session wait expired, the workflow stopped the app | 19:05:57 |

What went wrong, and the fix that followed each:

- The platform suite went to `gpt-6-astra` at high effort and the call ran past the 300 s timeout twice, so every platform stage fell back to the host and the AWS run was gone before approval. Now a worker call's timeout follows its effort (`high` 900 s, `medium` 600 s, never below `ORCHESTRATOR_ASTRA_TIMEOUT`) and a call that still times out is retried once at the next lower effort before the stage or suite is given up (`llm.AstraWorker`, `timeout_for`, `LOWER_EFFORT`).
- `build_image`'s worker named its file `Dockerfile.dev.generated`; the policy only knew the `Dockerfile.generated` prefix, so the stage failed policy and Jev rejected it. A root Dockerfile or Compose file is now fine when `.generated` sits anywhere after the base name (`policy.ROOT_GENERATED_FILE`, both copies).
- The 21 minutes of generation showed nothing on the timeline (node events arrive when a node ends). The generation node now reports one `generation` event and a `worker` event when each stage's files start and finish (model, effort, seconds, review verdict, any lower-effort retry) through `runner.progress`; the Stages tab of the timeline lists them.

Nothing was applied on AWS; OpenAI usage was one question round, two proposals, one worker assignment, about 16 generation and review calls, and the two timed-out suite calls.

### Live run 5 (2026-11-05, 01:08–01:25 UTC, workflow run 41946454197, run 9721290b): the suite finished, three policy findings left a gate nobody could pass, cancelled

Started by the push of c2608db. Questions answered at 12 s, Jev's plan at 95 s, generation 01:12:25–01:21:49 with every worker on the timeline (eight generated stages in 14–127 s each; the suite in 500 s at high effort, inside its new budget). The approval gate went human again: `integration_test` and `post_deploy_smoke` wrote files under `scripts/ci/` and a workflow not named `gen-<stage>.yml` (policy `path.outside`, Jev rejected both, dropped), and the suite's `ci/buildspec-scanners.yml` bound `SNYK_TOKEN: "SNYK_TOKEN"` under `secrets-manager`, which the secret rule read as a literal (Jev rejected the suite after its one revision, so every platform stage fell back to the host). Because the approval view carried the dropped stages' and the rejected suite's findings, neither autopilot nor a person could approve; the run was cancelled with nothing applied on AWS.

Fixes that followed: a value that is itself a credential name, optionally behind a path, is a reference and not a literal in both policy copies; the suite qualifies a bare id in a buildspec's `secrets-manager` block with the app prefix so injection and the build agree; a file a worker writes outside the allowed prefixes is moved (`scripts/…` and loose scripts to `ci/`, a workflow to `.github/workflows/gen-<stage>.yml`, a Dockerfile or Compose file to `docker/`) and its purpose says so; the approval payload carries only the stages still in the plan (and the suite when it is in use), with the rest under `dropped_artifacts`; a Jev reject that comes with policy findings gets the stage's one revision first.

### Live run 6 (2026-11-05, 01:46–02:03 UTC, workflow run 41950786100, run c25e0978): every file passed policy, Jev rejected two clean sets, the plan ran on the host and escalated

Started by the push of 6b5195a. Questions at 11 s, Jev's plan at 81 s, eight generated stages written and accepted in 21–66 s each (the relocated paths held), the suite in 638 s at high effort with every file policy-clean. Then Jev rejected the suite (0.91) and `deploy_dev` (0.94) after their one revision: the review criteria read "revisions are exhausted" as a reason to reject even with a clean policy result. With the suite rejected every platform stage fell back to the host, autopilot approved the host plan (policy clean, reviews of the kept stages accepted, confidence 1.00), `build` failed on `compileall` (the mounted checkout was not writable for `__pycache__`), `post_deploy_smoke` failed because a Python script was run with bash, the gate escalated at 02:03:03, and the job was cancelled. Nothing was applied on AWS.

Fixes that followed: Jev's reject criterion now reads "still reports an error and no revision is left", and `guard_review` accepts a clean policy result whatever the verdict (every reject criterion is a policy finding); a cloud profile does not approve a plan whose suite is not in use: autopilot rejects it once with the reason as feedback so Astra plans again, and a second miss goes to a person; the push workflow makes the whole checkout writable for the container's user; a `.py` stage script runs with `python3` on the host.

### Live run 7 (2026-11-05, 08:59–09:15 UTC, workflow run 41998717456, run cdf28981): the first push-driven plan that was approved on AWS, stopped at the first aws call by an empty `AWS_PROFILE`

Started by the push of cca8340. Questions at 10 s, Jev's plan at 73 s, seven generated stages and the suite (11 files, 361 s at high effort) all accepted, autopilot approved at 09:14:18 (policy clean, reviews accepted, confidence 1.00) with every fixed and generated stage bound to `aws_codebuild`. Provisioning then failed at `sts get-caller-identity`: Compose passes every placeholder (`AWS_PROFILE: ${AWS_PROFILE:-}`), so an unset host value reaches the container as `""`, and the aws CLI reads that as a profile named "" ("The config profile () could not be found"); injection failed the same way, the gate escalated at 09:14:45, and the job was cancelled. Nothing was created on AWS.

Fix that followed: `credentials.drop_empty_env()` removes registry variables whose value is empty; the app runs it at startup before the vault export and the AWS provider runs it before its first call. Unset means unset.

### Live run 8 (2026-11-05, 09:27–09:45 UTC, workflow run 42002552007, run ca71556b): the first builds on CodeBuild

Started by the push of c0c5c66. Questions at 11 s, Jev's plan at 76 s, eight generated stages and the suite (5 files, 327 s) accepted, autopilot approved at 09:38:44 (confidence 0.98). Provisioning applied for the first time: the bootstrap confirmed the bucket, the repository, and the role (policy set), uploaded `source.zip` (the checkout plus 22 generated files), and created the CodeBuild project `langgraph-support-app-orchestrator`; injection created `langgraph-support-app/SNYK_TOKEN` in Secrets Manager. Then the waves ran on CodeBuild:

| Stage | Result |
| --- | --- |
| `build` | SUCCEEDED in 70 s (every phase) |
| `frontend_build` | SUCCEEDED |
| `test` | every phase SUCCEEDED, then UPLOAD_ARTIFACTS FAILED: the buildspec skeleton carried an `artifacts:` block and the project has no artifacts destination |
| `scanners` | BUILD FAILED; the evidence held only the phase list, no log |
| `infra_plan` | never reached CodeBuild: the provider ran its `aws cloudformation deploy` from the orchestrator host, where `deploy/stack.yaml` does not exist |

The gate retried the three failed stages once (cap 2), escalated at 09:44:41, and the job was cancelled at the results screen. AWS spend: five CodeBuild builds of about a minute each, one upload, one secret.

Fixes that followed: both buildspec skeletons lost their `artifacts:` block and `generator.harden` drops one a model writes; a stage that has a buildspec always runs inside CodeBuild (`would_run` looks for the buildspec before the provider-specific host command); a failed build's evidence carries the last 60 lines of its CloudWatch log (`_log_tail`), for the person and for Astra's diagnosis; `deploy_defaults(tier)` exports `AWS_CFN_STACK`, `AWS_ECS_CLUSTER`, and `AWS_ECS_SERVICE` as `<app>-<tier>` when unset, and the project document now hands every build the account, bucket, repository, stack, cluster, and service names.

What the CloudWatch logs and the uploaded `source.zip` of run 8 showed afterwards, and what changed because of it (before run 9): the `test` build had passed every phase and failed only because its buildspec's `reports:` section needs `codebuild:CreateReportGroup` (now in the role policy); the `scanners` script treated every semgrep finding and every un-credentialed tool (Sonar, CodeQL) as fatal; the `deploy_dev` buildspec was the untouched skeleton (`echo "stage deploy_dev placeholder"`); the `infra_plan` stack invented a `SERVICE_ENABLED` name; the image was tagged by commit while the stack expected a tag by run. The platform plumbing is therefore code-written now, the **platform contract**: `orchestrator/templates/contract/*.sh` (build, test, scanners, package everywhere; container, release, deploy, smoke, dast on AWS) travel with every suite, each fixed stage's file runs `bash ci/<stage>.sh`, the orchestrator puts the files back if the model changes or drops them, a generated deploy, verify, or image stage on CodeBuild gets the matching call when its file would do nothing, a skeleton returned unchanged is a policy error, every scan of `scanners.yml` runs with findings gated at `SCAN_SEVERITY` (HIGH,CRITICAL) and a tool whose credential is not provided recorded as skipped, the deploy finds the default VPC and public subnets (`AWS_VPC_ID`, `AWS_SUBNET_IDS` when set), creates or updates the `<app>-<tier>` stack, waits for the service, checks `/status` on the task's public IP and publishes the URL for smoke, dast, and UAT, the task reads the app's own keys from Secrets Manager, builds run on `BUILD_GENERAL1_MEDIUM` for 30 minutes at most, and `.env` and key files never travel in the upload. Reference: `14-suites-provisioning-injection.md`, "Platform contract". On this checkout semgrep (p/python, p/javascript) reports no ERROR-level finding and trivy no HIGH or CRITICAL vulnerability, so the scanners stage is expected to pass.

### Live run 9 (2026-11-05, 21:06–21:30 UTC, workflow run 42109645409, run b0b24b1f): the scan menu answered, 38 files approved, AWS provisioned, stopped at secret injection

Started by the push of df9b3dc (the scan and test menu). The push alone ran the whole front
half: questions at 10 s, now six of them with the two menu questions (`scan_tools`,
`dast_tools`) answered from `orchestrator.yaml`; Jev `proceed`; the plan `generated_ecs_dev-1`
ranked at 80 s with ten stages on `aws_codebuild` (build, infra_plan, scanners, test,
integration_test, build_image, container_scan, deploy_dev, post_deploy_smoke, dast_scan) and
UAT local. Generation 21:10:37–21:16:38 on four workers: the seven generated stages in 14–115 s
each, every review `accept`, the suite (16 files) in 282 s at high effort, 38 files in all, no
policy finding. Autopilot approved at 21:16:38 (confidence 0.98). Provisioning applied: bucket,
repository, role, and the default VPC with six subnets confirmed, `source.zip` with 260 checkout
files plus 31 generated files uploaded, the CodeBuild project in place. Then injection failed
before the first build: `no value for AWS_SESSION_TOKEN`, and the guardrail escalated at
21:17:16. No build ran. The orchestrator marked the results viewed through the dashboard with
the token the user had entered, and the workflow stopped the application.

| Moment | Time (UTC) |
| --- | --- |
| Questions answered by the profile (six, two of them the scan menu) | 21:09:27 |
| Jev ranked `generated_ecs_dev-1`, runners assigned | 21:10:37 |
| Generation finished: 38 files, every review accept, policy clean | 21:16:38 |
| Autopilot approved (confidence 0.98) | 21:16:38 |
| Provisioning applied (upload, repository, project) | 21:16:47 |
| Injection failed on `AWS_SESSION_TOKEN`; escalated | 21:17:16 |

What went wrong, and the fix:

- The injection names were the union of every file's declared `secrets_referenced`, and the
  generated stages' `.env.dev.example` manifests, a `ci/verify-build-image.py`, and a
  `.github/workflows/gen-deploy_dev.yml` had declared the orchestrator's own AWS identity
  (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`), because those names were
  among the stage placeholders. The session token has no value with static keys, so
  `inject_secrets` failed and the run stopped. Now: a platform's identity secrets
  (`credentials.identity_secrets`) are never placeholders for a stage that runs inside that
  platform and never injectable; injection takes only explicit bindings plus what a record the
  platform itself resolves declares (`suite.bound_secret_names`, `PLATFORM_BOUND_KINDS`: on AWS
  the buildspecs, the stack, the project document); `generator.harden` drops a `secrets-manager`
  entry for an identity secret or a secret without a value; and `profile.approval` refuses a plan
  whose injection plan still has an unresolved bound name, so the session rejects it once with
  that feedback instead of approving a run that cannot start.

Nothing was built on AWS; the only writes were the source upload and the project document.
OpenAI usage: one question round, two proposals, one worker assignment, seven stage
generations, one suite generation, and the reviews.

### Live run 10 (2026-11-05, 21:33–21:41 UTC, workflow run 42113562288, run 3c2d392a): the injection fix held and applied, then the OpenAI account ran out of credits at the first stage brief

Started by the push of 703b104. The front half ran as in run 9: six questions answered by the
profile at 10 s, Jev's plan at about a minute (seven generated stages on CodeBuild plus the fixed
build, test, and scanners), generation 21:36:00–21:40:30 (the seven stages in 30–132 s each, the
16-file suite in 200 s, every review accept, policy clean), autopilot approved at 21:40:30
(confidence 0.98). The approval view's injection plan named only `OPENAI_API_KEY`, `SNYK_TOKEN`,
and `TYPESAFE_API_KEY`, nothing missing: the run 9 defect is gone. Provisioning applied at
21:40:35 (upload, repository, project) and injection applied: the three secrets were written to
Secrets Manager at 21:40:39–21:40:42. The first stage node then asked its worker
(`gpt-6.1-sol`) for the stage brief and OpenAI answered 429 `insufficient_quota`
(`credit_balance_exhausted`, "You have no credits remaining"); the session went to `error` at
21:40:53, the workflow's waiter exited 1, and the application was stopped. No CodeBuild build
started; no stack exists.

| Moment | Time (UTC) |
| --- | --- |
| Questions answered by the profile | 21:34:51 |
| Generation started on four workers | 21:36:00 |
| Suite finished (16 files, 200 s); autopilot approved | 21:40:30 |
| Provisioning applied; three app secrets injected | 21:40:35–21:40:42 |
| First stage brief: OpenAI 429, no credits; session error | 21:40:53 |

What went wrong, and the fix:

- The account's OpenAI credits ran out between run 9 and the first stage of run 10. That is the
  account's state, not a defect, and the next run needs credits first. The defect was that a
  stage *brief*, an advisory text, could end an approved run whose stages need no model at all
  (CodeBuild runs the files; Jev, a different vendor, routes). Now `_brief_for` returns a plain
  note when Astra cannot answer, the quality gate records "Astra was unavailable for the
  diagnosis" instead of failing, and the deploy document is skipped on any Astra error; the
  stages run as approved (`tests/test_run10_astra_optional.py`).
- The container's log was gone with the runner, so the only trace of the exception was the
  waiter's one line. The workflow's "Stop the application" step now prints the orchestrator
  container's last 400 log lines (already redacted by the orchestrator) before `compose down`.

Nothing was built on AWS; the writes were the source upload, the project document, and the
three secret values. OpenAI usage: one question round, two proposals, one worker assignment,
seven stage generations, one suite generation, the reviews, and the 429.

### Live run 11 (2026-11-05, 21:54–22:16 UTC, workflow run 42116879842, run 12a069ba): the first stages built on CodeBuild through the contract; three stages failed and Jev escalated

Started by the push of 6cf5a21 after a one-call probe of the OpenAI key. Questions answered by the
profile at 21:57:48, generation 21:58–22:08 (eight generated stages in 17–102 s each, the 16-file
suite in 486 s, every review accept, policy clean), autopilot approved at 22:08:06 (confidence
1.00) with an injection plan of exactly `OPENAI_API_KEY`, `SNYK_TOKEN`, `TYPESAFE_API_KEY`.
Provisioning applied (upload, repository, `update-project` ok) and injection applied. Then the
first platform wave: `build` passed on CodeBuild at 22:09:34 through `bash ci/build.sh`, the
first fixed stage ever to pass on the platform path, and `test` passed at 22:11:05 (373 tests).
The scanners stage honored the menu (`selected scan tools: semgrep,pip-audit,snyk,trivy,gitleaks`):
semgrep, pip-audit, and both Snyk tests passed, Sonar and CodeQL were skipped as not selected.
Three stages failed, were retried once by Jev's `remediate`, failed the same way, and Jev
escalated at 22:15:51 (confidence 0.35); the orchestrator cancelled the idle workflow job.

| Moment | Time (UTC) |
| --- | --- |
| Questions answered by the profile | 21:57:48 |
| Suite finished (486 s); autopilot approved at 1.00 | 22:08:06 |
| Provisioning and injection applied | 22:08:13–22:08:45 |
| `build` passed on CodeBuild | 22:09:34 |
| `test` passed; `sca` failed | 22:11:05 |
| `integration_test` failed; `scanners` failed | 22:12:02 / 22:12:24 |
| Retries failed; Jev escalated | 22:15:05 / 22:15:51 |

What went wrong, and the fix:

- `sca` (generated): its buildspec had no `env.shell: bash`, so CodeBuild ran the multi-line
  command with sh and died at `set -euo pipefail` ("Illegal option -o pipefail"). Both buildspec
  templates now say `shell: bash`, `generator.harden` adds or corrects it in every buildspec, and
  the generation prompt says so.
- `integration_test` (generated): `pytest -m integration` collected nothing (373 deselected) and
  pytest's exit code 5 was treated as a failure. The catalog's pytest entry now carries the exit
  codes, and the prompt says a test level with no tests is recorded as skipped with exit 0.
- `scanners` (contract): trivy found four HIGH/CRITICAL misconfigurations, all in two files the
  `infra_plan` worker wrote (`deploy/dev/ecs.generated.yml`: internet-facing load balancer with a
  plain-HTTP listener; `deploy/ecr.generated.yml`: a repository policy with `Principal: "*"`), and
  gitleaks flagged one synthetic key-shaped string in `tests/test_run5_hardening.py`. The
  repository's own files had none (verified locally on the uploaded `source.zip`). Now a wildcard
  principal is a policy error (`iac.public_principal`, both copies) so Jev sends such a file back
  before approval, the prompt tells Astra that its IaC must pass trivy misconfig and that
  `infra_plan` validates the orchestrator's stack rather than writing a second one, the test string
  is built at runtime, and `ci/scanners.sh` prints each failed tool's findings (file, rule, severity)
  right before its summary so the dashboard evidence and Astra's advice see them.

Nothing was deployed; the writes were the source upload, the project update, the three secret
values, and six CodeBuild builds (two passed, four failed). The GitHub job was cancelled at 22:17.

### Live run 12 (2026-11-05 23:41 – 2026-11-06 00:11 UTC, workflow run 42131461024, run 6250c14f): build, test, integration_test, and the scanners menu passed on CodeBuild; the generated infra_plan stage failed on its own change set

Started by the push of 1bf53f6 after a one-call probe of the OpenAI key. Questions answered at
23:45:18, plan `generated_ecs_dev_delivery-1` at 23:46:28 with nine generated stages, generation
23:46–00:02 (the suite in 440 s; `infra_apply` took 932 s because its high-effort call timed out
and was retried at the next lower effort), 44 files, every review accept, policy clean, autopilot
approved at 00:02:28 (confidence 0.98), provisioning and injection applied. Then the platform
waves: `build` passed at 00:04:55; `integration_test` passed at 00:07:31 (the exit-5 rule held);
`scanners` passed at 00:07:52, the first pass of the scan menu on CodeBuild (semgrep, pip-audit,
Snyk, trivy, gitleaks, with Sonar and CodeQL skipped as not selected); `test` failed once on a
timing-sensitive test and passed on Jev's retry; `infra_plan` failed twice: the worker's buildspec
ran its own `aws cloudformation deploy --no-execute-changeset` against the orchestrator's stack
without `--parameter-overrides` ("Parameters: [VpcId, Image, SubnetIds] must have values"). Jev
escalated at 00:10:50 (confidence 0.53) before the deploy wave; the orchestrator cancelled the idle
workflow job.

| Moment | Time (UTC) |
| --- | --- |
| Questions answered by the profile | 23:45:18 |
| Generation finished (44 files); autopilot approved at 0.98 | 00:02:28 |
| `build` passed on CodeBuild | 00:04:55 |
| `infra_plan` and `test` failed; `integration_test` passed | 00:07:25–00:07:31 |
| `scanners` passed: the scan menu's first pass on the platform | 00:07:52 |
| Retry: `test` passed, `infra_plan` failed again; Jev escalated | 00:10:19 / 00:10:50 |

What went wrong, and the fix:

- A model-written stack mutation is error-prone (run 8 invented a parameter, run 12 omitted
  them all), and the contract already resolves every parameter. `ci/deploy.sh` now has
  `DEPLOY_MODE=plan`: it validates the template, resolves VPC, subnets, image, and names exactly
  as the deploy would, reports the stack's and the image's state, and creates nothing. On
  CodeBuild an `infra_plan` (or any `*_plan`) stage is floored to `DEPLOY_MODE=plan bash
  ci/deploy.sh`, `infra_apply` and deploy stages to `bash ci/deploy.sh`, and any `cloudformation
  deploy / create-stack / update-stack / change-set` command the model wrote beside them is
  removed (`generator.platform_floor`, `_STACK_MUTATION`); the prompt says the same.
- `tests/test_autopilot.py::test_a_policy_error_keeps_the_approval_gate_human` read the autopilot
  event list the instant the phase flipped; on the CodeBuild host the event landed a moment later.
  The test now waits for the approval-gate event.

Nothing was deployed; the writes were the source upload, the project update, the three secret
values, and eight CodeBuild builds (five passed, three failed). The GitHub job was cancelled at
00:12.

### Live run 13 (2026-11-06, 00:27–00:45 UTC, workflow run 42137003919, run 0bcdc0e3): four stages passed on CodeBuild at the first attempt; the generated sca stage re-ran the whole scanners contract and died on a system-wide semgrep

Started by the push of aee3b48 after a one-call probe of the OpenAI key. Questions answered at
00:30:41, eight generated stages and the suite (230 s) accepted, autopilot approved at 00:37:29
(confidence 0.98), provisioning and injection applied. The platform waves: `build` passed at
00:38:50, `test` at 00:40:20 at the first attempt (the run 12 flake is gone), `frontend_build` at
00:41:01, `scanners` at 00:41:46. `sca` failed twice and Jev escalated at 00:44:24 (confidence
0.77 then escalate) before the image, infrastructure, and deploy waves; the orchestrator cancelled
the idle workflow job.

| Moment | Time (UTC) |
| --- | --- |
| Questions answered by the profile | 00:30:41 |
| Suite finished (230 s); autopilot approved at 0.98 | 00:37:29 |
| `build` passed | 00:38:50 |
| `test` passed at the first attempt | 00:40:20 |
| `sca` failed (attempt 1); `frontend_build` and `scanners` passed | 00:40:56 / 00:41:01 / 00:41:46 |
| `sca` failed (attempt 2); Jev escalated | 00:43:54 / 00:44:24 |

What went wrong, and the fix:

- The `sca` worker installed `semgrep==1.99.0` into the agent's system Python and then called
  `bash ci/scanners.sh` with no selection, so the contract re-ran the whole menu. Its semgrep step
  ran the venv's `semgrep` shim, which resolves `pysemgrep` on PATH and found the system install,
  which crashed on `pkg_resources`; the step wrote no report and the contract's report parser
  died with it. Now the contract runs the venv's own `python -m semgrep` (with `setuptools`
  installed), a missing or unreadable report is recorded as `error` instead of ending the script,
  and a generated scan stage is scoped to its own provider (`generator.scope_scan_contract`:
  `SCAN_TOOLS="pip-audit" bash ci/scanners.sh` for a `pip_audit` stage; the prompt says so and
  forbids a second semgrep or pip-audit in the system Python). The full menu belongs to the fixed
  `scanners` stage.
- In the `scanners` stage gitleaks did not run: `docker run` exited 125 with its output discarded,
  recorded as `error`, and the stage passed without a secret scan. The images for trivy and
  gitleaks now come from GitHub Container Registry (`ghcr.io/aquasecurity/trivy`,
  `ghcr.io/gitleaks/gitleaks`), since anonymous Docker Hub pulls from shared CodeBuild addresses
  are rate limited, and the contract keeps each docker tool's output in `var/scanners/<tool>.log`
  and prints its last lines when the tool could not run.

Nothing was deployed; the writes were the source upload, the project update, the three secret
values, and six CodeBuild builds (four passed, two failed). The GitHub job was cancelled at 00:45.

### Live run 14 (2026-11-06, 00:58–01:22 UTC, workflow run 42140636029, run c9c9bf43): seven stages passed on CodeBuild up to the image wave; the image scan failed the built image on unfixable Debian findings

Started by the push of b91d631 after a one-call probe of the OpenAI key. Questions answered at
01:01:02, nine generated stages and the suite (311 s) accepted, autopilot approved at 01:08:34
(confidence 0.98), provisioning and injection applied. Every stage up to the image wave passed at
the first attempt: `build` 01:10:04, `test` and `sca` 01:11:44 (the scoped contract:
`SCAN_TOOLS="pip-audit"`), `scanners` 01:12:59, `integration_test` 01:14:38, `infra_plan` and
`build_image` 01:17:42 (the plan-mode deploy contract validated the stack and resolved every
parameter; the image was built and pushed to ECR as `:<run id>`). `container_scan` then ran
`trivy image --severity HIGH,CRITICAL --exit-code 1` on that image: 67 findings, of which 66 were
Debian packages of the `python:3.12-slim` base with no released fix (65 HIGH, 1 CRITICAL) and one
was fixable (`setuptools 70.3.0`, CVE-2025-47273, fixed in 78.1.1). Jev escalated at 01:19:51
without a retry; the orchestrator cancelled the idle workflow job.

| Moment | Time (UTC) |
| --- | --- |
| Questions answered by the profile | 01:01:02 |
| Suite finished (311 s); autopilot approved at 0.98 | 01:08:34 |
| `build` passed | 01:10:04 |
| `test`, `sca` passed | 01:11:44 |
| `scanners` passed | 01:12:59 |
| `integration_test` passed | 01:14:38 |
| `infra_plan` (plan mode) and `build_image` passed | 01:17:42 |
| `container_scan` failed on the image; Jev escalated | 01:19:15 / 01:19:51 |

What went wrong, and the fix:

- A slim Debian base always carries vulnerabilities with no released fix, so a scan that fails
  on them can never pass. The catalog's image scan now ignores unfixable findings and fails on
  fixable HIGH/CRITICAL ones (`trivy image --ignore-unfixed`, `grype --only-fixed`), the prompt
  says so, and the one fixable finding is fixed: the Dockerfile upgrades `setuptools` past
  78.1.1 before installing the requirements.

Nothing was deployed; the writes were the source upload, the project update, the three secret
values, the image in ECR, and eight CodeBuild builds (seven passed, one failed). The GitHub job
was cancelled at 01:22.

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
