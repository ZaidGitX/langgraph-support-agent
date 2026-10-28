# 08 — Policy for generated commands and artifacts (Phase 3, used from Phase 4 on)

Sprint 2 kept Astra away from shell by schema. Sprint 3 lets Astra write files that contain
commands, so a code-level policy checks every generated artifact before the person sees it,
and every command again right before it runs. The policy is deterministic (regexes and path
rules), never a model opinion. `guardrails.py` keeps its role for the gate.

## `orchestrator/policy.py`

```python
@dataclass(frozen=True)
class Rule:
    id: str
    severity: Literal["error", "warn"]
    pattern: str                    # regex, compiled with re.IGNORECASE | re.MULTILINE
    message: str
    kinds: tuple[str, ...] = ()     # artifact kinds it applies to; () = all

RULES: list[Rule]                   # the table below, in this order
ALLOWED_PATH_PREFIXES = (".github/workflows/gen-", "ci/", "deploy/", "docker/", "docs/deploy/", ".env.", "Dockerfile.generated", "docker-compose.generated")

def check_text(text: str, *, kind: str = "shell", path: str = "") -> PolicyResult
def check_artifact(artifact: GeneratedArtifact) -> PolicyResult        # text rules + path rules + kind rules
def check_artifact_set(artifact_set: ArtifactSet, stage: dict) -> PolicyResult
def check_commands(commands: list[dict]) -> PolicyResult               # joins argv with spaces, kind="shell"
def summary(result: PolicyResult) -> dict                               # {"ok", "errors", "warns"} for records
def rules_summary() -> list[str]                                        # one line per rule, for Astra's prompt
```

`PolicyResult.ok` is `True` when there are no `error` findings. Findings carry the rule id,
severity, message, and the 1-based line.

## Rules

| id | severity | pattern (regex, simplified here) | applies to |
| --- | --- | --- | --- |
| `destructive.rm` | error | `\brm\s+-[a-z]*r[a-z]*f?[a-z]*\s+(/\|~\|\$HOME\|\.\.?)(\s\|$)` and `rm -rf \*` | all |
| `destructive.curl_sh` | error | `(curl\|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba\|z\|)sh\b` | all |
| `destructive.sudo` | error | `(^\|[;&|\s])sudo\s` | all |
| `destructive.chmod777` | error | `chmod\s+(-R\s+)?777` | all |
| `destructive.force_push` | error | `git\s+push\b[^\n]*(--force\|-f)\b[^\n]*\b(main\|master)\b` | all |
| `destructive.terraform` | error | `terraform\s+destroy` | all |
| `destructive.kubectl` | error | `kubectl\s+delete\s+(ns\|namespace\|-A\|--all)\b` | all |
| `destructive.aws` | error | `aws\s+\S+\s+delete-\S+`, `aws\s+s3\s+(rb\|rm)\b`, `aws\s+cloudformation\s+delete-stack` | all |
| `destructive.gcloud` | error | `gcloud\s+[^\n]*\bdelete\b` | all |
| `destructive.az` | error | `\baz\s+[^\n]*\bdelete\b` | all |
| `destructive.docker_prune` | error | `docker\s+(system\|volume\|image)\s+prune` | all |
| `prod.reference` | error | `\b(production\|prod)\b` | all |
| `secret.literal` | error | the patterns in `redaction.PATTERNS` | all |
| `secret.assignment` | error | `(?i)^\s*(export\s+)?[A-Z0-9_]*(KEY\|TOKEN\|SECRET\|PASSWORD)[A-Z0-9_]*\s*=\s*["']?[^\s"'$]{8,}` | all |
| `secret.unknown_name` | warn | `\$\{\{\s*secrets\.([A-Z0-9_]+)\s*\}\}` where the name is not in the credential registry | github_workflow |
| `secret.gh_set_literal` | error | `gh\s+secret\s+set\s+\S+\s+(-b\|--body)\s+["']?[^$\s]` | all |
| `exfil.env_dump` | error | `(^\|[;&|\s])(printenv\|env)(\s*$\|\s*\|)`, `cat\s+[^\n]*\.env(\s\|$)` (not `.env.example`), `echo\s+[^\n]*\$\{?(` + secret names + `)\b` | all |
| `trigger.not_dispatch` | error | a GitHub workflow whose `on:` block contains `push`, `pull_request`, `schedule`, or `workflow_run` | github_workflow |
| `trigger.missing_dispatch` | error | no `workflow_dispatch` under `on:` | github_workflow |
| `pin.unpinned_action` | error | `uses:\s*[^\s@]+\s*$` (no `@`) | github_workflow |
| `pin.floating_ref` | warn | `uses:\s*[^\s@]+@(main\|master\|latest)\b` | github_workflow |
| `perms.missing` | warn | no top-level `permissions:` | github_workflow |
| `shell.no_strict` | warn | a shell artifact without `set -euo pipefail` in its first 5 lines | shell |
| `shell.eval` | warn | `(^\|[;&|\s])eval\s` | all |
| `shell.base64_exec` | error | `base64\s+(-d\|--decode)[^\n]*\|\s*(ba\|)sh` | all |
| `shell.no_verify` | warn | `--no-verify` | all |
| `path.outside` | error | path not under `ALLOWED_PATH_PREFIXES`, contains `..`, or starts with `/` | all (path rule) |
| `path.workflow_name` | error | a github_workflow whose path is not `.github/workflows/gen-<stage>.yml` | github_workflow |
| `toggle.in_artifact` | error | `ORCHESTRATOR_CLOUD_APPLY\|ORCHESTRATOR_WRITE_ARTIFACTS` set inside an artifact | all |
| `size.too_large` | error | content over 32 KB | all |

`prod.reference` has one exemption: the exact phrase `no production` / `never production`
in a comment line (`#` or `//` prefix) is allowed so docs can state the rule.

The same rule ids and regexes live in `scripts/policy_rules.py` inside this skill, used by
the hooks and by `scripts/policy_check.py`. There, `trigger.*`, `perms.missing`,
`secret.unknown_name`, `shell.no_strict`, `size.too_large`, and `path.*` are implemented in
code (the `on:` block is parsed so a job named `release` is not mistaken for a trigger), and
`rule_ids()` returns the full set. `tests/test_policy.py::test_rule_ids_match_skill` loads
that file by path (when present) and asserts `policy.rule_ids() == skill.rule_ids()`, so the
two never drift. Lines that refuse production (`production|prod) ... exit 2`, "No path past
UAT", "never production") are exempt from `prod.reference` in both.

## Where the policy runs

1. `generate_artifacts` node: `check_artifact_set` → Jev `artifact_review` → revise once →
   drop or keep. Findings are written to `state["policy"]` and to each artifact record.
2. `approve_pipeline` payload: every artifact carries `policy: {"ok", "errors", "warns"}` and
   the findings list; the UI shows a badge per artifact and blocks the Approve button when any
   artifact has `ok == false` (the backend also refuses: `session.submit` raises
   `ValueError("pipeline has policy errors")` for `pipeline_approval` + `approved`).
3. `dispatch.execute_stage_v2`: `check_commands(commands)` right before execution; an error
   returns a `blocked` result (`guardrails` then escalates, as it does for a refused deploy).
4. Hooks (`guard_bash.py`, `post_edit_check.py`): the same regexes guard the coding agents'
   own shell commands and edits while they build the sprint.

## Guardrails (`orchestrator/guardrails.py`) changes

- `planned` stages are neither failures nor pending: `pending_stages` excludes them,
  `failed_stages` ignores them, `_deps_met` accepts them.
- Everything else is unchanged: budget → escalate; blocked → escalate; `escalate` with no
  failure → `pass`; `pass`/`skip` with failures → `remediate`; retry cap 2; no retry of a stage
  outside the approved pipeline; plan retries ≤ 2.

## Cloud apply gate

`ORCHESTRATOR_CLOUD_APPLY=1` is the only way a cloud runner executes. Without it every cloud
provider returns `planned`. The person sees that on the readiness panel ("cloud apply: off")
and on each planned tile. The hooks ask for confirmation before a coding agent runs a cloud
mutating command even when the variable is set.

## Tests (`tests/test_policy.py`)

- Each error rule has one positive and one negative sample (table-driven).
- `check_artifact` rejects a path `../x.sh`, `/etc/x`, `app/evil.sh`; accepts `ci/x.sh`,
  `.github/workflows/gen-container_scan.yml`, `docs/deploy/aws-dev.md`.
- A workflow with `on: push` fails; `on: workflow_dispatch` with pinned actions and
  `permissions:` passes with no warnings.
- `check_commands([{"argv": ["bash", "-c", "rm -rf /"]}])` → error.
- `prod.reference` exemption: `# never production` passes; `--target production` fails.
- `rules_summary()` returns one line per rule.
- `test_rule_ids_match_skill` (skipped when the skill directory is absent).
