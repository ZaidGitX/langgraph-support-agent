#!/usr/bin/env bash
set -euo pipefail
# Orchestrator platform contract for the "scanners" stage (Sprint 4): every scan of
# .github/workflows/scanners.yml, on any platform's agent that has python, node, and docker.
#
#   semgrep (SAST)      pip-audit (SCA)     snyk (SCA, SNYK_TOKEN)      trivy fs (vuln, misconfig, secret)
#   gitleaks (secrets)  sonar (SONAR_*)     codeql (SAST, bundle download, time-capped)
#
# Every tool writes a line to var/scanners/status.jsonl: passed, failed (findings at or above
# SCAN_SEVERITY, default HIGH,CRITICAL), skipped (its credential is not provided, or a
# time cap), or error (the tool itself could not run). A tool the workflow runs informationally
# stays informational. The stage fails only on "failed" lines; skipped and error lines stay
# visible so a person can decide. Credentials are read by name from the environment, never
# printed.
set -euo pipefail
STAGE="scanners"
: "${RUN_ID:=local}"
: "${TIER:=dev}"
: "${PLATFORM:=local}"
: "${SCAN_SEVERITY:=HIGH,CRITICAL}"
: "${CODEQL_ENABLED:=1}"
: "${CODEQL_TIME_CAP:=540}"
APP_NAME="__APP_NAME__"
TRIVY_IMAGE="__TRIVY_IMAGE__"
GITLEAKS_IMAGE="__GITLEAKS_IMAGE__"
SONAR_IMAGE="__SONAR_IMAGE__"
case "$TIER" in production|prod) echo "No path past UAT."; exit 2;; esac
OUT="var/$STAGE"
mkdir -p "$OUT"
LEDGER="$OUT/status.jsonl"
: > "$LEDGER"

record() {  # name status exit_code reason
  printf '{"check":"%s","status":"%s","exit_code":%s,"reason":"%s"}\n' "$1" "$2" "$3" "$4" >> "$LEDGER"
  echo "scanner $1: $2 (exit $3) $4"
}
present() {  # every named variable has a value (never printed)
  local name
  for name in "$@"; do
    [ -n "${!name:-}" ] || return 1
  done
}
has_docker() { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }
summary() { printf '{"type":"stage_summary","run_id":"%s","platform":"%s","tier":"%s","stage":"%s","status":"%s"%s}\n' "$RUN_ID" "$PLATFORM" "$TIER" "$STAGE" "$1" "${2:-}"; }

python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements-dev.txt

# --- semgrep: ERROR-level results are high; WARNING and INFO are reported only -----------------
rc=0
python -m venv "$OUT/semgrep-venv"
"$OUT/semgrep-venv/bin/python" -m pip install --quiet semgrep || rc=$?
if [ "$rc" -ne 0 ]; then
  record semgrep error "$rc" "semgrep could not be installed"
else
  rc=0
  "$OUT/semgrep-venv/bin/semgrep" scan --config p/python --config p/javascript --metrics=off --quiet \
    --json -o "$OUT/semgrep.json" app mcp_server orchestrator frontend/src || rc=$?
  if [ "$rc" -gt 1 ]; then
    record semgrep error "$rc" "semgrep did not complete"
  else
    high="$(python - "$OUT/semgrep.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
print(sum(1 for item in data.get("results", []) if str(item.get("extra", {}).get("severity", "")).upper() == "ERROR"))
PY
)"
    total="$(python -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("results", [])))' "$OUT/semgrep.json")"
    if [ "$high" -gt 0 ]; then record semgrep failed 1 "$high ERROR-level finding(s) of $total"; else record semgrep passed 0 "$total finding(s), none at ERROR level"; fi
  fi
fi

# --- pip-audit: fail only on a vulnerability with a fix (the repository's own gate) ------------
set +e
pip-audit -r requirements.txt --format json -o "$OUT/audit.json"
set -e
rc=0
python scripts/audit_gate.py "$OUT/audit.json" || rc=$?
if [ "$rc" -eq 0 ]; then record pip-audit passed 0 "no fixable vulnerability"; else record pip-audit failed "$rc" "fixable vulnerabilities, see var/scanners/audit.json"; fi

# --- snyk: Python and frontend, when SNYK_TOKEN is provided ------------------------------------
if present SNYK_TOKEN; then
  rc=0
  npm install --global --silent snyk || rc=$?
  if [ "$rc" -ne 0 ]; then
    record snyk-python error "$rc" "snyk could not be installed"
    record snyk-frontend error "$rc" "snyk could not be installed"
  else
    python -m pip install --quiet -r requirements.txt
    rc=0
    snyk test --file=requirements.txt --command=python3 --severity-threshold=high --json-file-output="$OUT/snyk-python.json" >/dev/null || rc=$?
    case "$rc" in 0) record snyk-python passed 0 "no high or critical vulnerability";; 1) record snyk-python failed 1 "high or critical vulnerabilities, see var/scanners/snyk-python.json";; *) record snyk-python error "$rc" "snyk did not complete";; esac
    rc=0
    npm ci --prefix frontend --silent || rc=$?
    if [ "$rc" -ne 0 ]; then
      record snyk-frontend error "$rc" "frontend dependencies could not be installed"
    else
      rc=0
      snyk test --file=frontend/package.json --severity-threshold=high --json-file-output="$OUT/snyk-frontend.json" >/dev/null || rc=$?
      case "$rc" in 0) record snyk-frontend passed 0 "no high or critical vulnerability";; 1) record snyk-frontend failed 1 "high or critical vulnerabilities, see var/scanners/snyk-frontend.json";; *) record snyk-frontend error "$rc" "snyk did not complete";; esac
    fi
  fi
else
  record snyk-python skipped 0 "SNYK_TOKEN is not provided"
  record snyk-frontend skipped 0 "SNYK_TOKEN is not provided"
fi

# --- trivy fs and gitleaks: the catalog's images; findings at SCAN_SEVERITY fail --------------
if has_docker; then
  rc=0
  docker run --rm -v "$PWD:/src" -w /src "$TRIVY_IMAGE" fs --scanners vuln,misconfig,secret --severity "$SCAN_SEVERITY" \
    --exit-code 1 --skip-dirs node_modules,var,.git,frontend/node_modules --format json -o "/src/$OUT/trivy.json" . >/dev/null || rc=$?
  case "$rc" in 0) record trivy passed 0 "no finding at $SCAN_SEVERITY";; 1) record trivy failed 1 "findings at $SCAN_SEVERITY, see var/scanners/trivy.json";; *) record trivy error "$rc" "trivy did not complete";; esac
  rc=0
  docker run --rm -v "$PWD:/src" "$GITLEAKS_IMAGE" detect --no-git --redact --source /src --exit-code 1 \
    --report-format json --report-path "/src/$OUT/gitleaks.json" >/dev/null 2>&1 || rc=$?
  case "$rc" in 0) record gitleaks passed 0 "no secret found";; 1) record gitleaks failed 1 "secrets found, see var/scanners/gitleaks.json";; *) record gitleaks error "$rc" "gitleaks did not complete";; esac
else
  record trivy error 1 "docker is not available on this agent"
  record gitleaks error 1 "docker is not available on this agent"
fi

# --- SonarQube Cloud: when its three names are provided ----------------------------------------
if present SONAR_TOKEN SONAR_ORGANIZATION SONAR_PROJECT_KEY; then
  if has_docker; then
    rc=0
    docker run --rm -e SONAR_TOKEN -e SONAR_HOST_URL=https://sonarcloud.io -v "$PWD:/usr/src" "$SONAR_IMAGE" \
      -Dsonar.organization="$SONAR_ORGANIZATION" -Dsonar.projectKey="$SONAR_PROJECT_KEY" \
      -Dsonar.sources=app,mcp_server,orchestrator,frontend/src -Dsonar.qualitygate.wait=true > "$OUT/sonar.log" 2>&1 || rc=$?
    case "$rc" in 0) record sonar passed 0 "quality gate passed";; *) record sonar failed "$rc" "quality gate failed or the scan did not complete, see var/scanners/sonar.log";; esac
  else
    record sonar error 1 "docker is not available on this agent"
  fi
else
  record sonar skipped 0 "SONAR_TOKEN, SONAR_ORGANIZATION, SONAR_PROJECT_KEY are not all provided"
fi

# --- CodeQL: the bundle is downloaded as a file (never piped to a shell), time-capped ---------
if [ "$CODEQL_ENABLED" = "1" ]; then
  rc=0
  mkdir -p "$OUT/codeql"
  curl -sSL --max-time 300 -o "$OUT/codeql/bundle.tar.gz" \
    https://github.com/github/codeql-action/releases/latest/download/codeql-bundle-linux64.tar.gz || rc=$?
  if [ "$rc" -ne 0 ]; then
    record codeql-python skipped "$rc" "the CodeQL bundle could not be downloaded"
    record codeql-javascript skipped "$rc" "the CodeQL bundle could not be downloaded"
  else
    tar -xzf "$OUT/codeql/bundle.tar.gz" -C "$OUT/codeql"
    CODEQL="$OUT/codeql/codeql/codeql"
    for language in python javascript; do
      rc=0
      timeout "$CODEQL_TIME_CAP" "$CODEQL" database create "$OUT/codeql/db-$language" --language="$language" --source-root . --overwrite --quiet > "$OUT/codeql/$language.log" 2>&1 || rc=$?
      if [ "$rc" -eq 0 ]; then
        timeout "$CODEQL_TIME_CAP" "$CODEQL" database analyze "$OUT/codeql/db-$language" --format=sarif-latest --output="$OUT/codeql-$language.sarif" --quiet >> "$OUT/codeql/$language.log" 2>&1 || rc=$?
      fi
      if [ "$rc" -eq 124 ]; then
        record "codeql-$language" skipped "$rc" "time cap of ${CODEQL_TIME_CAP}s reached"
      elif [ "$rc" -ne 0 ]; then
        record "codeql-$language" error "$rc" "CodeQL did not complete, see var/scanners/codeql/$language.log"
      else
        high="$(python - "$OUT/codeql-$language.sarif" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
rules = {}
for run in data.get("runs", []):
    for rule in (run.get("tool", {}).get("driver", {}).get("rules") or []):
        rules[rule.get("id")] = rule
count = 0
for run in data.get("runs", []):
    for result in run.get("results", []):
        rule = rules.get(result.get("ruleId"), {})
        props = rule.get("properties", {})
        severity = float(props.get("security-severity", 0) or 0)
        if result.get("level") == "error" or severity >= 7.0:
            count += 1
print(count)
PY
)"
        if [ "$high" -gt 0 ]; then record "codeql-$language" failed 1 "$high high or critical result(s)"; else record "codeql-$language" passed 0 "no high or critical result"; fi
      fi
    done
  fi
else
  record codeql-python skipped 0 "CODEQL_ENABLED is not 1"
  record codeql-javascript skipped 0 "CODEQL_ENABLED is not 1"
fi

# --- verdict ---------------------------------------------------------------------------------
failed="$(grep -c '"status":"failed"' "$LEDGER" || true)"
skipped="$(grep -c '"status":"skipped"' "$LEDGER" || true)"
errors="$(grep -c '"status":"error"' "$LEDGER" || true)"
checks="$(python -c 'import json,sys; print(json.dumps([json.loads(l) for l in open(sys.argv[1]) if l.strip()]))' "$LEDGER")"
if [ "$failed" -gt 0 ]; then
  summary failed ",\"failed\":$failed,\"skipped\":$skipped,\"errors\":$errors,\"checks\":$checks"
  exit 1
fi
summary passed ",\"failed\":0,\"skipped\":$skipped,\"errors\":$errors,\"checks\":$checks"
