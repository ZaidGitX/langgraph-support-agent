---
type: llm
focus: trace
weight: 1
---

Executing the provider without credentials returns status "planned" whose plan contains the missing variable names (such as AWS_ACCESS_KEY_ID, AWS_REGION, AWS_CODEBUILD_PROJECT) and a would_run argv beginning with aws codebuild start-build
