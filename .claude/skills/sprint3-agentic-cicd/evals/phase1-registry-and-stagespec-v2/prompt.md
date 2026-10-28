---
tags: [sprint3-agentic-cicd]
max_turns: 40
timeout_seconds: 1200
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

We're starting sprint 3 of the langgraph_support_app orchestrator. Do phase 1 only: add the stage registry (orchestrator/registry.py) with the stage families and the named stage templates, upgrade StageSpec / CandidatePipeline / PlanProposal in orchestrator/schemas.py so Astra can propose generated stages with a runner and an environment, add the new PipelineState fields, and make normalize_stages accept registry names and custom slugs. Keep every existing test passing and add tests/test_registry.py. Use the tcs_practice_env python for pytest and ruff. Don't touch the UI or the providers yet.
