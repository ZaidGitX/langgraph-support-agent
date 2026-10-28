# 03 — Astra: prompts, structured outputs, and artifact generation (Phases 2, 5)

Astra stays `OpenAIAstra` in `orchestrator/llm.py`, called only through `invoke_structured`.
What changes: richer payloads (retrieval, episodes, readiness, stage menu), sampling
temperature for proposals, and two new outputs: `ArtifactSet` and `DeployDoc`.

## `invoke_structured` v2

```python
def _client(model_name: str, temperature: float | None = None) -> ChatOpenAI:
    kwargs = dict(model=model_name, api_key=api_key, use_responses_api=True, timeout=120)
    if temperature is not None:
        kwargs["temperature"] = temperature
    return ChatOpenAI(**kwargs)

def invoke_structured(model_name, schema, system, payload, *, temperature=None):
    # two attempts as today; if the first error message mentions "temperature"
    # (some reasoning models reject it), retry once without it.
```

`OpenAIAstra.__init__(self, temperature: float | None = None)` reads
`ORCHESTRATOR_ASTRA_TEMPERATURE` (default `0.7`) when the argument is `None`. Only `propose`
and `generate_artifacts` pass the temperature; questions, briefs, advice, diagnosis, and docs
stay at the model default. The fake doubles ignore temperature.

## Methods

| Method | Schema | System prompt | Payload keys |
| --- | --- | --- | --- |
| `write_questions` | `QuestionSet` | `QUESTION_SYSTEM_V3` | `ecosystem{languages, entry_points, gaps, build_tools}`, `retrieved`, `episodes`, `readiness`, `stage_menu`, `already_answered` |
| `follow_up` | `FollowUpSet` | `FOLLOW_UP_SYSTEM` | `answers`, `retrieved`, `readiness`, `rounds`, `max_rounds` |
| `propose` | `PlanProposal` | `PLAN_SYSTEM_V3` | `repo`, `entry_points`, `answers`, `diagnosis`, `retrieved`, `episodes`, `readiness`, `stage_menu`, `platform`, `runners_available` |
| `generate_artifacts` | `ArtifactSet` | `GENERATE_SYSTEM` | `stage`, `runner`, `provider`, `environment`, `skeletons`, `placeholders`, `retrieved`, `answers`, `repo`, `policy_rules`, `policy_findings?` |
| `write_docs` | `DeployDoc` | `DOCS_SYSTEM` | `pipeline`, `environment`, `artifacts` (paths + purposes), `readiness`, `answers` |
| `brief`, `advise`, `diagnose` | unchanged | unchanged, plus one sentence about `planned` | unchanged |

## Prompts (copy these; edit only with a reason)

```python
QUESTION_SYSTEM_V3 = (
    "You are Astra. Write the questions a human must answer before a CI/CD pipeline can be "
    "planned for this checkout. Use the retrieved code excerpts and prior episodes to avoid "
    "asking what the repository already answers. Ask at most six questions. When a topic is "
    "open, use these ids exactly: env_target (local, dev, staging, or uat; never production), "
    "env_platform (local, docker, github, jenkins, aws, gcp, azure, remote, kubernetes), "
    "runner_preference, test_depth, scan_policy, release_policy. Mention in the prompt which "
    "credentials readiness says are missing so the person can choose a plan-only route "
    "knowingly. Do not answer the questions. Do not propose a pipeline. Do not write shell."
)

FOLLOW_UP_SYSTEM = (
    "You are Astra. The person answered the first round. Using their answers and the retrieved "
    "excerpts, ask at most three follow-up questions that change which stages, runner, "
    "provider, or environment the pipeline should use. Skip anything already answered. Use "
    "short snake_case ids. Do not answer the questions. Do not write shell."
)

PLAN_SYSTEM_V3 = (
    "You are Astra. Propose two to four different stage DAGs for this repository. Stage names "
    "come from stage_menu, or are new snake_case names when no template fits; name the family "
    "for every stage. Propose at least one deterministic candidate that uses only the fixed "
    "stages build, test, scanners, dast, package, container, release, deploy, uat, and at least "
    "one stochastic candidate that adds generated stages (for example container_scan with trivy, "
    "sbom with syft, deploy_dev on aws_codebuild, post_deploy_smoke) suited to the answers, the "
    "platform, and the retrieved code. Every candidate includes build, test, and uat; uat is last "
    "and depends on the stage before it. No production stage and nothing after uat. Pick a "
    "runner for each stage from runners_available; when the readiness note says credentials are "
    "missing, still propose the stage and say it will run in plan mode. Prior episodes tell you "
    "what ran and failed before; prefer routes that fixed earlier failures. For a fixed stage do "
    "not write commands; for a generated stage list the artifacts you intend to create (kind, "
    "path, purpose) without their content."
)

GENERATE_SYSTEM = (
    "You are Astra. Write the complete files one generated CI/CD stage needs for the given "
    "runner, provider, and environment. Start from the skeletons provided and keep them valid "
    "for their format (GitHub Actions YAML, Jenkinsfile, CodeBuild buildspec, Cloud Build "
    "config, Azure Pipelines YAML, Dockerfile, Compose, or a bash script with set -euo pipefail). "
    "Reference credentials only by the placeholder names provided, as ${{ secrets.NAME }} in "
    "GitHub Actions, $NAME or ${NAME} elsewhere; never write a value. Never reference production. "
    "Never use rm -rf on / or $HOME, curl piped to a shell, sudo, chmod 777, or --force on main. "
    "Pin every GitHub action to a tag or SHA. A GitHub workflow triggers only on "
    "workflow_dispatch. Print machine-readable summary lines so findings can be extracted. "
    "When policy_findings are present, fix every one of them and change nothing else. Return "
    "full file contents for every artifact."
)

DOCS_SYSTEM = (
    "You are Astra. Write a short deployment document for this pipeline and environment: what "
    "runs where, which placeholder variables must be set and where (GitHub secrets, .env, CI "
    "variables), how to run the generated artifacts, and how to roll back. Use only the "
    "artifact paths and placeholder names given. Do not include any secret value."
)
```

## `orchestrator/generator.py` (new)

```python
@dataclass
class GenerationContext:
    run_id: str
    stage: dict                 # the StageSpec dict from the pipeline
    repo: dict                  # profile
    retrieved: dict             # topic -> excerpts (trimmed to ≤ 6 excerpts total)
    answers: dict
    readiness: dict             # names only
    skeletons: dict[str, str]   # artifact kind -> rendered template text from assets/templates
    placeholders: list[str]     # credential names allowed for this provider
    policy_rules: list[str]     # human-readable summary from policy.RULES
    policy_findings: list[dict] = field(default_factory=list)

def context_for(state: dict, stage: dict) -> dict          # builds the payload for planner.generate_artifacts
def write(run_id: str, stage: dict, artifact_set: ArtifactSet, root: str) -> list[dict]
    # writes var/generated/<run_id>/<stage>/<artifact.path>, returns records (sha256, bytes, policy, …)
def materialize(run_id: str, stage: dict, root: str) -> list[str]
    # copies records into the repo at artifact.path; only when ORCHESTRATOR_WRITE_ARTIFACTS == "1";
    # a github_workflow goes to .github/workflows/gen-<stage>.yml (GitHub ignores subfolders)
def publish_branch(run_id: str, root: str) -> str
    # git checkout -b orchestrator/generated/<run_id>; add materialized files; commit; push with GH_TOKEN;
    # returns the branch name; only when ORCHESTRATOR_DISPATCH == "github" and ORCHESTRATOR_WRITE_ARTIFACTS == "1"
```

Rules:
- **Template first.** `providers.<runner>.render_skeleton(stage, environment)` renders
  `assets/templates/<kind>.tpl` with `string.Template` (`$NAME` placeholders) into the
  skeleton Astra completes. The skeletons already contain the trigger, permissions, the
  checkout step, the env placeholder block, and a `# --- ASTRA: steps ---` marker. Astra
  returns the whole file.
- **Paths.** Artifact paths are relative, no `..`, no leading `/`, and under one of:
  `.github/workflows/gen-*.yml`, `ci/**`, `deploy/**`, `docker/**`, `docs/deploy/**`,
  `.env.<tier>.example`, `Dockerfile.generated`, `docker-compose.generated.yml`. Anything else
  fails the policy check (`08-policy-guardrails.md`).
- **Env manifests** are written by the generator, not Astra: `.env.<tier>.example` lists every
  placeholder from `credentials.placeholders_for(provider)` plus app names, all empty.
- **Content size** ≤ 32 KB per artifact; ≤ 6 artifacts per stage.
- **Redaction** runs on Astra output before it is written (`redaction.redact`).

## Payload example for `generate_artifacts`

```json
{"stage": {"name": "container_scan", "family": "scan", "runner": "docker", "provider": "trivy",
           "environment": {"tier": "dev", "platform": "docker"}, "purpose": "scan the built image"},
 "skeletons": {"shell": "#!/usr/bin/env bash\nset -euo pipefail\n# stage: container_scan ...\n# --- ASTRA: steps ---\n"},
 "placeholders": ["TRIVY_TOKEN", "REGISTRY_URL"],
 "retrieved": {"docker": [{"path": "Dockerfile", "lines": "1-40", "text": "FROM node:22-slim AS frontend ..."}]},
 "answers": {"env_target": "dev", "env_platform": "docker", "scan_policy": "fail on HIGH,CRITICAL"},
 "repo": {"languages": ["Python", "JavaScript"], "build_tools": ["pip", "pytest", "npm", "docker", "github-actions"]},
 "policy_rules": ["no production", "secrets by reference", "pin actions", "no curl|sh", "..."]}
```

Expected `ArtifactSet`: one `shell` artifact at `ci/container_scan.sh` that builds the image
with `docker build -t app:$RUN_ID .` and runs `trivy image --severity HIGH,CRITICAL
--exit-code 1 app:$RUN_ID`, printing `Total: N (HIGH: x, CRITICAL: y)` lines the trivy
extractor understands.

## Fake double for tests (`tests/test_orchestrator.py`)

Extend `FakeAstra` with `follow_up` (returns one question on round 1 when
`payload["rounds"] == 1`, else none), `generate_artifacts` (returns a `shell` artifact whose
content is `#!/usr/bin/env bash\nset -euo pipefail\necho generated-ok\n`), `write_docs`.
`propose` returns one deterministic and one stochastic candidate; the stochastic one adds a
generated `container_scan` stage with `runner="local"` so the echo script really runs.
