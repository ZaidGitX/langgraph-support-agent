---
tags: [sprint3-agentic-cicd]
max_turns: 40
timeout_seconds: 1200
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

Right now Astra only gets a shallow file scan before it writes the ecosystem questions and proposes the DAGs. Make Astra actually read the checked-out code: add a local retrieval module over the repo (chunk the files, rank with BM25, cache the index under var/, keep .env out of it), feed the top excerpts per topic into the discover_ecosystem and plan_pipeline prompts, and surface what was retrieved in the report. It has to work offline in the tests with no OpenAI key — embeddings can be an optional add-on. Add tests for it and keep the existing suite green.
