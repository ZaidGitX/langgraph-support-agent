---
tags: [sprint3-agentic-cicd]
max_turns: 40
timeout_seconds: 1200
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

I want Astra's deploy_dev stage to be able to run on AWS CodeBuild. Add the aws_codebuild provider under orchestrator/providers/ following the provider protocol (detect / render_skeleton / execute with plan and apply modes). I have the aws CLI installed but no credentials configured, so the provider must come back as "planned" with the missing variable names and the exact `aws codebuild start-build` command it would run, and tests must never hit the network. Wire it into the providers registry and add tests/test_providers.py coverage for it. Don't create any AWS resources.
