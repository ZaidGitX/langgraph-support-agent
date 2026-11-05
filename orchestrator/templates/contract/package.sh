#!/usr/bin/env bash
set -euo pipefail
# Orchestrator platform contract for the "package" stage (Sprint 4): what package.yml does,
# then the artifact travels with the run (S3 on AWS) instead of a GitHub artifact.
STAGE="package"
: "${RUN_ID:=local}"
: "${TIER:=dev}"
: "${PLATFORM:=local}"
APP_NAME="__APP_NAME__"
case "$TIER" in production|prod) echo "No path past UAT."; exit 2;; esac
mkdir -p "var/$STAGE"
summary() { printf '{"type":"stage_summary","run_id":"%s","platform":"%s","tier":"%s","stage":"%s","status":"%s"%s}\n' "$RUN_ID" "$PLATFORM" "$TIER" "$STAGE" "$1" "${2:-}"; }

python -m orchestrator.local_ops package
artifact="dist/$APP_NAME.tgz"
test -f "$artifact"
location="$artifact"
if [ "$PLATFORM" = "aws" ] && [ -n "${AWS_S3_BUCKET:-}" ]; then
  location="s3://$AWS_S3_BUCKET/orchestrator/$RUN_ID/dist/$APP_NAME.tgz"
  aws s3 cp "$artifact" "$location" --region "${AWS_REGION:?AWS_REGION is required}"
fi
summary passed ",\"artifact\":\"$location\""
