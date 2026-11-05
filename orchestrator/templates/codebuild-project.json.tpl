{
  "name": "$CODEBUILD_PROJECT",
  "description": "Orchestrator project for $APP_NAME, run $RUN_ID. Source: a zip uploaded to S3 (any git host). privilegedMode stays true for image stages. No path past UAT.",
  "source": {
    "type": "S3",
    "location": "$S3_BUCKET/orchestrator/$RUN_ID/source.zip",
    "buildspec": "ci/buildspec-build.yml"
  },
  "artifacts": {"type": "NO_ARTIFACTS"},
  "cache": {"type": "S3", "location": "$S3_BUCKET/orchestrator/cache"},
  "environment": {
    "type": "LINUX_CONTAINER",
    "image": "aws/codebuild/standard:7.0",
    "computeType": "BUILD_GENERAL1_MEDIUM",
    "privilegedMode": true,
    "environmentVariables": [
      {"name": "APP_NAME", "value": "$APP_NAME", "type": "PLAINTEXT"},
      {"name": "TIER", "value": "$TIER", "type": "PLAINTEXT"},
      {"name": "AWS_REGION", "value": "$REGION", "type": "PLAINTEXT"},
      {"name": "AWS_ACCOUNT_ID", "value": "$AWS_ACCOUNT_ID", "type": "PLAINTEXT"},
      {"name": "AWS_S3_BUCKET", "value": "$AWS_S3_BUCKET", "type": "PLAINTEXT"},
      {"name": "AWS_ECR_REPOSITORY", "value": "$AWS_ECR_REPOSITORY", "type": "PLAINTEXT"},
      {"name": "AWS_CFN_STACK", "value": "$AWS_CFN_STACK", "type": "PLAINTEXT"},
      {"name": "AWS_ECS_CLUSTER", "value": "$AWS_ECS_CLUSTER", "type": "PLAINTEXT"},
      {"name": "AWS_ECS_SERVICE", "value": "$AWS_ECS_SERVICE", "type": "PLAINTEXT"},
      {"name": "AWS_VPC_ID", "value": "$AWS_VPC_ID", "type": "PLAINTEXT"},
      {"name": "AWS_SUBNET_IDS", "value": "$AWS_SUBNET_IDS", "type": "PLAINTEXT"}
    ]
  },
  "serviceRole": "$CODEBUILD_ROLE_ARN",
  "timeoutInMinutes": 30,
  "logsConfig": {"cloudWatchLogs": {"status": "ENABLED"}}
}
