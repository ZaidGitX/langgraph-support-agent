---
type: llm
focus: trace
weight: 1
---

validate_stage_name rejects "production" and "prod", CandidatePipeline validation requires uat to be the last stage, and no file introduces a production target or an edge after uat
