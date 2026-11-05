#!/usr/bin/env bash
set -euo pipefail
# Orchestrator platform contract for a post-deploy verification stage on AWS (Sprint 4): find
# the deployed service's URL (APP_PUBLIC_URL, else the URL the deploy stage published, else the
# running task's public IP) and exercise the application's routes.
STAGE="${STAGE_NAME:-smoke}"
: "${RUN_ID:=local}"
: "${TIER:=dev}"
: "${PLATFORM:=aws}"
APP_NAME="__APP_NAME__"
PORT="__PORT__"
case "$TIER" in production|prod) echo "No path past UAT."; exit 2;; esac
mkdir -p "var/$STAGE"
summary() { printf '{"type":"stage_summary","run_id":"%s","platform":"%s","tier":"%s","stage":"%s","status":"%s"%s}\n' "$RUN_ID" "$PLATFORM" "$TIER" "$STAGE" "$1" "${2:-}"; }

app_url() {
  local region="${AWS_REGION:?AWS_REGION is required}"
  local cluster="${AWS_ECS_CLUSTER:-$APP_NAME-$TIER}" service="${AWS_ECS_SERVICE:-$APP_NAME-$TIER}"
  if [ -n "${APP_PUBLIC_URL:-}" ]; then printf '%s' "${APP_PUBLIC_URL%/}"; return 0; fi
  if [ -n "${AWS_S3_BUCKET:-}" ] && aws s3 cp "s3://$AWS_S3_BUCKET/orchestrator/$RUN_ID/state/app_url.txt" "var/$STAGE/app_url.txt" --region "$region" >/dev/null 2>&1; then
    tr -d '[:space:]' < "var/$STAGE/app_url.txt"; return 0
  fi
  local task eni ip
  task="$(aws ecs list-tasks --cluster "$cluster" --service-name "$service" --desired-status RUNNING --query 'taskArns[0]' --output text --region "$region")"
  [ -n "$task" ] && [ "$task" != "None" ] || return 1
  eni="$(aws ecs describe-tasks --cluster "$cluster" --tasks "$task" --query "tasks[0].attachments[0].details[?name=='networkInterfaceId'].value | [0]" --output text --region "$region")"
  ip="$(aws ec2 describe-network-interfaces --network-interface-ids "$eni" --query 'NetworkInterfaces[0].Association.PublicIp' --output text --region "$region")"
  [ -n "$ip" ] && [ "$ip" != "None" ] || return 1
  printf 'http://%s:%s' "$ip" "$PORT"
}

url="$(app_url || true)"
if [ -z "$url" ]; then
  summary failed ',"reason":"no deployed service URL could be found"'
  exit 1
fi
checks=0
fail() { summary failed ",\"url\":\"$url\",\"reason\":\"$1\""; exit 1; }
curl -sf --max-time 15 "$url/status" -o "var/$STAGE/status.json" || fail "GET /status did not answer"
python -c 'import json,sys; body=json.load(open(sys.argv[1])); assert isinstance(body, dict)' "var/$STAGE/status.json" || fail "/status is not a JSON object"
checks=$((checks + 1))
curl -sf --max-time 15 "$url/api/support/status" -o "var/$STAGE/support-status.json" || fail "GET /api/support/status did not answer"
checks=$((checks + 1))
code="$(curl -s --max-time 15 -o /dev/null -w '%{http_code}' "$url/")"
case "$code" in 200|304) checks=$((checks + 1));; *) fail "GET / returned HTTP $code";; esac
echo "APP_PUBLIC_URL=$url"
summary passed ",\"url\":\"$url\",\"checks\":$checks"
