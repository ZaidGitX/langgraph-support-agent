#!/usr/bin/env bash
set -euo pipefail
# Orchestrator platform contract for the "dast" stage (Sprint 4): what dast.yml does against
# the deployed service: a reachability check, the OWASP ZAP baseline scan (required), and the
# StackHawk scan when HAWK_API_KEY and an application UUID in HAWK_APP_ID are provided.
# ZAP FAIL-level alerts fail the stage; warnings are reported. Nothing prints a secret value.
set -euo pipefail
STAGE="dast"
: "${RUN_ID:=local}"
: "${TIER:=dev}"
: "${PLATFORM:=aws}"
APP_NAME="__APP_NAME__"
PORT="__PORT__"
ZAP_IMAGE="__ZAP_IMAGE__"
HAWK_IMAGE="__HAWK_IMAGE__"
case "$TIER" in production|prod) echo "No path past UAT."; exit 2;; esac
OUT="var/$STAGE"
mkdir -p "$OUT"
summary() { printf '{"type":"stage_summary","run_id":"%s","platform":"%s","tier":"%s","stage":"%s","status":"%s"%s}\n' "$RUN_ID" "$PLATFORM" "$TIER" "$STAGE" "$1" "${2:-}"; }

app_url() {
  local region="${AWS_REGION:?AWS_REGION is required}"
  local cluster="${AWS_ECS_CLUSTER:-$APP_NAME-$TIER}" service="${AWS_ECS_SERVICE:-$APP_NAME-$TIER}"
  if [ -n "${APP_PUBLIC_URL:-}" ]; then printf '%s' "${APP_PUBLIC_URL%/}"; return 0; fi
  if [ -n "${AWS_S3_BUCKET:-}" ] && aws s3 cp "s3://$AWS_S3_BUCKET/orchestrator/$RUN_ID/state/app_url.txt" "$OUT/app_url.txt" --region "$region" >/dev/null 2>&1; then
    tr -d '[:space:]' < "$OUT/app_url.txt"; return 0
  fi
  local task eni ip
  task="$(aws ecs list-tasks --cluster "$cluster" --service-name "$service" --desired-status RUNNING --query 'taskArns[0]' --output text --region "$region")"
  [ -n "$task" ] && [ "$task" != "None" ] || return 1
  eni="$(aws ecs describe-tasks --cluster "$cluster" --tasks "$task" --query "tasks[0].attachments[0].details[?name=='networkInterfaceId'].value | [0]" --output text --region "$region")"
  ip="$(aws ec2 describe-network-interfaces --network-interface-ids "$eni" --query 'NetworkInterfaces[0].Association.PublicIp' --output text --region "$region")"
  [ -n "$ip" ] && [ "$ip" != "None" ] || return 1
  printf 'http://%s:%s' "$ip" "$PORT"
}

base="$(app_url || true)"
if [ -z "$base" ]; then
  summary failed ',"reason":"no deployed service URL could be found"'
  exit 1
fi
curl -sf --max-time 15 "$base/status" -o "$OUT/status.json"
python -c 'import json,sys; assert isinstance(json.load(open(sys.argv[1])), dict)' "$OUT/status.json"
docker info >/dev/null

# ZAP baseline: exit 0 pass, 1 FAIL alerts, 2 WARN alerts (ignored with -I), 3 the scan did not run.
rc=0
docker run --rm -u root -v "$PWD/$OUT:/zap/wrk:rw" "$ZAP_IMAGE" zap-baseline.py -t "$base" -J zap.json -r zap.html -I >/dev/null 2>&1 || rc=$?
case "$rc" in
  0) zap="passed";;
  1) zap="failed";;
  *) summary failed ",\"url\":\"$base\",\"reason\":\"ZAP did not complete (exit $rc)\""; exit 1;;
esac

# StackHawk: only with its API key and an application UUID; stackhawk.yml is in the checkout.
hawk="skipped"
if [ -n "${HAWK_API_KEY:-}" ] && printf '%s' "${HAWK_APP_ID:-}" | grep -Eq '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'; then
  rc=0
  docker run --rm -e HAWK_API_KEY -e HAWK_APP_ID -e "APP_HOST=$base" -v "$PWD:/hawk:rw" "$HAWK_IMAGE" > "$OUT/hawk.log" 2>&1 || rc=$?
  case "$rc" in 0) hawk="passed";; 42) hawk="failed";; *) hawk="error";; esac
fi
if [ "$zap" = "failed" ] || [ "$hawk" = "failed" ]; then
  summary failed ",\"url\":\"$base\",\"zap\":\"$zap\",\"stackhawk\":\"$hawk\",\"report\":\"var/dast/zap.json\""
  exit 1
fi
summary passed ",\"url\":\"$base\",\"zap\":\"$zap\",\"stackhawk\":\"$hawk\",\"report\":\"var/dast/zap.json\""
