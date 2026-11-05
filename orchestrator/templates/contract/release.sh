#!/usr/bin/env bash
set -euo pipefail
# Orchestrator platform contract for the "release" stage (Sprint 4): promote the run's image
# tag to the tier tag in the registry (no rebuild). On AWS: ECR <repository>:<run id> is
# re-tagged <repository>:<tier> through the manifest. Elsewhere the local release plan is written.
STAGE="release"
: "${RUN_ID:=local}"
: "${TIER:=dev}"
: "${PLATFORM:=local}"
APP_NAME="__APP_NAME__"
case "$TIER" in production|prod) echo "No path past UAT."; exit 2;; esac
mkdir -p "var/$STAGE"
summary() { printf '{"type":"stage_summary","run_id":"%s","platform":"%s","tier":"%s","stage":"%s","status":"%s"%s}\n' "$RUN_ID" "$PLATFORM" "$TIER" "$STAGE" "$1" "${2:-}"; }

if [ "$PLATFORM" != "aws" ]; then
  python -m orchestrator.local_ops release
  summary passed ',"promotion":"local release plan written to dist/release.json"'
  exit 0
fi
: "${AWS_REGION:?AWS_REGION is required}"
: "${AWS_ECR_REPOSITORY:?AWS_ECR_REPOSITORY is required}"
manifest="var/$STAGE/manifest.json"
aws ecr batch-get-image --repository-name "$AWS_ECR_REPOSITORY" --image-ids imageTag="$RUN_ID" \
  --query 'images[0].imageManifest' --output text --region "$AWS_REGION" > "$manifest"
test -s "$manifest"
if aws ecr describe-images --repository-name "$AWS_ECR_REPOSITORY" --image-ids imageTag="$TIER" --region "$AWS_REGION" >/dev/null 2>&1; then
  aws ecr batch-delete-image --repository-name "$AWS_ECR_REPOSITORY" --image-ids imageTag="$TIER" --region "$AWS_REGION" >/dev/null
fi
aws ecr put-image --repository-name "$AWS_ECR_REPOSITORY" --image-tag "$TIER" --image-manifest "file://$manifest" --region "$AWS_REGION" >/dev/null
digest="$(aws ecr describe-images --repository-name "$AWS_ECR_REPOSITORY" --image-ids imageTag="$RUN_ID" --query 'imageDetails[0].imageDigest' --output text --region "$AWS_REGION")"
summary passed ",\"promotion\":\"$AWS_ECR_REPOSITORY:$RUN_ID -> $AWS_ECR_REPOSITORY:$TIER\",\"digest\":\"$digest\""
