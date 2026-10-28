---
type: llm
focus: trace
weight: 1
---

The payload passed to Astra's write_questions and propose methods in orchestrator/graph.py includes retrieved code excerpts (per topic, each with a path and text), and the rendered report contains a Retrieval section listing the files that were read
