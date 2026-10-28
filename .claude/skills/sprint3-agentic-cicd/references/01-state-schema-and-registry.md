# 01 — State schema and the stage registry (Phase 1)

Goal: open the stage vocabulary without breaking the nine fixed stages, and add the state
fields the later phases write. After this phase every existing test still passes.

## New module: `orchestrator/registry.py`

```python
"""Stage vocabulary. Fixed stages keep their nodes; templates and custom names run on the
generic generated_stage node. Names are data, not graph nodes."""
from __future__ import annotations
import re
from dataclasses import dataclass, field

STAGE_FAMILIES = ("build", "test", "scan", "package", "container", "release", "deploy", "verify", "uat", "custom")
RUNNERS = ("local", "docker", "github_actions", "jenkins", "aws_codebuild", "gcp_cloudbuild",
           "azure_pipelines", "ssh_remote", "kubernetes")
PLATFORMS = ("local", "docker", "github", "jenkins", "aws", "gcp", "azure", "remote", "kubernetes")
STATUSES = ("pending", "pass", "fail", "skipped", "planned", "blocked")
CUSTOM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")

@dataclass(frozen=True)
class StageTemplate:
    name: str
    family: str
    summary: str
    default_runners: tuple[str, ...] = ("local", "docker", "github_actions")
    providers: tuple[str, ...] = ()          # keys in assets/scanners.json or provider names
    produces: tuple[str, ...] = ("shell",)   # artifact kinds Astra usually generates
    after: tuple[str, ...] = ()              # typical depends_on hint, not enforced
    env_names: tuple[str, ...] = ()          # credential registry keys usually needed

FIXED_STAGES: dict[str, StageTemplate]   # build, test, scanners, dast, package, container, release, deploy, uat
STAGE_TEMPLATES: dict[str, StageTemplate]  # the table below, keyed by name, includes FIXED_STAGES

def is_fixed(name: str) -> bool
def template_for(name: str) -> StageTemplate | None
def family_of(name: str) -> str            # template family, else "custom"
def validate_stage_name(name: str) -> str  # returns the slug or raises ValueError
def stage_menu() -> list[dict]             # [{name, family, summary, default_runners, providers}] for prompts
def runners_for(platform: str) -> list[str]
```

`validate_stage_name` accepts a fixed name, a template name, or a custom slug matching
`CUSTOM_NAME_RE` that is not a reserved word (`production`, `prod`, `promote`, `main`).
It lower-cases and strips. Anything else raises `ValueError("stage name ... is not allowed")`.

### Stage templates (register all of these)

| Family | Names (summary) | default_runners | providers |
| --- | --- | --- | --- |
| build | `build` (fixed), `lint` (ruff + eslint), `compile` (compileall / tsc), `frontend_build` (vite build), `build_image` (docker build, no push), `dependency_install` (pip/npm ci) | local, docker, github_actions | — / `hadolint` for images |
| test | `test` (fixed pytest), `unit_test`, `integration_test` (compose up + HTTP), `e2e_test` (browser or API flow), `contract_test` (OpenAPI schema), `perf_test` (k6/locust), `smoke_test` (`/status` probe) | local, docker, github_actions, aws_codebuild, gcp_cloudbuild, azure_pipelines, jenkins | `k6`, `locust`, `schemathesis` |
| scan | `scanners` (fixed), `sast`, `sca`, `secrets_scan`, `container_scan`, `iac_scan`, `license_scan`, `sbom`, `dast_scan`, `dockerfile_lint`, `workflow_lint` | local, docker, github_actions, jenkins | `semgrep`, `bandit`, `codeql`, `sonar`, `pip_audit`, `npm_audit`, `osv_scanner`, `snyk`, `trivy`, `grype`, `gitleaks`, `trufflehog`, `checkov`, `tfsec`, `syft`, `zap`, `stackhawk`, `nuclei`, `hadolint`, `actionlint` |
| package | `package` (fixed tarball), `package_artifact` (wheel / npm pack), `publish_artifact` (S3 / GCS / Azure Blob / GitHub release asset; plan mode without creds) | local, github_actions, aws_codebuild, gcp_cloudbuild, azure_pipelines | `aws_s3`, `gcs`, `azure_blob`, `github_release` |
| container | `container` (fixed Dockerfile check), `push_image` (ECR / Artifact Registry / ACR / GHCR; plan mode without creds) | docker, github_actions, aws_codebuild, gcp_cloudbuild, azure_pipelines | `ecr`, `artifact_registry`, `acr`, `ghcr` |
| release | `release` (fixed plan), `release_notes` (from git log), `tag_release` (annotated tag, no push unless apply), `sign_artifact` (cosign; plan without key) | local, github_actions | `cosign` |
| deploy | `deploy` (fixed gated plan), `infra_plan` (terraform/cdk/cfn plan), `infra_apply` (apply; needs `ORCHESTRATOR_CLOUD_APPLY=1`), `deploy_dev`, `deploy_staging`, `deploy_uat` (ECS / Cloud Run / Container Apps / k8s / ssh+compose), `migrate_db`, `rollback_plan` | aws_codebuild, gcp_cloudbuild, azure_pipelines, kubernetes, ssh_remote, docker | `aws_ecs`, `aws_cfn`, `gcp_cloud_run`, `azure_container_apps`, `kubectl`, `ssh_compose` |
| verify | `post_deploy_smoke`, `health_check`, `observability_check` (CloudWatch / Cloud Logging / Monitor query, plan mode) | local, docker, aws_codebuild, gcp_cloudbuild, azure_pipelines | `cloudwatch`, `cloud_logging`, `azure_monitor` |
| uat | `uat` (fixed; always last; always runner `local`) | local | — |

Every template has `summary` text Astra sees in the stage menu. Keep summaries under 100
characters.

## `orchestrator/schemas.py` v2

Keep every existing model and add the following. `StageSpec` must still accept the Sprint 2
call `StageSpec(name="build", depends_on=[], purpose="compile")` (tests use it), so the new
fields have defaults and a `model_validator(mode="before")` fills `family` and `kind` from the
registry when they are missing.

```python
StageFamily = Literal["build", "test", "scan", "package", "container", "release", "deploy", "verify", "uat", "custom"]
Runner = Literal["local", "docker", "github_actions", "jenkins", "aws_codebuild", "gcp_cloudbuild", "azure_pipelines", "ssh_remote", "kubernetes"]
Platform = Literal["local", "docker", "github", "jenkins", "aws", "gcp", "azure", "remote", "kubernetes"]
Tier = Literal["local", "dev", "staging", "uat"]           # production is not a choice
ArtifactKind = Literal["github_workflow", "jenkinsfile", "buildspec", "cloudbuild", "azure_pipeline",
                       "shell", "dockerfile", "compose", "env_example", "doc", "kubernetes_manifest", "script"]

class EnvironmentSpec(BaseModel):
    tier: Tier = "local"
    platform: Platform = "local"
    region: str = ""          # e.g. us-east-1; empty for local
    notes: str = ""

class ArtifactSpec(BaseModel):
    kind: ArtifactKind
    path: str = Field(description="Relative path inside the checkout where the file would live.")
    purpose: str

class GeneratedArtifact(ArtifactSpec):
    content: str = Field(description="Full file contents. Secrets only as ${{ secrets.NAME }}, $NAME, or ${NAME}.")
    secrets_referenced: list[str] = Field(default_factory=list)

class StageSpec(BaseModel):
    name: str                                   # validated with registry.validate_stage_name
    family: StageFamily = "custom"
    kind: Literal["fixed", "generated"] = "fixed"
    runner: Runner = "local"
    provider: str = ""                          # scanner or provider key, "" for fixed catalog stages
    environment: EnvironmentSpec = Field(default_factory=EnvironmentSpec)
    depends_on: list[str] = Field(default_factory=list)
    purpose: str
    artifacts: list[ArtifactSpec] = Field(default_factory=list)   # intent; content arrives in generate_artifacts
    stochastic: bool = False

class CandidatePipeline(BaseModel):
    id: str
    summary: str
    mode: Literal["deterministic", "stochastic"] = "deterministic"
    rationale: str = ""
    environment: EnvironmentSpec = Field(default_factory=EnvironmentSpec)
    stages: list[StageSpec] = Field(min_length=1)

class PlanProposal(BaseModel):
    candidates: list[CandidatePipeline] = Field(min_length=2, max_length=4)

class FollowUpSet(BaseModel):
    questions: list[EcosystemQuestion] = Field(default_factory=list, max_length=3)

class FollowUpDecision(BaseModel):
    action: Literal["proceed", "ask_more"]
    rationale: str

class RunnerDecision(BaseModel):
    runners: dict[str, Runner]                  # stage name -> runner
    rationale: str

class ArtifactSet(BaseModel):
    stage: str
    artifacts: list[GeneratedArtifact] = Field(min_length=1, max_length=6)
    notes: str = ""

class ArtifactReview(BaseModel):
    verdict: Literal["accept", "revise", "reject"]
    rationale: str

class DeployDoc(BaseModel):
    path: str
    content: str

class PolicyFinding(BaseModel):
    rule: str
    severity: Literal["error", "warn"]
    message: str
    line: int = 0

class PolicyResult(BaseModel):
    ok: bool
    findings: list[PolicyFinding] = Field(default_factory=list)
```

Rules:
- `StageSpec.name` validator calls `registry.validate_stage_name`; `kind` becomes `fixed`
  when `registry.is_fixed(name)` else `generated`; `family` becomes `registry.family_of(name)`.
- `validate_stage_name` refuses any underscore-separated word `prod` or `production`
  (`deploy_prod`, `release_to_production`), not only the bare names; `product_catalog_check`
  is fine.
- `uat` stays `kind=fixed`, `runner=local`; the validator forces that.
- `CandidatePipeline` validator: when `uat` is present it is the last stage, appears once,
  depends on at least one earlier stage, and no stage depends on it. A missing `uat` is
  accepted by the schema (the Sprint 2 test doubles build build-only and build+test
  candidates and must keep passing); `model_validate(..., context={"require_uat": True})`
  turns the requirement on, and `plan_pipeline` uses that context for live Astra proposals
  from phase 5 on. The Astra prompt still asks for `uat` in every candidate.
- `PlanProposal` validator: ids unique; at least one `deterministic` candidate. The graph logs
  (not fails) when no `stochastic` candidate was proposed.
- Keep `StageName` as an alias exported for old imports: `StageName = str`.
- Phase 1 interim rule: until the `generated_stage` node exists (phase 5), `plan_pipeline`
  calls `normalize_stages(..., allow_generated=False)` so a non-fixed name never becomes a
  `Send` to a missing node; the dropped name is recorded as
  `<name>->generated_stage_unavailable` in `pipeline["dropped_edges"]`. Phase 5 removes the
  argument.

## `orchestrator/state.py` additions

```python
MAX_CLARIFY_ROUNDS = 2          # first round + one follow-up
MAX_ARTIFACT_REVISIONS = 1
MAX_HOPS = 96                   # phase 5 raises it to 128 together with recursion_limit (120 -> 160)
STATUSES = registry.STATUSES

class PipelineState(TypedDict):
    ...existing...
    retrieval: dict                                   # {index_id, files, chunks, hits: {topic: [...]}}
    episodes: list[dict]                              # recalled prior runs (summaries only)
    readiness: dict                                   # credentials.readiness() output (names only)
    runner_plan: Annotated[dict, merge_dict]          # stage -> {runner, provider, mode: plan|apply}
    generated_artifacts: Annotated[dict, merge_dict]  # stage -> [artifact records]
    policy: Annotated[list[dict], operator.add]       # policy findings records
    clarify_rounds: Annotated[int, operator.add]
    follow_up_decision: dict
    artifact_review: Annotated[dict, merge_dict]      # stage -> {verdict, rationale, revisions}
```

`initial_state` sets each to its empty value. `env_state` gains
`"platform": "local"` and `"cloud_apply": False`.

Helpers to add:
```python
def planned_stages(state) -> list[str]
def pending_stages(state)   # planned now counts as done (status in {"pass", "skipped", "planned"})
def platform_of(answers) -> str      # answers["env_platform"], default "local", validated against PLATFORMS
def runner_default_for(platform) -> str
```

`_deps_met` in graph.py treats `planned` like `pass`.

Artifact record shape stored in `generated_artifacts[stage]` (never the raw content):
`{"path", "kind", "purpose", "sha256", "bytes", "stored_at": "var/generated/<run>/<stage>/<path>", "policy": {"ok", "errors", "warns"}, "secrets_referenced": [...]}`.

Policy record shape in `policy`:
`{"stage", "path", "rule", "severity", "message", "line"}`.

## `normalize_stages` v2 (graph.py)

- Accept any name that passes `registry.validate_stage_name`; keep the dedupe, missing-dep
  removal, and cycle breaking exactly as today.
- Carry through `family, kind, runner, provider, environment, artifacts, stochastic`.
- `tool` label: `TOOL_BINDINGS[name]` for fixed stages, else
  `f"{template.summary} via {runner}"` or `f"custom stage via {runner}"`.
- `uat` must be last; if it is not, move it last and record `"uat->moved_last"` in the removed
  list so the report shows it.

## `catalog.commands_for` v2

Unchanged for fixed stages. For a generated stage return `[]`; the generator fills
`stage["commands"]` after artifacts exist (`06-providers-runners.md`).

## Tests for this phase (`tests/test_registry.py`)

- every fixed name is in `STAGE_TEMPLATES` and `is_fixed` is true only for the nine
- `validate_stage_name("Container_Scan ")` → `container_scan`; `"production"` raises;
  `"a"` raises; `"deploy-prod"` raises (hyphen and prod)
- `StageSpec(name="build", depends_on=[], purpose="x")` → `kind == "fixed"`, `family == "build"`
- `StageSpec(name="container_scan", purpose="x")` → `kind == "generated"`, `family == "scan"`
- `StageSpec(name="uat", runner="docker", purpose="x")` → runner coerced to `local`
- `CandidatePipeline` with `uat` not last raises; without `uat` raises
- `normalize_stages` keeps a custom stage and still drops cycles
- `pending_stages` excludes a `planned` stage
