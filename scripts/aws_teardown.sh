#!/usr/bin/env bash
# Remove what a platform run created in AWS (Sprint 4, phase 12). PRINTS by default; runs with
# --apply. --all also removes what scripts/aws_bootstrap.sh created (role, repository, bucket).
#
#   scripts/aws_teardown.sh                 # plan: the CodeBuild project, its logs, the uploads
#   scripts/aws_teardown.sh --apply         # remove them
#   scripts/aws_teardown.sh --apply --all   # also the role, the ECR repository, and the bucket
set -euo pipefail

APPLY=0
ALL=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --all) ALL=1 ;;
    *) echo "unknown argument $arg" >&2; exit 2 ;;
  esac
done
: "${AWS_REGION:?set AWS_REGION}"
: "${AWS_S3_BUCKET:?set AWS_S3_BUCKET}"
: "${AWS_ECR_REPOSITORY:?set AWS_ECR_REPOSITORY}"
: "${AWS_CODEBUILD_PROJECT:?set AWS_CODEBUILD_PROJECT}"
ROLE_NAME="${AWS_CODEBUILD_ROLE_NAME:-${AWS_CODEBUILD_PROJECT}-codebuild-role}"

run() {
  if [ "$APPLY" = "1" ]; then
    echo "+ $*"
    "$@" || echo "  (continuing: the command failed, the resource may already be gone)"
  else
    echo "plan: $*"
  fi
}

APP_NAME="langgraph-support-app"
TIER="${TIER:-dev}"

# What a run creates: the project, its log group, the uploaded source zips and cache, and the
# generated ECS stack when a deploy stage created it (the stack removes its own log group).
run aws codebuild delete-project --name "$AWS_CODEBUILD_PROJECT" --region "$AWS_REGION"
if aws cloudformation describe-stacks --stack-name "${APP_NAME}-${TIER}" --region "$AWS_REGION" >/dev/null 2>&1; then
  run aws cloudformation delete-stack --stack-name "${APP_NAME}-${TIER}" --region "$AWS_REGION"
fi
run aws logs delete-log-group --log-group-name "/aws/codebuild/$AWS_CODEBUILD_PROJECT" --region "$AWS_REGION"
for group_arn in $(aws codebuild list-report-groups --region "$AWS_REGION" --query "reportGroups[?contains(@, ':report-group/${AWS_CODEBUILD_PROJECT}-')]" --output text 2>/dev/null); do
  run aws codebuild delete-report-group --arn "$group_arn" --delete-reports --region "$AWS_REGION"
done
run aws s3 rm "s3://$AWS_S3_BUCKET/orchestrator/" --recursive --region "$AWS_REGION"
# The ECS stack removes its own cluster, service, roles, and log group; the run's images and
# the app's secrets stay unless --all.

if [ "$ALL" = "1" ]; then
  for secret_arn in $(aws secretsmanager list-secrets --region "$AWS_REGION" --filters "Key=name,Values=${APP_NAME}/" --query 'SecretList[].ARN' --output text 2>/dev/null); do
    run aws secretsmanager delete-secret --secret-id "$secret_arn" --force-delete-without-recovery --region "$AWS_REGION"
  done
  run aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name "${ROLE_NAME}-policy"
  run aws iam delete-role --role-name "$ROLE_NAME"
  run aws ecr delete-repository --repository-name "$AWS_ECR_REPOSITORY" --region "$AWS_REGION" --force
  run aws s3 rb "s3://$AWS_S3_BUCKET" --force --region "$AWS_REGION"
fi

if [ "$APPLY" = "0" ]; then
  echo
  echo "Nothing was removed. Re-run with --apply (and --all for the bootstrap resources)."
fi
