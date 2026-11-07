# Live run 25 report (2026-11-07, run d44ee711-4715-4f92-9b10-22cc6217a569)

The first clean run from a push to a deployed dev service and a human UAT sign-off. Workflow run
42338935411 on push 945c2a0. This is the orchestrator's own report (`report_markdown`), copied from
the session at 05:20 UTC; names only, no values.

```text
Agentic CI/CD run d44ee711-4715-4f92-9b10-22cc6217a569
Status: passed
Astra (planner): gpt-6-astra
Checkpointer: sqlite. Aurora PostgreSQL is the deferred production binding.
AgentCore, OpenSearch, ECR, ECS, and S3 were not called.
Past UAT: false. This graph has no production edge.

Repo
  root: /workspace
  languages: Python, JavaScript
  build tools: pip, pytest, npm, docker, github-actions
  dependency signals: Dockerfile, docker-compose.yml, frontend/package.json, requirements-dev.txt, requirements.txt

Ecosystem
  retrieval: Local BM25 index e9ce3418c089 over 204 files and 757 chunks (bm25). graphify and OpenSearch are deferred and were not queried.
  entry points: app/main.py, tests/test_app.py, mcp_server/server.py, Dockerfile, frontend/package.json, frontend scripts: build, dev
  dast: DAST runs only when that stage is in the approved pipeline. .github/workflows/dast.yml smokes the Compose app URL, then runs OWASP ZAP and StackHawk. A push does not start that workflow.

Readiness (names only)
  dispatch=platform cloud_apply=True embeddings=bm25
  github: mode=apply cli=present missing=none
  aws: mode=apply cli=present missing=none
  gcp: mode=plan cli=present missing=GCP_PROJECT_ID, GOOGLE_APPLICATION_CREDENTIALS
  azure: mode=plan cli=present missing=AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_CLIENT_SECRET, AZURE_SUBSCRIPTION_ID
  jenkins: mode=plan missing=JENKINS_URL, JENKINS_USER, JENKINS_API_TOKEN, JENKINS_JOB
  docker: mode=plan cli=absent missing=none
  ssh: mode=plan cli=absent missing=SSH_HOST, SSH_USER, SSH_KEY_PATH
  kubernetes: mode=plan cli=present missing=KUBECONFIG, K8S_NAMESPACE
  scanners: mode=apply missing=none
  signing: mode=apply missing=none
  git: mode=apply cli=present missing=none

Answers
  env_target: dev
  env_platform: aws
  test_depth: unit and integration
  scan_tools: semgrep, pip-audit, snyk, trivy, gitleaks
  dast_tools: zap
  release_policy: deploy the dev tier on ECS Fargate from the generated CloudFormation stack, creating the cluster and the service when they do not exist yet; manual release after UAT sign-off

Candidates
  - fixed_dev_readiness [deterministic p=0.00]: build, test, scanners, package, container, release, deploy, dast, uat
  - generated_ecs_dev [stochastic, chosen p=1.00]: build, test, integration_test, scanners, build_image, container_scan, deploy_dev, post_deploy_smoke, dast, uat

Chosen pipeline
  id: generated_ecs_dev-1
  summary: Generated integration tests, image security gate, and CloudFormation-backed ECS Fargate bootstrap or update.
  - build [fixed, runner aws_codebuild] after [none]
  - scanners [fixed, runner aws_codebuild] after [build]
  - test [fixed, runner aws_codebuild] after [build]
  - integration_test [generated, runner aws_codebuild] after [test]
  - build_image [generated, runner aws_codebuild] after [integration_test, scanners]
  - container_scan [generated, runner aws_codebuild, provider trivy] after [build_image]
  - deploy_dev [generated, runner aws_codebuild, provider aws_ecs] after [container_scan]
  - post_deploy_smoke [generated, runner aws_codebuild] after [deploy_dev]
  - dast [fixed, runner aws_codebuild] after [post_deploy_smoke]
  - uat [fixed, runner local] after [dast]

Workers (Astra's sub-agents)
  - build: gpt-6.1-terra at minimal effort
  - scanners: gpt-6.1-sol at low effort
  - test: gpt-6.1-terra at minimal effort
  - integration_test: gpt-6.1-sol at medium effort
  - build_image: gpt-6.1-sol at medium effort
  - container_scan: gpt-6.1-sol at medium effort
  - deploy_dev: gpt-6-astra at high effort
  - post_deploy_smoke: gpt-6.1-sol at medium effort
  - dast: gpt-6.1-sol at low effort
  - uat: gpt-6.1-sol at low effort
  - pipeline: gpt-6-astra at high effort

Generated artifacts: every file policy ok=True, review=accept, revisions=0
  integration_test, build_image, container_scan, deploy_dev, post_deploy_smoke: one buildspec each (on CodeBuild the orchestrator's skeleton calling the contract), an env manifest (names only), the deploy doc and a smoke script kept as evidence
  pipeline: ci/buildspec-{build,scanners,test,dast}.yml, deploy/codebuild-project.json, deploy/ecs-stack.yaml, the contract scripts ci/{build,test,scanners,dast,package,container,release,deploy,smoke,image_scan}.sh, docs/deploy/aws.md, .env.dev.example

Policy findings
  no findings

Platform
  suite for aws via aws_codebuild (ok)
  provision: pass (apply) missing=none
  injection: pass (apply) names=HAWK_API_KEY, OPENAI_API_KEY, SNYK_TOKEN, SONAR_TOKEN, TYPESAFE_API_KEY missing=none

Stage evidence (every stage at its first attempt)
  - build attempt 1 pass exit 0 runner aws_codebuild
  - scanners attempt 1 pass exit 0 runner aws_codebuild
  - test attempt 1 pass exit 0 runner aws_codebuild
  - integration_test attempt 1 pass exit 0 runner aws_codebuild
  - build_image attempt 1 pass exit 0 runner aws_codebuild
  - container_scan attempt 1 pass exit 0 runner aws_codebuild
  - deploy_dev attempt 1 pass exit 0 runner aws_codebuild
  - post_deploy_smoke attempt 1 pass exit 0 runner aws_codebuild
  - dast attempt 1 pass exit 0 runner aws_codebuild
  - uat attempt 1 pass exit 0 runner local

Quality gate
  route: pass, retry: False (TypeSafe Choice route=pass confidence=0.870; model=jev-1.14.0)

Approvals
  pipeline: approved (autopilot, confidence 1.00)
  deploy: pending
  uat: approved (a person, in the dashboard)

Trace
  checkout → discover_ecosystem → clarify_with_user → follow_up → plan_pipeline → generate_artifacts → approve_pipeline → dispatch_stages → build → quality_gate → dispatch_stages → scanners → test → quality_gate → dispatch_stages → integration_test → quality_gate → dispatch_stages → build_image → quality_gate → dispatch_stages → container_scan → quality_gate → dispatch_stages → deploy_dev → quality_gate → dispatch_stages → post_deploy_smoke → quality_gate → dispatch_stages → dast → quality_gate → dispatch_stages → uat → quality_gate → report
```

Jev's routing decisions (every conditional edge one TypeSafe `Choice`/`Noul` call, model jev-1.14.0):
clarify `ask` 1.00; follow_up `proceed` 0.56; rank `generated_ecs_dev` 1.00; runner `aws_codebuild` for
every generated stage (0.82 to 0.92); artifact_review `accept` six times (0.89 to 0.98); quality
gate `pass` nine times (0.64 to 0.97).

| Moment | Time (UTC) |
| --- | --- |
| Push of 945c2a0; workflow started | 04:32:56 |
| Questions answered by the profile | 04:36:17 |
| Suite done; autopilot approved at 1.00 | 04:41:37 |
| `build` passed | 04:44:05 |
| `test`, `scanners` passed | 04:46:28, 04:46:39 |
| `integration_test` passed | 04:49:02 |
| `build_image` passed | 04:51:55 |
| `container_scan` passed | 04:54:18 |
| `deploy_dev` passed (stack CREATE_COMPLETE, service steady, `/status` answered) | 04:58:02 |
| `post_deploy_smoke` passed | 04:59:53 |
| `dast` passed | 05:02:26 |
| UAT gate opened for the person | 05:02:29 |
| UAT signed off in the dashboard | 05:19:33 |
| Report written, status passed; results viewed | 05:20:01 |

Deployed: `http://98.82.28.19:8000` (ECS Fargate, cluster and service `langgraph-support-app-dev`,
stack `langgraph-support-app-dev`), image `langgraph-support-app:d44ee711-4715-4f92-9b10-22cc6217a569`.
