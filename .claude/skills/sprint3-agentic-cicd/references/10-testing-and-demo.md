# 10 — Testing, verification, and the live demo (every phase; Phase 7)

Tests are offline, deterministic, and fast (the Sprint 2 suite runs in ~2 s; keep Sprint 3
under ~20 s). Every phase ends with `bash .claude/skills/sprint3-agentic-cicd/scripts/run_sprint3_checks.sh`
green and one commit.

## Test files

| File | Covers |
| --- | --- |
| `tests/test_registry.py` | registry names, validation, `StageSpec` defaults, `CandidatePipeline` uat rules, `normalize_stages` with custom names, `planned` in `pending_stages` (`01`) |
| `tests/test_retrieval.py` | index build/caching, `.env` exclusion, topic context, determinism (`05`) |
| `tests/test_episodes.py` | record/recall/limit/upsert/other repo (`05`) |
| `tests/test_credentials.py` | names, readiness modes, no values leak, `.env.example` sync, redaction patterns (`07`) |
| `tests/test_policy.py` | every rule positive/negative, path rules, workflow triggers, command check, skill rule parity (`08`) |
| `tests/test_providers.py` | detect_all, plan-mode results per runner, local echo execution, scanner catalog argv, generic findings, `execute_stage_v2` policy block (`06`) |
| `tests/test_generator.py` | skeleton rendering, `write()` records with sha256, path rules, materialize gated by `ORCHESTRATOR_WRITE_ARTIFACTS`, env manifest from registry (`03`) |
| `tests/test_orchestrator.py` (extend) | Jev new edges shapes; full fake run with a generated echo stage; follow-up round; planned cloud stage still concludes `passed`; artifact review `reject` drops the stage; no production anywhere (`02`, `04`) |
| `tests/test_session_api.py` (extend) | follow-up gate round trip; approval refused on policy errors; `/readiness`, `/session/artifacts`, `/episodes` endpoints |
| `tests/test_pipeline_rules.py` (keep) | fixed behavior pinned; `test_only_the_session_workflow_runs_on_push` now also covers any `gen-*.yml` |

## Fake doubles (`tests/test_orchestrator.py`)

```python
class FakeAstra:              # extend, keep the Sprint 2 behavior
    def write_questions(...)  # adds env_platform
    def follow_up(payload)    # one question on rounds == 1 and payload has "retrieved"; else none
    def propose(payload)      # deterministic: build→test→uat ; stochastic: build→test→container_scan(generated, runner local)→uat
    def generate_artifacts(payload)  # one shell artifact ci/<stage>.sh: "#!/usr/bin/env bash\nset -euo pipefail\necho generated-ok\n"
    def write_docs(payload)   # DeployDoc(path="docs/deploy/local-local.md", content="# demo\n")
    def brief / advise / diagnose  # unchanged

class FakeJev:
    def clarify(...)          # unchanged
    def follow_up(payload)    # ask_more when payload["rounds"] == 1 and payload.get("retrieved"), else proceed
    def rank(payload)         # first candidate unless payload has "prefer" id
    def runner(payload)       # every stage -> its first runner option
    def artifact_review(payload)  # accept when policy.ok else revise (once) else reject
    def gate(...)             # unchanged; ignores planned

class PlannedCloudAstra(FakeAstra):   # stochastic candidate adds deploy_dev with runner aws_codebuild
```

`conftest.py` adds an autouse fixture that unsets `ORCHESTRATOR_CLOUD_APPLY`,
`ORCHESTRATOR_WRITE_ARTIFACTS`, `ORCHESTRATOR_EMBEDDINGS`, `ORCHESTRATOR_MCP`, and every
cloud credential name (from `credentials.all_names()` minus the app names) so a developer's
shell never changes a test outcome. The `.env` loader only knows the app names, so this is
safe.

## Key end-to-end assertions

- `test_generated_stage_runs_local_echo`: trace contains `container_scan`; its result row has
  `runner == "local"`, `kind == "generated"`, `status == "pass"`, `evidence` contains
  `generated-ok`; `generated_artifacts["container_scan"][0]["sha256"]` is 64 hex chars; the
  file exists under `var/generated/<run>/container_scan/ci/container_scan.sh` (use a tmp
  `ORCHESTRATOR_VAR_DIR` or pass `root=tmp_path`).
- `test_follow_up_round`: trace has `clarify_with_user` twice, `follow_up` once; answers
  include the follow-up id; `clarify_rounds == 2`; no third round.
- `test_planned_cloud_stage_does_not_fail_the_run`: `deploy_dev` row `status == "planned"`,
  `plan["missing"]` non-empty, gate route `pass`, final status `passed`, `aws_called` false.
- `test_artifact_review_reject_drops_stage`: a policy-failing artifact (contains
  `--target production`) → stage removed, `policy` has the `prod.reference` finding, run
  still reaches `uat`.
- `test_no_production_anywhere`: rendered report, machine report JSON, and every generated
  artifact text contain no `production` word except the documented exemption line.

## Verification commands (the gate script runs these)

```bash
PY=/opt/anaconda3/envs/tcs_practice_env/bin/python
$PY -m ruff check app mcp_server orchestrator tests scripts
$PY -m compileall -q app mcp_server orchestrator scripts
$PY -m pytest -q
python3 .claude/skills/sprint3-agentic-cicd/scripts/policy_check.py .github/workflows var/generated ci deploy
python3 .claude/skills/sprint3-agentic-cicd/scripts/validate_workflow.py .github/workflows
(cd frontend && npm run build)        # when frontend/node_modules exists
```

## Demo (Phase 7)

Three ways to run it; rehearse A, then B, then C when the GitHub token is valid.

**A. Local processes (fastest to iterate)**
```bash
conda activate tcs_practice_env
ORCHESTRATOR_AUTOSTART=1 ORCHESTRATOR_DISPATCH=local uvicorn app.main:app --port 8000
cd frontend && npm run dev            # http://127.0.0.1:5173/#/orchestrator
```
Docker runner is live (daemon on the host), cloud runners are plan mode unless credentials
are set.

**B. Compose (what the push does)**
```bash
docker compose up --build             # http://127.0.0.1:8000/#/orchestrator
```
Inside the container the docker runner is plan mode unless the socket line in
`docker-compose.yml` is uncommented.

**C. Push** → Actions log prints the tunnel URL; `ORCHESTRATOR_DISPATCH=github`; fixed stages
dispatch their workflows; generated workflows dispatch only when
`ORCHESTRATOR_WRITE_ARTIFACTS=1` and the token can push a branch (`contents: write`).

**Script to say** (per screen)
1. Readiness: "The orchestrator knows which CLIs and which credential names exist. AWS CLI is
   present, credentials are not, so AWS routes will plan, not apply."
2. Questions: "Astra read the checkout with retrieval; it asks about platform and runner,
   and tells us what is missing."
3. Follow-up: "Jev decided one more round was worth it."
4. Candidates: "A deterministic route and a stochastic route. Jev's probabilities are shown.
   The stochastic route adds a container scan and a dev deploy on CodeBuild."
5. Artifacts: "Astra wrote this script, this buildspec, this env manifest, and this doc. The
   policy checked them: pinned actions, placeholders only, no production."
6. Approve → stages run: "The scan ran here for real; the CodeBuild deploy is planned with
   the exact command and the variables it needs."
7. UAT sign-off → results: "Findings, next steps, retrieval evidence, and the episode."
8. Second run: "Astra recalls the first episode and proposes with that lesson."

## Demo-ready acceptance checklist

- [ ] `run_sprint3_checks.sh` green; test count ≥ 55 + the new files.
- [ ] Readiness panel renders before a session; no value ever appears (search the page
      source for a known fake value set in the shell).
- [ ] A stochastic candidate with ≥ 1 generated stage appears every run (if Astra proposes
      none, the trace says so and the demo still works).
- [ ] Generated artifacts visible before approval with policy badges; approve disabled on
      policy errors.
- [ ] A generated local/docker stage executes and its tile shows findings or `generated-ok`.
- [ ] A cloud stage renders `planned` amber with the missing names; gate passes.
- [ ] UAT gate still blocks; no production reference in report, state, or files.
- [ ] Episode recorded; second run shows it in Episodes and in Astra's questions.
- [ ] `docs/SYSTEM_MAP.md`, `docs/ORCHESTRATOR_RUNBOOK.md`, `README.md`, `.env.example`
      updated; `git status` clean after the phase commit.
- [ ] The push workflow still passes `test_only_the_session_workflow_runs_on_push`.

## Pitfalls seen in this repo

- The Responses API path rejects `temperature` for some models: `invoke_structured` retries
  without it.
- `gh` on this machine has an invalid keyring token (`gh auth refresh -h github.com`); local
  GitHub dispatch fails until refreshed; local mode does not need it.
- GitHub Actions ignores workflow files in subfolders; generated workflows must be
  `.github/workflows/gen-*.yml`.
- `tests/test_pipeline_rules.py::test_demo_module_does_not_script_answers` fails if the
  string `env_target` appears in `orchestrator/demo.py`; keep demo helpers out of that file.
- `session._run` allows 12 interrupt resumes; two question rounds + approval + uat + retries
  fit; do not add more gates without raising it.
- The Docker image copies `.github`; a generated workflow written at runtime inside the
  container is not on the GitHub ref until `publish_branch` pushes it.
