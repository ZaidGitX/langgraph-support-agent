# 06 — Providers and runners (Phase 4)

A **runner** is where a stage executes. A **provider** is the adapter that renders artifacts
for that runner, detects whether it can run now, and executes in `plan` or `apply` mode.
Scanner tools are catalog entries (`assets/scanners.json`), not code paths.

## Package layout

```
orchestrator/providers/
├── __init__.py          # exports registry helpers
├── base.py              # ProviderStatus, StageRun, Provider protocol, helpers (plan_result, run_argv)
├── registry.py          # PROVIDERS, for_runner, detect_all, mode_for, LOCAL_RUNNERS
├── local.py             # bash <stored script> through runner.run_commands
├── docker.py            # docker run with the checkout mounted
├── github_actions.py    # generated workflow on a branch + gh workflow run (reuses dispatch helpers)
├── jenkins.py           # REST: crumb → buildWithParameters → poll → consoleText
├── aws.py               # CodeBuild start-build; ECR/ECS/S3 deploy helpers
├── gcp.py               # Cloud Build submit; Artifact Registry/Cloud Run helpers
├── azure.py             # Azure Pipelines run; ACR/Container Apps helpers
├── ssh.py               # scp + ssh bash
└── kubernetes.py        # kubectl apply (dry-run=client in plan mode) + rollout status
```

## `base.py`

```python
@dataclass
class ProviderStatus:
    runner: str
    cli: bool
    cli_path: str
    present: list[str]       # credential names found (never values)
    missing: list[str]
    mode: Literal["apply", "plan"]
    notes: str = ""

@dataclass
class StageRun:               # same keys the Sprint 2 runner returns, plus plan
    status: str               # pass | fail | skipped | planned | blocked
    exit_code: int
    blocked: bool
    commands: list[dict]      # [{label, argv, exit_code, evidence}]
    checks: list[dict]        # [{name, status, job, tool?, findings?, next_step?, needs_astra?}]
    evidence: str
    plan: dict                # {} when applied; {"runner","mode","missing","would_run","artifacts"} when planned
    def as_dict(self) -> dict

class Provider(Protocol):
    runner: str
    platform: str
    cli: str | None
    credential_keys: list[str]          # registry keys from credentials.json
    artifact_kinds: list[str]
    def detect(self) -> ProviderStatus
    def render_skeleton(self, stage: dict, environment: dict) -> dict[str, str]
    def commands_for(self, stage: dict, records: list[dict]) -> list[dict]
    def execute(self, stage: dict, records: list[dict], cwd: str, *, mode: str, answers: dict) -> StageRun

def plan_result(runner, missing, would_run, artifacts, note="") -> StageRun
def run_argv(argv, cwd, timeout, label) -> dict      # one subprocess, redacted evidence (reuse runner.py)
```

`registry.mode_for(runner, status)`: `apply` when `status.cli and not status.missing` and
(`runner in LOCAL_RUNNERS` or `os.getenv("ORCHESTRATOR_CLOUD_APPLY") == "1"`); else `plan`.
`LOCAL_RUNNERS = {"local", "docker"}`. `github_actions` counts as cloud for this rule but is
also allowed when `ORCHESTRATOR_DISPATCH == "github"` (the Sprint 2 path).

`detect()` never calls a network endpoint. It checks `shutil.which(cli)` and the presence of
credential names in `os.environ` (after `config.load_env_files()`). The only exception is
`docker.detect()`, which runs `docker info --format '{{.ServerVersion}}'` with a 5 s timeout
to learn whether the daemon is reachable.

## Runner matrix

| runner | detect (cli + names) | plan mode does | apply mode runs |
| --- | --- | --- | --- |
| `local` | always ready | n/a, always apply | `bash var/generated/<run>/<stage>/<script>` via `runner.run_commands` |
| `docker` | `docker` + daemon reachable | records the `docker run` argv | `docker run --rm -v <root>:/work -w /work -e RUN_ID -e STAGE <image> bash <script>`; image `ORCHESTRATOR_DOCKER_IMAGE` (default `python:3.12-slim`); scanner stages may use the catalog image (`aquasec/trivy`, `semgrep/semgrep`, `zricethezav/gitleaks`, `ghcr.io/zaproxy/zaproxy`) |
| `github_actions` | `gh` + `GH_TOKEN`/`GITHUB_TOKEN` + `GITHUB_REPOSITORY` | writes `gen-<stage>.yml`; plan says which branch it would be published on | `generator.materialize` + `generator.publish_branch` → `gh workflow run gen-<stage>.yml --ref orchestrator/generated/<run> -f run_id=<run>`; then `dispatch._find_run`, `gh run watch`, `_workflow_checks` (generalize `run_workflow(stage, answers, public_url, *, workflow_file=None, ref=None, inputs=None)`) |
| `jenkins` | `JENKINS_URL, JENKINS_USER, JENKINS_API_TOKEN, JENKINS_JOB` (no CLI) | Jenkinsfile artifact; plan shows the REST sequence | `GET /crumbIssuer/api/json` → `POST /job/<JOB>/buildWithParameters?RUN_ID=…` (basic auth, urllib) → poll `/job/<JOB>/lastBuild/api/json` until `result` → `consoleText` as evidence (redacted, ≤ 8 KB) |
| `aws_codebuild` | `aws` + (`AWS_ACCESS_KEY_ID`+`AWS_SECRET_ACCESS_KEY` or `AWS_PROFILE`) + `AWS_REGION` + `AWS_CODEBUILD_PROJECT` | buildspec artifact; plan shows `aws codebuild start-build --project-name … --buildspec-override file://…` | start-build → `aws codebuild batch-get-builds --ids <id>` poll (15 s) → `aws logs get-log-events` for the build log group when present |
| `gcp_cloudbuild` | `gcloud` + (`GOOGLE_APPLICATION_CREDENTIALS` or `GCLOUD_SA_KEY`) + `GCP_PROJECT_ID` | cloudbuild.yaml artifact; plan shows `gcloud builds submit --config … --project …` | submit (streams log) → `gcloud builds describe <id> --format json` |
| `azure_pipelines` | `az` + `AZURE_DEVOPS_ORG_URL, AZURE_DEVOPS_PROJECT, AZURE_DEVOPS_EXT_PAT, AZURE_PIPELINE_ID` | azure-pipelines.yml artifact; plan shows `az pipelines run --id … --branch …` | run → `az pipelines runs show --id … --query result` poll |
| `ssh_remote` | `ssh` + `SSH_HOST, SSH_USER, SSH_KEY_PATH` | script artifact; plan shows `scp` + `ssh` | `scp -i $SSH_KEY_PATH <script> user@host:/tmp/<run>-<stage>.sh` then `ssh … 'bash /tmp/<run>-<stage>.sh'` |
| `kubernetes` | `kubectl` + (`KUBECONFIG` or `~/.kube/config`) + `K8S_NAMESPACE` | manifest artifact; runs `kubectl apply --dry-run=client -f <manifest>` (allowed in plan mode: it is local validation) | `kubectl apply -f <manifest> -n $K8S_NAMESPACE` then `kubectl rollout status deployment/<name> --timeout=180s` |

Deploy-family stages on cloud runners use provider sub-commands by `stage["provider"]`:
`aws_ecs` (`aws ecs update-service --cluster $AWS_ECS_CLUSTER --service $AWS_ECS_SERVICE
--force-new-deployment`), `aws_cfn` (`aws cloudformation deploy --stack-name …
--no-execute-changeset` in plan, execute in apply), `gcp_cloud_run` (`gcloud run deploy
$GCP_CLOUD_RUN_SERVICE --image … --region $GCP_REGION`), `azure_container_apps`
(`az containerapp update --name $AZURE_CONTAINER_APP --resource-group $AZURE_RESOURCE_GROUP
--image …`), `kubectl`, `ssh_compose` (`docker compose -f docker-compose.generated.yml up -d`
over ssh). Each is a short function in the provider module that returns argv; the template
for the artifact carries the same command so the person can read it.

## Plan-mode result (what every provider returns when it cannot run)

```python
StageRun(status="planned", exit_code=0, blocked=False,
         commands=[{"label": f"{runner} plan", "argv": would_run[0] if would_run else [], "exit_code": 0, "evidence": note}],
         checks=[{"name": f"{runner} plan", "status": "planned", "job": "", "findings": note, "next_step": "Set " + ", ".join(missing) + " and run again to apply.", "needs_astra": False}],
         evidence=note, plan={"runner": runner, "mode": "plan", "missing": missing, "would_run": would_run, "artifacts": [r["path"] for r in records]})
```
`note` is `"planned: missing AWS_ACCESS_KEY_ID, AWS_REGION; would run: aws codebuild start-build …"`.

## `dispatch.execute_stage_v2`

```python
def execute_stage_v2(spec, commands, cwd, answers, readiness, records) -> dict:
    runner = spec["runner"]
    provider = providers.for_runner(runner)
    status = providers.status_from_readiness(readiness, runner)      # or provider.detect()
    mode = providers.mode_for(runner, status)
    verdict = policy.check_commands(commands)                        # second line of defense
    if not verdict.ok:
        return _failed(spec["name"], "policy blocked: " + "; ".join(f.message for f in verdict.findings)) | {"blocked": True}
    if spec["name"] == "dast_scan" and not public_url: ...            # same rule as fixed dast
    return provider.execute(spec, records, cwd, mode=mode, answers=answers).as_dict()
```
`execute_stage` (Sprint 2) is unchanged and still used by the nine fixed nodes.

## Scanner catalog (`assets/scanners.json` → copy to `orchestrator/scanners.json`)

Each entry:
```json
"trivy": {"family": "scan", "stages": ["container_scan", "sca", "iac_scan"], "cli": "trivy",
          "docker_image": "aquasec/trivy:0.58.0", "install": "brew install trivy",
          "argv": {"container_scan": ["trivy", "image", "--severity", "HIGH,CRITICAL", "--exit-code", "1", "$IMAGE"],
                   "sca": ["trivy", "fs", "--scanners", "vuln", "--severity", "HIGH,CRITICAL", "$ROOT"]},
          "ci_action": "aquasecurity/trivy-action@v0.36.0", "env": ["TRIVY_TOKEN"],
          "extract": {"count_regex": "Total:\\s*(\\d+)", "line_regex": "│|Total:|CRITICAL|HIGH",
                      "fail_when": "count>0", "next_step_hit": "Upgrade the package Trivy listed, then run the scan again.",
                      "next_step_clean": "No dependency change is required for Trivy."},
          "aliases": ["trivy", "vulnerabilities (trivy"]}
```
`catalog.scanner_commands(key, stage, *, root, image, app_url)` builds argv from the entry
with `$ROOT`, `$IMAGE`, `$APP_URL`, `$RUN_ID` substitution; falls back to `_skip(tool, ci)`
when the CLI is absent and the runner is `local`; uses the `docker_image` when the runner is
`docker`.

`findings.py` loads `extract` blocks into `EXTRACTORS` at import. The eight hand-written
extractors stay as overrides. `tool_key(step_name)` also checks catalog `aliases`. Unknown
tools get the generic extractor: lines matching `(?i)(error|fail|vuln|critical|high|finding)`
with `needs_astra = exit_code != 0`.

## Tests (`tests/test_providers.py`, offline)

- `detect_all()` returns a status for every runner in `RUNNERS`; with a scrubbed environment
  (`monkeypatch.delenv` for every registry name) every cloud runner has `mode == "plan"`.
- `aws.execute(..., mode="plan")` returns `status == "planned"`, `plan["would_run"][0][0] == "aws"`,
  and never calls `subprocess.run` (monkeypatch it to raise).
- `local.execute` runs a stored echo script and returns `pass`.
- `docker.execute(mode="plan")` records the `docker run` argv with the checkout mounted.
- `kubernetes.execute(mode="plan")` would run `kubectl apply --dry-run=client` (monkeypatch
  `subprocess.run` to assert the argv).
- `github_actions` plan names the branch `orchestrator/generated/<run_id>` and the file
  `.github/workflows/gen-<stage>.yml`.
- `catalog.scanner_commands("trivy", "container_scan", image="app:x")` substitutes `$IMAGE`;
  with trivy absent and runner local it returns the skip record.
- `findings.extract_result("grype", [...])` (catalog-driven) and a generic unknown tool.
- `execute_stage_v2` with a command that fails policy returns `blocked == True` without
  running anything.
