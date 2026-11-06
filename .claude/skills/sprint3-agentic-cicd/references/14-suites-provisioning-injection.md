# 14 — Platform suites, provisioning, secret injection (Sprint 4, Phase 9)

Sprint 3 pinned the fixed stages to the local host or GitHub Actions and let only generated
stages pick a runner. Phase 9 lets the whole pipeline run on one platform: Astra writes the
platform's own files for the fixed stages, the provider creates what the platform needs,
copies the secret names the files reference into the platform's store, and triggers each
stage through the platform. Everything stays plan-mode until the platform is connected and
`ORCHESTRATOR_CLOUD_APPLY=1`.

## Switch and runner plan

`ORCHESTRATOR_DISPATCH=platform` plus the answered platform (`env_platform`: jenkins, aws,
gcp, azure; `github` behaves like `github` dispatch). `graph._runner_plan` assigns the
platform's default runner to every fixed stage except `uat`, with the runner's readiness mode.
`local` and `github` dispatch are unchanged (`tests/test_suite.py::test_local_dispatch_is_unchanged`).

## `orchestrator/suite.py`

| Function | Role |
| --- | --- |
| `wants_suite(state)` | platform dispatch on and a suite platform answered |
| `suite_stages(pipeline)` | the approved fixed stages minus `uat`, in order |
| `command_hints(stage)` | portable bash per fixed stage (what the GitHub workflow does, without GitHub); scanners and DAST use the catalog's docker images with `-e NAME` for each credential name |
| `render_skeletons(platform, stages, environment, run_id, placeholders)` | keyed by artifact path: Jenkins `ci/pipeline.Jenkinsfile` (`STAGE` parameter, one `stage()` per fixed stage gated with `when`) + `deploy/jenkins-job.xml`; AWS `ci/buildspec-<stage>.yml` per stage (`buildspec.stage.yml.tpl`, Secrets Manager references by name) + `deploy/codebuild-project.json` (S3 source) + `deploy/ecs-stack.yaml` when deploy is a stage; GCP `ci/cloudbuild.yaml` (`_STAGE` substitution, `waitFor` chain); Azure `ci/azure-pipelines.yml` (`stage` parameter, `condition` per stage) |
| `context_for(state, provider)` | the `generate_suite` payload: stages with hints, the fixed workflow texts (≤ 6 000 chars each), skeletons, suggested paths, placeholders (provider group + scanner names), scanner images, readiness, policy rules |
| `check(suite)` | every artifact through `policy.check_artifact` (the suite may hold up to 14 files) |
| `generate(state, planner, provider, review)` | Astra → env manifest → policy → Jev review (one revision) → `generator.write` under `var/generated/<run>/pipeline/`; returns records, review, policy rows, `ok`, `stage_files` (derived from the written paths by `stage_file`; the model never reports a mapping, strict mode forbids one) |
| `provision_bundle(generated)` | every generated record of the run, suite first, one per path: what travels with the source upload so a platform build finds `ci/<stage>.sh` |

The tier of a cloud suite is the answered target (`dev`, `staging`, `uat`); `local` is never a
cloud tier. Templates: `Jenkinsfile.suite.tpl`, `buildspec.stage.yml.tpl`,
`cloudbuild.suite.yaml.tpl`, `azure-pipelines.suite.yml.tpl`, `codebuild-project.json.tpl`,
`jenkins-job.xml.tpl`, `ecs-stack.yaml.tpl`. New artifact kinds: `cloudformation`,
`codebuild_project`, `jenkins_job` (registry, schemas, templates, policy `kind_for_path` in
both copies).

## Astra and Jev

`OpenAIAstra.generate_suite(payload) -> PipelineSuite` with `SUITE_SYSTEM` (translate, keep
formats valid, placeholders only, no production, summary line per stage, keep the suggested paths).
`PipelineSuite` has no mapping field: OpenAI strict output rejects `dict[...]` (live run 1).
Jev reviews the suite with the existing `artifact_review` edge under the pseudo-stage
`pipeline`; a `reject` (or a policy error after one revision) sets
`pipeline.suite.status` to the reason and moves the fixed stages back to `local`; a generated
stage bound to the platform runner moves with them (`_fall_back_to_host`), and the status names
every moved stage ("on the host instead: …"). Without a suite there is no project and no upload.

## Graph

- `generate_artifacts`: runs even without generated stages when a suite is wanted; stores the
  suite under `generated_artifacts["pipeline"]`, `artifact_review["pipeline"]`, policy rows;
  records `pipeline.suite = {platform, runner, status, stage_files}`; computes the plan-mode
  `provision` and `injection` records for the approval screen. The suite is written on its own
  worker thread beside the generated stages (`ORCHESTRATOR_ASTRA_CONCURRENCY`), see
  `02-graph-and-session.md`; the plan-mode provisioning waits for every worker.
- `approve_pipeline` payload and `machine_report` carry `provision`, `injection`, `suite`.
- `dispatch_stages` → `_platform_setup`: after approval, `provider.provision(records, mode,
  bundle=suite.provision_bundle(generated_artifacts))` and `provider.inject_secrets(names, mode)`
  run before the first wave; plan mode is idempotent, an apply runs once; a failed apply
  escalates the gate. The AWS provider packs the bundle into `source.zip` at the records'
  relative paths (one per path, suite first).
- Fixed stage nodes: a runner other than `local`/`github_actions` goes through
  `dispatch.execute_stage_v2` with the suite records (`stage.plan` and `runner` on the result);
  a `planned` deploy is not "refused".
- `route_dispatch` hands the suite records to fixed stages when they have no artifacts of their
  own.

## Providers

| Provider | Stage trigger | `provision_commands` | `inject_commands` (value on stdin) |
| --- | --- | --- | --- |
| aws | `codebuild start-build --project-name $AWS_CODEBUILD_PROJECT --buildspec-override file://ci/buildspec-<stage>.yml` | `s3 cp var/generated/<run>/pipeline/source.zip s3://$AWS_S3_BUCKET/orchestrator/<run>/source.zip`, `ecr describe-repositories`, `codebuild create-project --cli-input-json file://deploy/codebuild-project.rendered.json`; `prepare_provision` zips the checkout (without var, node_modules, .git, dist) plus the suite and expands the config names in the project document | `secretsmanager put-secret-value --secret-id <app>/NAME --secret-string file:///dev/stdin` |
| jenkins | `buildWithParameters` with `RUN_ID`, `TIER`, `STAGE` | `POST $JENKINS_URL/createItem?name=$JENKINS_JOB` with `deploy/jenkins-job.xml` | `POST …/credentials/store/system/domain/_/createCredentials` (secret text, id = NAME, JSON on stdin) |
| gcp | `gcloud builds submit --config ci/cloudbuild.yaml --substitutions …,_STAGE=<stage>` | none (submit-based) | `gcloud secrets versions add NAME --data-file=-` |
| azure | `az pipelines run --parameters runId tier stage=<stage>` | none | `az pipelines variable create --name NAME --secret true --value @-` |
| github_actions | unchanged | none | `gh secret set NAME` (gh reads stdin when `--body` is absent) |

`ProvisionResult` (`runner, action, status planned/pass/fail/skipped, mode, commands, names,
missing, evidence, target`) is what the state, the approval payload, and the report carry.

Build speed (before live run 3): the CodeBuild project document enables an S3 cache at
`$AWS_S3_BUCKET/orchestrator/cache` and `timeoutInMinutes` 20; both buildspec skeletons carry a
`cache.paths` block for pip and `frontend/node_modules`, and the suite prompt asks for lean
stages (install only what the stage runs, never rebuild the frontend or the image needlessly).
`AwsProvider.timeout` is 1200 s with `POLL_SECONDS` 10: a build still `IN_PROGRESS` at the
deadline gets `codebuild stop-build` and fails the stage with "STOPPED by the orchestrator".
`scripts/aws_teardown.sh --apply` also deletes the `<app>-<tier>` stack.

Bootstrap (2026-11-04, the person runs no script): `AwsProvider.provision` in apply mode first
runs `ensure_platform(cwd, run_id)`: `sts get-caller-identity` fills `AWS_ACCOUNT_ID`; unset
names are derived (`AWS_S3_BUCKET` = `langgraph-orchestrator-<account>`, `AWS_ECR_REPOSITORY` =
the app name, `AWS_CODEBUILD_PROJECT` = `<app>-orchestrator`) and exported like the vault does;
`head-bucket` → `create-bucket` + `put-public-access-block`; `describe-repositories` →
`create-repository`; `get-role` → `create-role` (trust: codebuild.amazonaws.com), then
`put-role-policy` with `role_policy()` (the project's logs, the bucket, the repository, the
app's secrets, and the scoped CloudFormation, ECS, EC2, log-group, and task-role statements a
Fargate deploy needs), unless `AWS_CODEBUILD_ROLE_ARN` names a role of another name, which is
left alone; `AWS_CODEBUILD_ROLE_ARN` is exported for the project document. Then the upload
commands run and `_create_or_update_project` sends `create-project`, or `update-project` when
`batch-get-projects` finds it, retried while a new role propagates. Plan mode lists the
commands and notes what an apply would create. `scripts/aws_bootstrap.sh` is the manual
fallback and prints the same policy through `role_policy`. `tests/test_aws_bootstrap.py`.

Hardening of model-written files (`suite._harden`, `generator.harden`, live run 3 on
2026-11-04): the CodeBuild project document keeps `privilegedMode: true` (image stages run
docker) and its description is cut to 255 characters (CreateProject refuses more; the run's
provisioning failed on 268); a buildspec line that sets `AWS_REGION` to nothing is dropped
(it overrides the project's region), and the generated-stage skeleton no longer carries
`$REGION`. `tests/test_hardening.py`. Live run 5 (2026-11-05): a bare id under a buildspec's
`secrets-manager` block that is a registry secret name is qualified to `<app>/NAME`
(`suite._qualify_secret_ids`), so `referenced_secrets`, injection, and the build agree;
`with_manifest` also runs `generator.relocate` on every suite file. `tests/test_run5_hardening.py`.
Live run 8 (2026-11-05, the first CodeBuild builds): neither buildspec skeleton carries an
`artifacts:` block any more and `generator.harden` drops one a model writes (the project is
`NO_ARTIFACTS`; the test stage passed every phase and failed at UPLOAD_ARTIFACTS);
`AwsProvider.would_run` looks for the stage's buildspec before any provider-specific host
command, so a generated `aws_cfn`/`aws_ecs`/`ecr` stage with a buildspec runs inside CodeBuild
(infra_plan's CloudFormation command had run on the orchestrator host); `_codebuild` appends
the last 60 CloudWatch lines of a failed build (`_log_tail`, `LOG_TAIL_LINES`) to the
evidence; `deploy_defaults(tier)` exports `AWS_CFN_STACK`, `AWS_ECS_CLUSTER`, `AWS_ECS_SERVICE`
= `<app>-<tier>` when unset (ensure_platform takes the tier from `env_target`), and the project
template hands every build `AWS_ACCOUNT_ID`, `AWS_S3_BUCKET`, `AWS_ECR_REPOSITORY`, and those
three names. `tests/test_run8_hardening.py`.

Secrets, decided after live run 1: a stage file may bind only `suite.injectable_secrets`
(secret kind, a value present on the orchestrator host, never the platform's own identity
names such as `AWS_ACCESS_KEY_ID`; the CodeBuild skeleton binds scanner names only in the
stage that runs the scanner), the payload lists them as `available_secrets`, and injection
copies exactly what the written files bind (`suite.referenced_secrets`: `APP/NAME`,
`credentials('NAME')`, `secrets/NAME/versions`, `$(NAME)`). The AWS provider's
`run_injection` runs `describe-secret` first and `create-secret` when the id is absent, then
`put-secret-value`, the value always on stdin. A failed injection stops the run before the
first wave (`guardrail: secret injection failed`): every platform build would fail resolving
the missing secret otherwise.
`AWS_CODEBUILD_ROLE_ARN` is a config name the person supplies: the orchestrator never
creates IAM. The ECS stack creates a task execution role only when none is passed.

## Platform contract (Sprint 4, after live run 8, 2026-11-05)

Live run 8 showed what the model alone produces: a deploy stage returned as the untouched
skeleton, an infra stage that invented an environment name, a scanners stage that treated
every finding and every un-credentialed tool as fatal. The orchestrator now owns the platform
plumbing and Astra works on top of it.

- `orchestrator/templates/contract/*.sh`, rendered by `templates.contract_script(key)` (plain
  shell; only `__APP_NAME__`, `__PORT__`, and the scanner image tokens are replaced): `build`,
  `test`, `scanners`, `package` run on every platform's agent; `container`, `release`, `deploy`,
  `smoke`, `dast` speak to AWS (ECR, CloudFormation, ECS). Each does what the fixed GitHub
  workflow does, then what Sprint 4 needs beyond it: `container` builds and pushes
  `<registry>/<repository>:<run id>` (`WITH_CLOUD_CLIS=0`), `release` promotes that tag to
  `<tier>` through the manifest, `deploy` makes sure the image exists (else builds it), finds
  the VPC and public subnets when `AWS_VPC_ID`/`AWS_SUBNET_IDS` are empty, runs
  `aws cloudformation deploy` on `deploy/ecs-stack.yaml` with `CAPABILITY_NAMED_IAM`, waits for
  the service, checks `/status` on the task's public IP, and publishes the URL to
  `s3://<bucket>/orchestrator/<run>/state/app_url.txt`; `smoke` and `dast` read that URL (or
  rediscover it) and exercise the routes, ZAP baseline required, StackHawk when `HAWK_*` are
  provided. `scanners` runs semgrep, pip-audit, snyk, trivy fs, gitleaks, Sonar, and CodeQL
  (bundle download, time-capped) and writes `var/scanners/status.jsonl` with `passed`,
  `failed` (findings at `SCAN_SEVERITY`, default HIGH,CRITICAL), `skipped` (credential not
  provided, time cap), or `error` (the tool could not run); only `failed` fails the stage.
- `suite.contract_files(platform)` adds the scripts to every suite's skeletons (`ci/<key>.sh`);
  `command_hints(stage, platform)` returns `bash ci/<stage>.sh` for a stage that has one, so
  the Jenkins, Cloud Build, Azure, and CodeBuild skeletons call the scripts. The ECS stack
  skeleton travels with every AWS suite (not only when `deploy` is a stage).
- `suite.with_manifest(..., platform=, stages=)` → `assert_contract`: after Astra returns, the
  contract files (and the stack) are put back when changed or dropped, and each fixed stage's
  buildspec gets `bash ci/<stage>.sh` appended to `phases.build.commands` when the model dropped
  it (`_with_build_command`). Astra adds steps around the call; it never owns the plumbing.
- `generator.platform_floor` (through `with_env_manifest`) gives a generated stage on
  `aws_codebuild` of family deploy / verify / container (or a name with deploy, smoke, or
  _image) the matching contract call when its buildspec is missing, still the skeleton
  placeholder, or does not do the family's job (`_FLOOR_MARKERS`).
- `policy.placeholder_findings`: the skeleton's no-op command (`echo "stage X placeholder"`)
  is an `artifact.placeholder` error in `check_artifact_set` and `suite.check`; the `--- ASTRA:`
  comments may stay. Skeletons themselves are never checked for it.
- Secrets: `graph._bound_secret_names(bundle)` injects every name any file of the run binds
  (the suite, a generated stage, the ECS task's `Secrets`), from content and from the records.
  The stack's task execution role is named `<app>-<tier>-task-execution` (inside the CodeBuild
  role's `role/<app>-*` statement) and may read `<app>/*` in Secrets Manager;
  `suite._container_secret_lines` binds `OPENAI_API_KEY`, `TYPESAFE_API_KEY`,
  `LANGSMITH_API_KEY` when present on the orchestrator host (`APP_TASK_SECRETS`).
- Account: `ensure_platform` → `discover_network` exports `AWS_VPC_ID` and `AWS_SUBNET_IDS`
  (registry config names) from the default VPC when unset; the project document hands every
  build the account, bucket, repository, stack, cluster, service, VPC, and subnets, runs on
  `BUILD_GENERAL1_MEDIUM` with `timeoutInMinutes` 30 (`AwsProvider.timeout` 1800), and the
  role may create report groups (`codebuild:CreateReportGroup` …) and describe or re-tag
  images. `prepare_provision` never packs `.env`, `.env.*` (examples excepted), key files, or
  `secrets.enc` (`_holds_credentials`).
- `scripts/aws_teardown.sh` also removes the project's report groups and, with `--all`, the
  `<app>/*` secrets. `tests/test_run9_contract.py`.

## Policy

`secret.assignment` exempts by-reference bindings in both copies (`_SECRET_REFERENCE`):
Jenkins `credentials('NAME')`, CodeBuild `"<secret-id>:<KEY>"` and parameter-store `"/path"`
references, Azure `$(NAME)`. A literal still fails (`tests/test_policy.py`).

## UI

`components/PlatformPlan.jsx` on the approval and results screens: platform and runner, the
file per stage, provisioning (status, mode, commands, evidence), injection (names, target).
The suite appears as the `pipeline` tab of the artifact viewer.

## Tests (`tests/test_suite.py`)

Skeletons for every platform stay policy-clean; an AWS platform run uses the suite and plans
without credentials (runner per stage, `--buildspec-override`, provisioning commands,
`planned_stages`); a Jenkins run triggers one job per stage with `STAGE`; local dispatch is
unchanged; a rejected suite falls back to the host; injection plans for every provider never
carry a value; provisioning is skipped without a document and planned with one.

Still open for the live proof (phase 12): the orchestrator does not create the S3 bucket,
the ECR repository, or the CodeBuild service role; those are the user's, named in the
registry, and every apply is carded first.

## The scan menu in a suite (Sprint 4, run 9)

`suite.command_hints(stage, platform, answers)` puts the selection in front of the contract
call for the `scanners` and `dast` stages (`suite.selection_prefix`:
`SCAN_TOOLS="semgrep,pip-audit" SCAN_SEVERITY="HIGH,CRITICAL" bash ci/scanners.sh`,
`DAST_TOOLS="zap" bash ci/dast.sh`), so the Jenkinsfile, every CodeBuild buildspec, the Cloud
Build step, and the Azure stage carry the person's menu; `render_skeletons(..., answers=)` and
`with_manifest(..., answers=)` / `assert_contract` thread it, and a restored call carries the
selection too. `SCANNER_TOOLS` lists every provider of the menu, so a provided `SONAR_*`,
`SNYK_TOKEN`, or `HAWK_*` name joins the placeholders and `available_secrets`. The contract
scripts `ci/scanners.sh` and `ci/dast.sh` read `SCAN_TOOLS`, `SCAN_SEVERITY`, `DAST_TOOLS`
(`all` by default, `none` allowed) and record an unselected or credential-less provider as
`skipped`, never failed; the stage fails only on a `failed` line.

## What injection copies (after live run 9)

Live run 9 stopped at injection with `no value for AWS_SESSION_TOKEN`: the names were the union
of every generated record's declared `secrets_referenced`, and generated stages had declared the
orchestrator's own AWS identity in `.env.dev.example` manifests, a script, and a GitHub workflow.
Rules now: `credentials.identity_secrets(group)` (the group's secret-kind names) are never
placeholders for a stage that runs inside that platform (`generator.placeholders_for`,
`suite.context_for`) and never injectable (`suite.injectable_secrets`);
`suite.bound_secret_names(records, platform)` takes explicit bindings in any file's content plus
the declared names of records the platform itself resolves (`PLATFORM_BOUND_KINDS`: AWS
buildspecs, the stack, the project document; Jenkinsfile and job XML; cloudbuild; azure
pipeline; on GitHub the workflows); `generator.harden` drops a `secrets-manager` entry for an
identity secret or a registry secret without a value (`strip_unresolvable_bindings`); and
`profile.approval` refuses a plan whose injection plan has a bound name without a value, so the
session rejects it once with that feedback. Tests: `tests/test_run10_injection.py`.

**Live run 18 (2026-11-06).** The project document Astra wrote listed nine environment names and
dropped `AWS_ECR_REPOSITORY`, `AWS_CFN_STACK`, `AWS_ECS_CLUSTER`, and `AWS_ECS_SERVICE`; the
skeleton-only `build_image` buildspec (run 17's rule) declares none of them, so `ci/container.sh`
stopped on the missing repository. Rules now: `deploy/codebuild-project.json` is the
orchestrator's like the stack and the contract scripts (`suite.assert_contract` restores it from
the template); `templates.CODEBUILD_ENV_NAMES` is the one list of build names and a test holds the
template to it; `AwsProvider.would_run` adds a JSON `--environment-variables-override` carrying
RUN_ID, PLATFORM, TIER, APP_NAME, and each AWS_* config name as a `$NAME` placeholder, which
`apply` expands from the host (after `ensure_platform` exported them) and `resolve_build_env`
trims of unresolved entries (JSON because `AWS_SUBNET_IDS` holds commas); `SUITE_SYSTEM` says the
project document is the orchestrator's. Tests: `tests/test_run18_build_env.py`.

**Live run 19 (2026-11-06).** The run 18 fix held (every build carried the thirteen names), but
the restored project document had been rendered with `environment["run_id"]`, which the suite
context does not carry: its source location said `orchestrator/local/source.zip` while the upload
went to the run's key, and both builds failed at DOWNLOAD_SOURCE. Rules now: `with_manifest` and
`assert_contract` take `run_id` (`suite.generate` passes the state's; the environment, then the
answers, are fallbacks), and `AwsProvider.pin_project_document` rewrites the rendered document at
provision from the run and the host (project name, the exact upload key, `CODEBUILD_ENV_NAMES`
values, a name without a value left out), so the stored document and the upload cannot disagree.
Tests: `tests/test_run19_source_key.py`.

**Live run 20 (2026-11-06).** Seven stages passed at the first attempt (build, test, infra_plan,
scanners, integration_test, build_image, container_scan) and the deploy ran for the first time.
The stack rolled back: the redaction every stored artifact passes through (`generator.write`) had
an unbounded `sk-` key pattern that turned `task-execution` into `ta[redacted]` in the stack's
`RoleName`; the retry then could not update a ROLLBACK_COMPLETE stack. Rules now: the key pattern
needs a token boundary and a key's length; the assignment pattern leaves shell default expansions
(`${NAME:-}`, `:=`, `:?`, `:+`) alone (the stored `dast.sh` had lost its `-}`); every contract
script, skeleton, and the stack are tested to survive `redaction.redact` unchanged; `deploy.sh`
creates a new stack with `create-stack --on-failure DELETE` (a failed create removes itself and
prints its CREATE_FAILED events from the stack id), updates an existing one with `cloudformation
deploy`, and never deletes a stack: a stack left in a failed state is reported, and the person
removes it with `scripts/aws_teardown.sh` before the next run. The destructive-command guard is
unchanged. Tests: `tests/test_run20_deploy_recovery.py`.
