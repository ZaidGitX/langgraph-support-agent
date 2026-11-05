#!/usr/bin/env bash
set -euo pipefail
# Orchestrator platform contract for the "container" stage (Sprint 4): build the image and,
# on AWS, push it to the ECR repository as <repository>:<run id>, the tag every later stage
# (release, deploy, smoke) relies on. The image scan of ci.yml runs informationally after it.
STAGE="container"
: "${RUN_ID:=local}"
: "${TIER:=dev}"
: "${PLATFORM:=local}"
APP_NAME="__APP_NAME__"
TRIVY_IMAGE="__TRIVY_IMAGE__"
case "$TIER" in production|prod) echo "No path past UAT."; exit 2;; esac
mkdir -p "var/$STAGE"
summary() { printf '{"type":"stage_summary","run_id":"%s","platform":"%s","tier":"%s","stage":"%s","status":"%s"%s}\n' "$RUN_ID" "$PLATFORM" "$TIER" "$STAGE" "$1" "${2:-}"; }

docker info >/dev/null
image="$APP_NAME:$RUN_ID"
if [ "$PLATFORM" = "aws" ]; then
  : "${AWS_REGION:?AWS_REGION is required}"
  : "${AWS_ECR_REPOSITORY:?AWS_ECR_REPOSITORY is required}"
  account="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text --region "$AWS_REGION")}"
  registry="$account.dkr.ecr.$AWS_REGION.amazonaws.com"
  image="$registry/$AWS_ECR_REPOSITORY:$RUN_ID"
  aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$registry" >/dev/null
fi

# The deployed service does not need the platform CLIs inside its image.
docker build --build-arg WITH_CLOUD_CLIS=0 -t "$image" .
if [ "$PLATFORM" = "aws" ]; then
  docker push "$image"
fi

# Image scan as in ci.yml: informational here; a container_scan stage decides on its own.
rc=0
if docker run --rm -v /var/run/docker.sock:/var/run/docker.sock "$TRIVY_IMAGE" image --severity HIGH,CRITICAL --exit-code 0 \
    --format json -o /tmp/trivy-image.json "$image" >/dev/null 2>&1; then
  echo "trivy image scan completed (informational)"
else
  rc=$?
  echo "trivy image scan did not complete (exit $rc); informational"
fi
digest="$(docker inspect --format '{{index .RepoDigests 0}}' "$image" 2>/dev/null || true)"
summary passed ",\"image\":\"$image\",\"digest\":\"${digest:-local}\""
