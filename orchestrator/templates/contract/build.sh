#!/usr/bin/env bash
set -euo pipefail
# Orchestrator platform contract for the "build" stage (Sprint 4). Code-written: it does what
# .github/workflows/build.yml and the audit step of ci.yml do, on any platform's agent.
# A stage buildspec runs `bash ci/build.sh`; Astra may add steps before or after it.
STAGE="build"
: "${RUN_ID:=local}"
: "${TIER:=dev}"
: "${PLATFORM:=local}"
APP_NAME="__APP_NAME__"
case "$TIER" in production|prod) echo "No path past UAT."; exit 2;; esac
mkdir -p "var/$STAGE"
summary() { printf '{"type":"stage_summary","run_id":"%s","platform":"%s","tier":"%s","stage":"%s","status":"%s"%s}\n' "$RUN_ID" "$PLATFORM" "$TIER" "$STAGE" "$1" "${2:-}"; }

python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements-dev.txt
python -m compileall -q app mcp_server orchestrator
ruff check app mcp_server orchestrator tests scripts

# Dependency audit: fail only on a vulnerability that has a fix (scripts/audit_gate.py).
set +e
pip-audit -r requirements.txt --format json -o "var/$STAGE/audit.json"
set -e
python scripts/audit_gate.py "var/$STAGE/audit.json"
summary passed ',"checks":["compileall","ruff","pip-audit"]'
