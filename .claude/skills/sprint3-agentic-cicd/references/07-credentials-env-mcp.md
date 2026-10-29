# 07 — Credential placeholders, readiness, redaction, and the MCP manifest (Phase 3)

One registry names every environment variable any provider, scanner, or the app may need.
Everything else derives from it: `.env.example`, the compose pass-through block, the
orchestrator workflow's secrets block, the readiness panel, Astra's placeholder list, and
redaction. Values are never read into state, logs, prompts, or files; only names and
presence booleans are.

## `orchestrator/credentials.py`

Source of truth: `assets/credentials.json` in this skill. Copy it to
`orchestrator/credentials.json` (the code reads its own copy; keep both identical; a test
compares them when the skill directory exists).

```python
def registry() -> dict                       # the parsed JSON
def all_names() -> list[str]                 # every env name, sorted, deduped
def names_for(key: str) -> list[str]         # provider / scanner / app key -> required names
def placeholders_for(key: str) -> list[str]  # required + optional names Astra may reference
def is_present(name: str) -> bool            # after config.load_env_files(); value never returned
def readiness(provider_statuses: dict | None = None) -> dict
def render_env_example() -> str              # deterministic text for .env.example
```

`readiness()` output (names only):
```json
{"generated_at": "2026-10-28T19:05:00Z",
 "app": {"present": ["OPENAI_API_KEY", "TYPESAFE_API_KEY"], "missing": []},
 "providers": {"aws": {"cli": true, "cli_path": "/opt/homebrew/bin/aws", "present": [], "missing": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION", "AWS_CODEBUILD_PROJECT"], "mode": "plan"},
               "gcp": {"cli": false, ...}, "azure": {...}, "jenkins": {...}, "github": {...}, "docker": {...}, "ssh": {...}, "kubernetes": {...}},
 "runners": {"local": {"cli": true, "missing": [], "mode": "apply"}, "aws_codebuild": {"cli": true, "missing": [...], "mode": "plan"}, ...},
 "scanners": {"trivy": {"cli": false, "docker_image": "aquasec/trivy:0.58.0", "env_missing": []}, "snyk": {"cli": false, "env_missing": []}, ...},
 "toggles": {"ORCHESTRATOR_DISPATCH": "local", "ORCHESTRATOR_CLOUD_APPLY": false, "ORCHESTRATOR_WRITE_ARTIFACTS": false, "ORCHESTRATOR_EMBEDDINGS": "bm25", "ORCHESTRATOR_MCP": false}}
```
Alternative groups (`alt_groups` in the JSON) let one of several names satisfy a slot, for
example `AWS_PROFILE` instead of the key pair, `GCLOUD_SA_KEY` instead of
`GOOGLE_APPLICATION_CREDENTIALS`, `GH_TOKEN` instead of `GITHUB_TOKEN`.

## Names by provider (what `assets/credentials.json` holds)

| Key | Required names | Optional / alternatives |
| --- | --- | --- |
| `app` | `OPENAI_API_KEY`, `TYPESAFE_API_KEY` | `OPENAI_MODEL`, `TYPESAFE_MODEL`, `LANGSMITH_API_KEY` |
| `github` | `GITHUB_TOKEN` (alt `GH_TOKEN`), `GITHUB_REPOSITORY` | `GITHUB_REF`, `GITHUB_REF_NAME`, `GITHUB_SHA` |
| `aws` | `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` (alt `AWS_PROFILE`), `AWS_REGION` | `AWS_SESSION_TOKEN`, `AWS_ACCOUNT_ID`, `AWS_CODEBUILD_PROJECT`, `AWS_ECR_REPOSITORY`, `AWS_ECS_CLUSTER`, `AWS_ECS_SERVICE`, `AWS_S3_BUCKET`, `AWS_CFN_STACK` |
| `gcp` | `GOOGLE_APPLICATION_CREDENTIALS` (alt `GCLOUD_SA_KEY`), `GCP_PROJECT_ID` | `GCP_REGION`, `GCP_ARTIFACT_REPO`, `GCP_CLOUD_RUN_SERVICE`, `GCS_BUCKET` |
| `azure` | `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_SUBSCRIPTION_ID` | `AZURE_RESOURCE_GROUP`, `AZURE_LOCATION`, `AZURE_ACR_NAME`, `AZURE_CONTAINER_APP`, `AZURE_DEVOPS_ORG_URL`, `AZURE_DEVOPS_PROJECT`, `AZURE_DEVOPS_EXT_PAT`, `AZURE_PIPELINE_ID` |
| `jenkins` | `JENKINS_URL`, `JENKINS_USER`, `JENKINS_API_TOKEN`, `JENKINS_JOB` | — |
| `docker` | — (daemon) | `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`, `REGISTRY_URL`, `GHCR_TOKEN`, `ORCHESTRATOR_DOCKER_IMAGE` |
| `ssh` | `SSH_HOST`, `SSH_USER`, `SSH_KEY_PATH` | `SSH_PORT` |
| `kubernetes` | `KUBECONFIG` (alt: `~/.kube/config` exists), `K8S_NAMESPACE` | `K8S_CONTEXT` |
| scanners | `SONAR_TOKEN`+`SONAR_ORGANIZATION`+`SONAR_PROJECT_KEY` (sonar), `SNYK_TOKEN` (snyk), `HAWK_API_KEY`+`HAWK_APP_ID` (stackhawk) | `SEMGREP_APP_TOKEN`, `TRIVY_TOKEN`, `GITLEAKS_LICENSE`, `NVD_API_KEY` |
| signing | — | `COSIGN_KEY`, `COSIGN_PASSWORD` |
| toggles | — | `ORCHESTRATOR_DISPATCH`, `ORCHESTRATOR_AUTOSTART`, `ORCHESTRATOR_DB`, `ORCHESTRATOR_CLOUD_APPLY`, `ORCHESTRATOR_WRITE_ARTIFACTS`, `ORCHESTRATOR_EMBEDDINGS`, `ORCHESTRATOR_MCP`, `ORCHESTRATOR_ASTRA_TEMPERATURE`, `APP_PUBLIC_URL` |

Only `kind: "secret"` names are redacted by value; `kind: "config"` names (models, regions,
project ids) are not secrets but are still never echoed by the hooks.

## `orchestrator/redaction.py` (new, shared)

```python
SECRET_NAMES = [n for n in credentials.all_names() if credentials.kind_of(n) == "secret"]
PATTERNS = [sk-…, AKIA[0-9A-Z]{16}, ASIA[0-9A-Z]{16}, gh[pousr]_[A-Za-z0-9]{20,}, github_pat_[A-Za-z0-9_]{20,},
            AIza[0-9A-Za-z_-]{35}, ya29\.[A-Za-z0-9_-]+, xox[baprs]-[A-Za-z0-9-]+, hawk\.[A-Za-z0-9]{8,}\.[A-Za-z0-9_-]{8,},
            snyk_uat\.[A-Za-z0-9._-]+, eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,},
            -----BEGIN [A-Z ]*PRIVATE KEY-----, (?i)(api[_-]?key|token|secret|password)\s*[=:]\s*\S+]
def redact(text: str, limit: int | None = None) -> str
```
`llm.scrub` and `runner.redact_secrets` become thin wrappers over `redaction.redact` (keep
their names; tests import them). `local_ops.secrets()` reuses `PATTERNS` for its file scan.

## Files to regenerate from the registry

- `.env.example`: `credentials.render_env_example()` output, grouped by key with a comment
  line per group, every value empty. A test asserts the keys in `.env.example` equal
  `all_names()`.
- `docker-compose.yml` `environment:` block: one `NAME: ${NAME:-}` line per name (plus the
  existing ones). Add a commented volume line
  `# - /var/run/docker.sock:/var/run/docker.sock  # enable for the docker runner inside compose`.
- `.github/workflows/orchestrator.yml` `env:` block: `NAME: ${{ secrets.NAME }}` for every
  `kind: secret` cloud and scanner name (missing GitHub secrets resolve to empty, which is plan
  mode). Keep `permissions: contents: read` unless the demo publishes generated workflows to a
  branch, in which case add `contents: write` with a comment.
- `README.md` / `docs/ORCHESTRATOR_RUNBOOK.md`: a "Placeholders" section listing where each
  group is set (local `.env`, GitHub secrets, CI variables).

## Readiness in the graph and the API

- `checkout` stores `readiness` in state; `session.view["readiness"]` mirrors it.
- `GET /api/orchestrator/readiness` returns `credentials.readiness(providers.detect_all())`
  without starting a session (so the UI can show it on the idle screen).
- Readiness never includes values. The hooks in this skill block commands that would print
  them.

## MCP manifest (`assets/mcp_servers.json` → `orchestrator/mcp_servers.json`)

```json
{"servers": {
  "aws-mcp": {"transport": "stdio", "command": "uvx",
              "args": ["mcp-proxy-for-aws@latest", "https://aws-mcp.us-east-1.api.aws/mcp", "--metadata", "INSTALL_SOURCE=orchestrator"],
              "env_names": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION", "AWS_PROFILE"],
              "use": "read-only discovery: aws___search_documentation, aws___read_documentation, aws___list_regions, aws___get_regional_availability"},
  "github": {"transport": "stdio", "command": "docker",
             "args": ["run", "-i", "--rm", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN", "ghcr.io/github/github-mcp-server"],
             "env_names": ["GITHUB_PERSONAL_ACCESS_TOKEN"], "use": "list workflow runs, read job logs"},
  "gcp": {"transport": "stdio", "command": "npx", "args": ["-y", "@google-cloud/mcp-server@latest"], "env_names": ["GOOGLE_APPLICATION_CREDENTIALS", "GCP_PROJECT_ID"], "use": "placeholder; verify the package before enabling"},
  "azure": {"transport": "stdio", "command": "npx", "args": ["-y", "@azure/mcp@latest", "server", "start"], "env_names": ["AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_SECRET", "AZURE_SUBSCRIPTION_ID"], "use": "placeholder; verify the package before enabling"},
  "jenkins": {"transport": "stdio", "command": "uvx", "args": ["jenkins-mcp-server@latest"], "env_names": ["JENKINS_URL", "JENKINS_USER", "JENKINS_API_TOKEN"], "use": "placeholder; verify the package before enabling"}
}}
```

`orchestrator/mcp_bus.py` (optional, `ORCHESTRATOR_MCP=1`): `load_manifest()`,
`async def tools_for(names: list[str])` using `langchain_mcp_adapters.client.MultiServerMCPClient`
(already pinned: 0.3.2; `client = MultiServerMCPClient({name: {"transport": "stdio",
"command": ..., "args": ..., "env": {...names present...}}}); tools = await client.get_tools()`).
Only servers whose `env_names` are all present are started. The graph uses it in
`discover_ecosystem` for read-only lookups (documentation excerpts for the chosen provider,
region lists) with a 20 s budget; any failure is one trace line, never an exception. Nothing
in the graph applies changes through MCP.

For the coding agents: the `aws-mcp` server is configured in Claude Code and in Cursor, and
the GitHub MCP server in Cursor. Use them while implementing providers to read documentation
(`aws___search_documentation "codebuild start-build buildspec-override"`), never to create
resources.

## Tests (`tests/test_credentials.py`)

- `all_names()` has no duplicates and every name matches `^[A-Z][A-Z0-9_]+$`.
- `readiness()` with every secret name deleted shows `aws.mode == "plan"` and `local` apply.
- `readiness()` output serialized to JSON contains no value of a set variable (set a fake
  `AWS_SECRET_ACCESS_KEY=fake-value-xyz` with monkeypatch and assert the string is absent).
- `render_env_example()` keys equal `all_names()`; the committed `.env.example` matches.
- `redact("AKIA" + "0" * 16)` → `[redacted]`; a JWT and a `github_pat_` are redacted. (Build
  secret-shaped fixtures at runtime like this; a literal on one line trips Gitleaks in CI.)
- `orchestrator/credentials.json` equals the skill asset when `.claude/skills/...` exists.
