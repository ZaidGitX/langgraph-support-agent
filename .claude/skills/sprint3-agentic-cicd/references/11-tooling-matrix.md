# 11 — Tooling matrix: CLIs, SDKs, MCP servers, and dry-run rules

Verified on this machine on 2026-10-28. Re-run `scripts/preflight.py` to refresh.

## Interpreters and package managers

| Tool | Path / version | Use |
| --- | --- | --- |
| Project Python | `/opt/anaconda3/envs/tcs_practice_env/bin/python` (3.14.6) | all `pytest`, `ruff`, `python -m orchestrator.*`, `uvicorn` |
| Packages in that env | langgraph 1.2.18, langgraph-checkpoint-sqlite 3.1.2 (langgraph-checkpoint 4.3.1), langchain-core 1.6.9, langchain-openai 1.4.1, langchain-typesafe 0.0.1a3, langchain-mcp-adapters 0.3.2, mcp 1.30.0, openai 2.52, fastapi 0.142, pydantic 2.13, httpx 0.28, PyYAML 6.0.3, numpy 2.5, tiktoken 0.13, sqlite-vec 0.1.9, pytest 9.1, pytest-cov, ruff 0.16.2, pip-audit 2.10 | retrieval, providers, tests |
| System `python3` | `/opt/anaconda3/bin/python3` (3.13) | hooks and skill scripts only (stdlib) |
| `node` / `npm` / `npx` | 26.11 / 11.20 | `frontend` build and dev server |
| `uv` / `uvx` | 0.13.6 | MCP proxies, one-off tools (`uvx --from semgrep semgrep`, `uvx actionlint` is not a Python tool; see below) |
| `conda` | present (`conda activate tcs_practice_env`) | the runbook's shell |

New Python dependencies for Sprint 3: none required. PyYAML is already installed and may be
added to `requirements.txt` (`PyYAML==6.0.3`) for workflow validation. Do not add boto3,
google-cloud, or azure SDKs; providers call the CLIs so every platform follows one pattern
and the Docker image stays small.

## CLIs

| CLI | Installed | Credentials | Provider | Install (if missing) | Read-only commands allowed from a coding agent |
| --- | --- | --- | --- | --- | --- |
| `aws` 2.38.6 | yes | **none** (`aws sts get-caller-identity` → NoCredentials; `~/.aws/config` exists, no profile) | `aws_codebuild`, `aws_ecs`, `aws_cfn`, `aws_s3` | — | `aws sts get-caller-identity`, `aws codebuild list-projects`, `aws ecr describe-repositories`, `aws ecs describe-services`, `aws configure list` (never `configure get *secret*`) |
| `gh` 2.103 | yes | keyring token invalid | `github_actions` | — | `gh run list`, `gh run view`, `gh workflow list`; `gh auth refresh -h github.com` is the user's step |
| `docker` 29 / compose 5.5 | yes | daemon socket (user session) | `docker`, local image builds | — | `docker info`, `docker images`, `docker compose config` |
| `kubectl` | yes | no kubeconfig | `kubernetes` | — | `kubectl version --client`, `kubectl apply --dry-run=client` |
| `hawk` 6.6 (StackHawk CLI) | yes | `HAWK_API_KEY`, `HAWK_APP_ID` | `stackhawk` scanner | — | `hawk --version` |
| `gcloud` | no | — | `gcp_cloudbuild`, `gcp_cloud_run` | `brew install --cask google-cloud-sdk` | `gcloud config list`, `gcloud builds list` |
| `az` | no | — | `azure_pipelines`, `azure_container_apps` | `brew install azure-cli`; `az extension add --name azure-devops` | `az account show`, `az pipelines list` |
| Jenkins | no CLI needed (REST) | `JENKINS_URL`, `JENKINS_USER`, `JENKINS_API_TOKEN` | `jenkins` | local demo: `docker run -d -p 8080:8080 jenkins/jenkins:lts` | `GET $JENKINS_URL/api/json` |
| `terraform` | no | — | `infra_plan` / `infra_apply` | `brew install terraform` | `terraform plan` |
| `semgrep` | no | optional `SEMGREP_APP_TOKEN` | `sast` | `uvx --from semgrep semgrep --version` or `brew install semgrep` | `semgrep scan --config p/python` |
| `trivy` | no | optional `TRIVY_TOKEN` | `container_scan`, `sca`, `iac_scan` | `brew install trivy` or image `aquasec/trivy` | `trivy fs .` |
| `grype` / `syft` | no | — | `sca`, `sbom` | `brew install grype syft` | `syft dir:.` |
| `gitleaks` | no | — | `secrets_scan` | `brew install gitleaks` | `gitleaks detect --no-git --redact` |
| `snyk` | no | `SNYK_TOKEN` | `sca` | `npm i -g snyk` | `snyk test` |
| `codeql` | no (GitHub-hosted only) | — | `sast` in `scanners.yml` | — | — |
| `zap` | no | — | `dast_scan` | image `ghcr.io/zaproxy/zaproxy:stable` | baseline scan against the tunnel URL |
| `hadolint`, `actionlint`, `shellcheck` | no | — | `dockerfile_lint`, `workflow_lint` | `brew install hadolint actionlint shellcheck` or images `hadolint/hadolint`, `rhysd/actionlint`, `koalaman/shellcheck` | lint only |
| `k6` | no | — | `perf_test` | `brew install k6` | `k6 run` |
| `cloudflared` | no (installed by the push workflow) | — | tunnel for DAST | not needed locally | — |
| `jq` 1.8 | yes | — | hooks and scripts | — | — |

A provider whose CLI is absent reports `cli: false` and plans; it never tries to install
anything. Install hints are for the user, printed by `preflight.py`.

## MCP servers

| Server | Where configured | Transport | Needs | Use while coding | Use at runtime (`ORCHESTRATOR_MCP=1`) |
| --- | --- | --- | --- | --- | --- |
| `aws-mcp` | Claude Code (`~/.claude.json`) and Cursor (`~/.cursor/mcp.json`) via `uvx mcp-proxy-for-aws@latest https://aws-mcp.us-east-1.api.aws/mcp` | stdio proxy to HTTP | AWS credentials for most tools; docs search works without | `aws___search_documentation`, `aws___read_documentation`, `aws___list_regions` to verify CLI flags while writing `providers/aws.py` | read-only discovery in `discover_ecosystem` |
| `github` | Cursor (`docker run ghcr.io/github/github-mcp-server`) | stdio | `GITHUB_PERSONAL_ACCESS_TOKEN` | list workflow runs, read logs when debugging dispatch | optional |
| `gcp`, `azure`, `jenkins` | not configured; placeholders in `assets/mcp_servers.json` | stdio | provider credentials | verify the package name before enabling | optional |

The Claude Code session that builds this sprint may see the `aws-mcp` server fail to connect
(it did on 2026-10-28 with "Invalid request parameters"). Treat that as unavailable for the
session and fall back to the CLI's `--help` and the AWS docs pages fetched directly.

## Dry-run rules for coding agents (enforced by `hooks/guard_bash.py`)

- Allowed without asking: read-only commands in the table above, `pytest`, `ruff`, `npm run
  build`, `docker compose config`, `kubectl --dry-run=client`, `terraform plan` with no
  backend.
- Ask first (hook returns `ask`): anything that creates, updates, or starts cloud resources
  (`aws codebuild start-build`, `aws ecs update-service`, `gcloud builds submit`,
  `az pipelines run`, `gh workflow run`, `docker push`, `kubectl apply` without dry-run,
  `terraform apply`), even with `ORCHESTRATOR_CLOUD_APPLY=1`.
- Denied: destructive commands, production targets, printing secrets (`cat .env`,
  `printenv`, `echo $OPENAI_API_KEY`), `gh secret set` with a literal value.
- The AWS CLI on this machine may be used to **test the provider's plan path and read-only
  calls only** (the user's instruction); do not configure credentials or create resources.

## Sandbox notes (Claude Code desktop)

- Bash runs in a seatbelt sandbox: writes are limited to the project, `$TMPDIR`, and
  `/tmp/claude`; `.claude/skills`, `.claude/hooks`, and settings files are read-only from
  Bash (use the Write/Edit tools for skill files; hooks are installed by
  `scripts/install_hooks.py` run by the user or outside the sandbox).
- Network egress is allow-listed per command; the EC2 metadata endpoint
  (`169.254.169.254`) is always blocked, which is why `aws` prints IMDS noise; set
  `AWS_EC2_METADATA_DISABLED=true` in the shell to silence it.
- `open` and `osascript` are blocked; give the user the URL instead of opening it.
