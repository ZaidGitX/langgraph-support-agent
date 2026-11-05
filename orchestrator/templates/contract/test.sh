#!/usr/bin/env bash
set -euo pipefail
# Orchestrator platform contract for the "test" stage (Sprint 4): what .github/workflows/test.yml
# does, with a JUnit report under var/test/ for the platform's test reporting.
STAGE="test"
: "${RUN_ID:=local}"
: "${TIER:=dev}"
: "${PLATFORM:=local}"
APP_NAME="__APP_NAME__"
case "$TIER" in production|prod) echo "No path past UAT."; exit 2;; esac
mkdir -p "var/$STAGE"
summary() { printf '{"type":"stage_summary","run_id":"%s","platform":"%s","tier":"%s","stage":"%s","status":"%s"%s}\n' "$RUN_ID" "$PLATFORM" "$TIER" "$STAGE" "$1" "${2:-}"; }

python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements-dev.txt
rc=0
python -m pytest -q --junitxml="var/$STAGE/pytest.xml" 2>&1 | tee "var/$STAGE/pytest.log" || rc=$?
passed="$(grep -Eo '[0-9]+ passed' "var/$STAGE/pytest.log" | tail -1 || true)"
if [ "$rc" -ne 0 ]; then
  summary failed ",\"pytest_exit\":$rc,\"junit\":\"var/test/pytest.xml\""
  exit "$rc"
fi
summary passed ",\"pytest\":\"${passed:-unknown}\",\"junit\":\"var/test/pytest.xml\""
