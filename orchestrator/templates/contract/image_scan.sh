#!/usr/bin/env bash
set -euo pipefail
# Orchestrator platform contract for an image scan stage (container_scan, image_scan) on AWS:
# scan the run's image, which the container contract pushed to ECR as <repository>:<run id>.
# No stage leaves an image archive behind (live run 16's generated scan looked for one); the
# image is pulled from the registry. Vulnerabilities without a released fix are ignored (a slim
# Debian base always carries some, live run 14) and fixable findings at SCAN_SEVERITY fail.
STAGE="image_scan"
: "${RUN_ID:=local}"
: "${TIER:=dev}"
: "${PLATFORM:=aws}"
: "${SCAN_SEVERITY:=HIGH,CRITICAL}"
APP_NAME="__APP_NAME__"
TRIVY_IMAGE="__TRIVY_IMAGE__"
case "$TIER" in production|prod) echo "No path past UAT."; exit 2;; esac
OUT="var/$STAGE"
mkdir -p "$OUT"
summary() { printf '{"type":"stage_summary","run_id":"%s","platform":"%s","tier":"%s","stage":"%s","status":"%s"%s}\n' "$RUN_ID" "$PLATFORM" "$TIER" "$STAGE" "$1" "${2:-}"; }

: "${AWS_REGION:?AWS_REGION is required}"
: "${AWS_ECR_REPOSITORY:?AWS_ECR_REPOSITORY is required}"
docker info >/dev/null
account="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text --region "$AWS_REGION")}"
registry="$account.dkr.ecr.$AWS_REGION.amazonaws.com"
image="$registry/$AWS_ECR_REPOSITORY:$RUN_ID"
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$registry" >/dev/null

# The run's image: pushed by a container stage, else built and pushed here.
if ! aws ecr describe-images --repository-name "$AWS_ECR_REPOSITORY" --image-ids imageTag="$RUN_ID" --region "$AWS_REGION" >/dev/null 2>&1; then
  echo "image $image is not in ECR yet; building it in this stage"
  bash ci/container.sh
fi
docker pull "$image" >/dev/null

rc=0
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$PWD/$OUT:/out" "$TRIVY_IMAGE" image \
  --scanners vuln --ignore-unfixed --severity "$SCAN_SEVERITY" --exit-code 1 \
  --format json -o /out/trivy-image.json "$image" > "$OUT/trivy-image.log" 2>&1 || rc=$?
if [ "$rc" -eq 1 ]; then
  python - "$OUT/trivy-image.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
rows = []
for res in data.get("Results", []):
    for item in res.get("Vulnerabilities") or []:
        rows.append(f"image finding: {res.get('Target')} {item.get('PkgName')} {item.get('InstalledVersion')} -> {item.get('FixedVersion')} {item.get('Severity')} {item.get('VulnerabilityID')}")
for row in rows[:25]:
    print(row)
if len(rows) > 25:
    print(f"image finding: ... {len(rows) - 25} more in var/image_scan/trivy-image.json")
PY
  summary failed ",\"image\":\"$image\",\"severity\":\"$SCAN_SEVERITY\",\"reason\":\"fixable vulnerabilities, listed above\",\"report\":\"var/image_scan/trivy-image.json\""
  exit 1
fi
if [ "$rc" -ne 0 ]; then
  tail -n 5 "$OUT/trivy-image.log" 2>/dev/null | sed 's/^/trivy: /'
  summary failed ",\"image\":\"$image\",\"reason\":\"trivy did not complete (exit $rc)\""
  exit 1
fi
summary passed ",\"image\":\"$image\",\"severity\":\"$SCAN_SEVERITY\",\"ignore_unfixed\":true,\"report\":\"var/image_scan/trivy-image.json\""
