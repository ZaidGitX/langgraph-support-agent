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
| `generate(state, planner, provider, review)` | Astra → env manifest → policy → Jev review (one revision) → `generator.write` under `var/generated/<run>/pipeline/`; returns records, review, policy rows, `ok`, `stage_files` |

The tier of a cloud suite is the answered target (`dev`, `staging`, `uat`); `local` is never a
cloud tier. Templates: `Jenkinsfile.suite.tpl`, `buildspec.stage.yml.tpl`,
`cloudbuild.suite.yaml.tpl`, `azure-pipelines.suite.yml.tpl`, `codebuild-project.json.tpl`,
`jenkins-job.xml.tpl`, `ecs-stack.yaml.tpl`. New artifact kinds: `cloudformation`,
`codebuild_project`, `jenkins_job` (registry, schemas, templates, policy `kind_for_path` in
both copies).

## Astra and Jev

`OpenAIAstra.generate_suite(payload) -> PipelineSuite` with `SUITE_SYSTEM` (translate, keep
formats valid, placeholders only, no production, summary line per stage, fill `stage_files`).
Jev reviews the suite with the existing `artifact_review` edge under the pseudo-stage
`pipeline`; a `reject` (or a policy error after one revision) sets
`pipeline.suite.status` to the reason and moves the fixed stages back to `local`.

## Graph

- `generate_artifacts`: runs even without generated stages when a suite is wanted; stores the
  suite under `generated_artifacts["pipeline"]`, `artifact_review["pipeline"]`, policy rows;
  records `pipeline.suite = {platform, runner, status, stage_files}`; computes the plan-mode
  `provision` and `injection` records for the approval screen.
- `approve_pipeline` payload and `machine_report` carry `provision`, `injection`, `suite`.
- `dispatch_stages` → `_platform_setup`: after approval, `provider.provision(records, mode)`
  and `provider.inject_secrets(names, mode)` run before the first wave; plan mode is
  idempotent, an apply runs once; a failed apply escalates the gate.
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
| github_actions | unchanged | none | `gh secret set NAME --body-file -` |

`ProvisionResult` (`runner, action, status planned/pass/fail/skipped, mode, commands, names,
missing, evidence, target`) is what the state, the approval payload, and the report carry.
`AWS_CODEBUILD_ROLE_ARN` is a config name the person supplies: the orchestrator never
creates IAM. The ECS stack creates a task execution role only when none is passed.

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
