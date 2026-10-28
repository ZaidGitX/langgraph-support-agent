#!/usr/bin/env bash
# Sprint 3 demo walkthrough. Prints the steps; --start runs the local API, --compose runs Docker Compose.
#
#   bash .claude/skills/sprint3-agentic-cicd/scripts/demo_walkthrough.sh            # print the script
#   bash .claude/skills/sprint3-agentic-cicd/scripts/demo_walkthrough.sh --start    # uvicorn on :8000 (local dispatch)
#   bash .claude/skills/sprint3-agentic-cicd/scripts/demo_walkthrough.sh --compose  # docker compose up --build
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../../../.." && pwd)}"
PY="${PY:-/opt/anaconda3/envs/tcs_practice_env/bin/python}"
cd "$ROOT" || exit 2

cat <<'TXT'
Sprint 3 demo — agentic CI/CD orchestrator

Preconditions
  1. .env has OPENAI_API_KEY and TYPESAFE_API_KEY (names only are ever shown).
  2. Cloud credentials are intentionally absent -> cloud routes run in plan mode.
  3. Docker daemon running (docker runner executes locally); run checks first:
       bash .claude/skills/sprint3-agentic-cicd/scripts/run_sprint3_checks.sh

Screens and what to say
  Readiness   "CLIs and credential NAMES detected. AWS CLI present, credentials missing -> plan mode."
  Questions   "Astra read the checkout (retrieval) and asks about platform, runner, test depth, scan policy."
  Follow-up   "Jev (TypeSafe Noul+Choice) decided one more round was worth it."
  Candidates  "Deterministic route vs stochastic route with generated stages; Jev's probabilities shown."
  Artifacts   "Astra wrote the script/buildspec/env manifest/doc; policy verdicts per file; approve is gated."
  Run         "Local/docker stages execute; cloud stages are planned with the exact command and missing names."
  UAT         "Human sign-off; there is no production edge."
  Results     "Findings, next steps, retrieval evidence, Jev decisions, the recorded episode."
  Second run  "Astra recalls the first episode in its questions and proposals."

URLs
  local:   http://127.0.0.1:8000/#/orchestrator   (or http://127.0.0.1:5173/#/orchestrator with `npm run dev`)
  compose: http://127.0.0.1:8000/#/orchestrator
  push:    the Actions log prints the trycloudflare URL
TXT

case "${1:-}" in
  --start)
    echo
    echo "starting uvicorn (local dispatch, autostart on) ..."
    export ORCHESTRATOR_AUTOSTART=1 ORCHESTRATOR_DISPATCH=local AWS_EC2_METADATA_DISABLED=true
    exec "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
    ;;
  --compose)
    echo
    echo "starting docker compose ..."
    exec docker compose up --build
    ;;
  *)
    ;;
esac
