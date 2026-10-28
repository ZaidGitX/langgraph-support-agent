#!/usr/bin/env bash
# Sprint 3 gate: ruff, compileall, pytest, artifact policy, workflow validation, frontend build.
# Writes var/.sprint3-checks.json so the Stop hook and sprint3_progress.py know the last result.
#
#   bash .claude/skills/sprint3-agentic-cicd/scripts/run_sprint3_checks.sh [--quick] [--no-frontend] [--root DIR]
#
# --quick       skip the frontend build
# --no-frontend same as --quick
# --root DIR    check that checkout instead of the one this skill lives in (also SPRINT3_ROOT=DIR);
#               use it for a repo copy so var/ and the stamp are written there, never in the original
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ROOT="${SPRINT3_ROOT:-${CLAUDE_PROJECT_DIR:-$(cd "$SKILL_DIR/../../.." && pwd)}}"
ARGS=("$@")
for ((i = 0; i < ${#ARGS[@]}; i++)); do
  if [ "${ARGS[$i]}" = "--root" ] && [ $((i + 1)) -lt ${#ARGS[@]} ]; then
    ROOT="$(cd "${ARGS[$((i + 1))]}" && pwd)"
  fi
done
PY="${PY:-/opt/anaconda3/envs/tcs_practice_env/bin/python}"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3)"
  echo "warning: project interpreter not found, using $PY" >&2
fi
export AWS_EC2_METADATA_DISABLED=true

QUICK=0
for arg in "$@"; do
  case "$arg" in
    --quick|--no-frontend) QUICK=1 ;;
  esac
done

cd "$ROOT" || exit 2
mkdir -p var
declare -a FAILED=()
START=$(date +%s)

step() {
  local name="$1"; shift
  echo "==> $name"
  if "$@"; then
    echo "    ok: $name"
  else
    echo "    FAIL: $name"
    FAILED+=("$name")
  fi
}

step "ruff" "$PY" -m ruff check app mcp_server orchestrator tests scripts
step "compileall" "$PY" -m compileall -q app mcp_server orchestrator scripts
step "pytest" "$PY" -m pytest -q -p no:cacheprovider
step "policy" python3 "$SCRIPT_DIR/policy_check.py" --root "$ROOT" .github/workflows var/generated ci deploy docker
step "workflows" python3 "$SCRIPT_DIR/validate_workflow.py" .github/workflows
if [ "$QUICK" -eq 0 ] && [ -d frontend/node_modules ]; then
  step "frontend build" bash -c 'cd frontend && npm run build --silent'
else
  echo "==> frontend build skipped (--quick or frontend/node_modules missing)"
fi

END=$(date +%s)
OK=true
if [ "${#FAILED[@]}" -gt 0 ]; then OK=false; fi
FAILED_JSON=$(printf '%s\n' "${FAILED[@]:-}" | python3 -c 'import json,sys; items=[l for l in sys.stdin.read().splitlines() if l]; print(json.dumps(items))')
python3 - "$OK" "$FAILED_JSON" "$START" "$END" <<'PY'
import json, sys, time
ok = sys.argv[1] == "true"
failed = json.loads(sys.argv[2])
start, end = int(sys.argv[3]), int(sys.argv[4])
stamp = {"ok": ok, "failed": failed, "timestamp": time.time(),
         "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%S"), "duration_s": end - start}
with open("var/.sprint3-checks.json", "w", encoding="utf-8") as fh:
    json.dump(stamp, fh, indent=2)
PY

echo
if [ "$OK" = true ]; then
  echo "sprint 3 checks: ALL GREEN ($((END - START))s)"
  exit 0
fi
echo "sprint 3 checks: FAILED -> ${FAILED[*]} ($((END - START))s)"
exit 1
