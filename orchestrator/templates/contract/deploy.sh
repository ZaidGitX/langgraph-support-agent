#!/usr/bin/env bash
set -euo pipefail
# Orchestrator platform contract for the "deploy" stage on AWS (Sprint 4): one Fargate service
# per app and tier from the orchestrator's CloudFormation stack (deploy/ecs-stack.yaml).
#
#   1. tier guard (no path past UAT)                     4. aws cloudformation deploy (creates or updates)
#   2. the run's image must exist in ECR, else build it  5. wait until the service is stable
#   3. VPC and subnets: given, else the default VPC       6. find the task's public IP, check /status,
#                                                           publish the URL for smoke, dast, and UAT
# Every name comes from the environment the CodeBuild project supplies; credentials are the
# service role's. Nothing here reads or prints a secret value.
set -euo pipefail
STAGE="deploy"
: "${RUN_ID:=local}"
: "${TIER:=dev}"
: "${PLATFORM:=aws}"
APP_NAME="__APP_NAME__"
PORT="__PORT__"
case "$TIER" in production|prod) echo "No path past UAT."; exit 2;; esac
mkdir -p "var/$STAGE"
summary() { printf '{"type":"stage_summary","run_id":"%s","platform":"%s","tier":"%s","stage":"%s","status":"%s"%s}\n' "$RUN_ID" "$PLATFORM" "$TIER" "$STAGE" "$1" "${2:-}"; }

: "${AWS_REGION:?AWS_REGION is required}"
: "${AWS_ECR_REPOSITORY:?AWS_ECR_REPOSITORY is required}"
STACK="${AWS_CFN_STACK:-$APP_NAME-$TIER}"
CLUSTER="${AWS_ECS_CLUSTER:-$APP_NAME-$TIER}"
SERVICE="${AWS_ECS_SERVICE:-$APP_NAME-$TIER}"
account="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text --region "$AWS_REGION")}"
image="$account.dkr.ecr.$AWS_REGION.amazonaws.com/$AWS_ECR_REPOSITORY:$RUN_ID"
template="deploy/ecs-stack.yaml"
test -f "$template"

# 2. The image for this run; a container stage normally pushed it, else build it here.
if ! aws ecr describe-images --repository-name "$AWS_ECR_REPOSITORY" --image-ids imageTag="$RUN_ID" --region "$AWS_REGION" >/dev/null 2>&1; then
  echo "image $image is not in ECR yet; building it in this stage"
  bash ci/container.sh
fi

# 3. Networking: the names the project supplies, else the account's default VPC.
vpc="${AWS_VPC_ID:-}"
subnets="${AWS_SUBNET_IDS:-}"
if [ -z "$vpc" ]; then
  vpc="$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text --region "$AWS_REGION")"
fi
if [ -z "$vpc" ] || [ "$vpc" = "None" ]; then
  summary failed ',"reason":"no VPC: set AWS_VPC_ID and AWS_SUBNET_IDS, or create a default VPC"'
  exit 1
fi
if [ -z "$subnets" ]; then
  subnets="$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$vpc" Name=map-public-ip-on-launch,Values=true --query 'Subnets[].SubnetId' --output text --region "$AWS_REGION" | tr '\t' ',')"
fi
if [ -z "$subnets" ] || [ "$subnets" = "None" ]; then
  subnets="$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$vpc" --query 'Subnets[].SubnetId' --output text --region "$AWS_REGION" | tr '\t' ',')"
fi
if [ -z "$subnets" ] || [ "$subnets" = "None" ]; then
  summary failed ",\"reason\":\"no subnet in $vpc: set AWS_SUBNET_IDS\""
  exit 1
fi
echo "stack $STACK in $vpc on $subnets with $image"

# 4. Create or update the stack; the change set is executed for every allowed tier.
aws cloudformation deploy --stack-name "$STACK" --template-file "$template" \
  --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset --region "$AWS_REGION" \
  --parameter-overrides "AppName=$APP_NAME" "Tier=$TIER" "VpcId=$vpc" "SubnetIds=$subnets" "Image=$image" "ContainerPort=$PORT" \
  --tags "orchestrator=$APP_NAME" "run_id=$RUN_ID" "tier=$TIER"
aws cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].StackStatus' --output text --region "$AWS_REGION"

# 5. The service must reach its desired count with the new task definition.
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE" --region "$AWS_REGION"

# 6. The task's public address, then the application's own status route.
task="$(aws ecs list-tasks --cluster "$CLUSTER" --service-name "$SERVICE" --desired-status RUNNING --query 'taskArns[0]' --output text --region "$AWS_REGION")"
eni="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task" --query "tasks[0].attachments[0].details[?name=='networkInterfaceId'].value | [0]" --output text --region "$AWS_REGION")"
ip="$(aws ec2 describe-network-interfaces --network-interface-ids "$eni" --query 'NetworkInterfaces[0].Association.PublicIp' --output text --region "$AWS_REGION")"
if [ -z "$ip" ] || [ "$ip" = "None" ]; then
  summary failed ',"reason":"the task has no public IP; the subnets need a public route or AssignPublicIp"'
  exit 1
fi
url="http://$ip:$PORT"
ok=0
for attempt in $(seq 1 18); do
  if curl -sf --max-time 10 "$url/status" -o "var/$STAGE/status.json"; then ok=1; break; fi
  echo "waiting for $url/status (attempt $attempt)"
  sleep 10
done
printf '%s\n' "$url" > "var/$STAGE/app_url.txt"
if [ -n "${AWS_S3_BUCKET:-}" ]; then
  aws s3 cp "var/$STAGE/app_url.txt" "s3://$AWS_S3_BUCKET/orchestrator/$RUN_ID/state/app_url.txt" --region "$AWS_REGION" >/dev/null
fi
if [ "$ok" -ne 1 ]; then
  summary failed ",\"stack\":\"$STACK\",\"url\":\"$url\",\"reason\":\"the service is stable but /status did not answer\""
  exit 1
fi
echo "APP_PUBLIC_URL=$url"
summary passed ",\"stack\":\"$STACK\",\"cluster\":\"$CLUSTER\",\"service\":\"$SERVICE\",\"image\":\"$image\",\"url\":\"$url\""
