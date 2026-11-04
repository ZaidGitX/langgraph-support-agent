#!/usr/bin/env bash
# Manual fallback for the AWS platform bootstrap (Sprint 4, phase 12). Since 2026-11-04 the
# orchestrator creates the same things itself on the first apply, with the vault credentials
# (orchestrator/providers/aws.py, AwsProvider.ensure_platform): the source bucket, the ECR
# repository, and the scoped CodeBuild service role whose policy this script prints from the
# same Python function. Use it only to pre-create or inspect them from a terminal. It PRINTS
# every command by default and runs them only with --apply.
#
#   scripts/aws_bootstrap.sh                        # plan: show what would be created
#   scripts/aws_bootstrap.sh --apply                # create the bucket, the ECR repository, and the role
#   scripts/aws_bootstrap.sh --apply --with-deploy  # accepted for compatibility: the deploy
#                                                   # statements are always part of the policy
#
# The role policy covers the project's logs, the bucket, the repository, the app's secrets,
# and the scoped CloudFormation, ECS, EC2, log-group, and task-role calls a Fargate deploy of
# the generated stack needs (names prefixed with the app name).
#
# Names come from the environment (the same names the dashboard's Connections panel and
# .env.example use): AWS_REGION, AWS_S3_BUCKET, AWS_ECR_REPOSITORY, AWS_CODEBUILD_PROJECT.
# It prints the role ARN to put into AWS_CODEBUILD_ROLE_ARN. Nothing here names production.
set -euo pipefail

APPLY=0
WITH_DEPLOY=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --with-deploy) WITH_DEPLOY=1 ;;
    *) echo "unknown argument $arg" >&2; exit 2 ;;
  esac
done
: "${AWS_REGION:?set AWS_REGION}"
: "${AWS_S3_BUCKET:?set AWS_S3_BUCKET}"
: "${AWS_ECR_REPOSITORY:?set AWS_ECR_REPOSITORY}"
: "${AWS_CODEBUILD_PROJECT:?set AWS_CODEBUILD_PROJECT}"
ROLE_NAME="${AWS_CODEBUILD_ROLE_NAME:-${AWS_CODEBUILD_PROJECT}-codebuild-role}"
APP_NAME="langgraph-support-app"
PYTHON="${PYTHON:-/opt/anaconda3/envs/tcs_practice_env/bin/python}"

run() {
  if [ "$APPLY" = "1" ]; then
    echo "+ $*"
    "$@"
  else
    echo "plan: $*"
  fi
}

echo "account: $(aws sts get-caller-identity --query Account --output text --region "$AWS_REGION")"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text --region "$AWS_REGION")"

# 1. The source bucket (the orchestrator uploads var/generated/<run>/pipeline/source.zip here).
if aws s3api head-bucket --bucket "$AWS_S3_BUCKET" --region "$AWS_REGION" 2>/dev/null; then
  echo "bucket $AWS_S3_BUCKET exists"
elif [ "$AWS_REGION" = "us-east-1" ]; then
  run aws s3api create-bucket --bucket "$AWS_S3_BUCKET" --region "$AWS_REGION"
else
  run aws s3api create-bucket --bucket "$AWS_S3_BUCKET" --region "$AWS_REGION" --create-bucket-configuration "LocationConstraint=$AWS_REGION"
fi
run aws s3api put-public-access-block --bucket "$AWS_S3_BUCKET" --region "$AWS_REGION" \
  --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# 2. The image repository the container stage pushes to.
if aws ecr describe-repositories --repository-names "$AWS_ECR_REPOSITORY" --region "$AWS_REGION" >/dev/null 2>&1; then
  echo "ecr repository $AWS_ECR_REPOSITORY exists"
else
  run aws ecr create-repository --repository-name "$AWS_ECR_REPOSITORY" --region "$AWS_REGION" --image-scanning-configuration scanOnPush=true
fi

# 3. The CodeBuild service role: logs, the bucket, the repository, and app/* secrets. Nothing else.
TRUST="$(mktemp)"
POLICY="$(mktemp)"
trap 'rm -f "$TRUST" "$POLICY"' EXIT
cat > "$TRUST" <<JSON
{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "codebuild.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
JSON
"$PYTHON" -c "import json, sys; sys.path.insert(0, '.'); from orchestrator.providers.aws import role_policy; print(json.dumps(role_policy('$ACCOUNT_ID', '$AWS_REGION', bucket='$AWS_S3_BUCKET', repository='$AWS_ECR_REPOSITORY', project='$AWS_CODEBUILD_PROJECT'), indent=2))" > "$POLICY"
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "role $ROLE_NAME exists"
else
  run aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "file://$TRUST" \
    --description "CodeBuild service role for the orchestrator ($APP_NAME)"
fi
run aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name "${ROLE_NAME}-policy" --policy-document "file://$POLICY"

echo
echo "Set these names (Connections panel or .env), never a value in a file you commit:"
echo "  AWS_REGION=$AWS_REGION"
echo "  AWS_ACCOUNT_ID=$ACCOUNT_ID"
echo "  AWS_S3_BUCKET=$AWS_S3_BUCKET"
echo "  AWS_ECR_REPOSITORY=$AWS_ECR_REPOSITORY"
echo "  AWS_CODEBUILD_PROJECT=$AWS_CODEBUILD_PROJECT"
echo "  AWS_CODEBUILD_ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
if [ "$APPLY" = "0" ]; then
  echo
  echo "Nothing was created. Re-run with --apply to create what is listed above."
fi
