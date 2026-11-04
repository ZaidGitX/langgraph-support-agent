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
    "computeType": "BUILD_GENERAL1_SMALL",
    "privilegedMode": true,
    "environmentVariables": [
      {"name": "APP_NAME", "value": "$APP_NAME", "type": "PLAINTEXT"},
      {"name": "TIER", "value": "$TIER", "type": "PLAINTEXT"},
      {"name": "AWS_REGION", "value": "$REGION", "type": "PLAINTEXT"}
    ]
  },
  "serviceRole": "$CODEBUILD_ROLE_ARN",
  "timeoutInMinutes": 20,
  "logsConfig": {"cloudWatchLogs": {"status": "ENABLED"}}
}
