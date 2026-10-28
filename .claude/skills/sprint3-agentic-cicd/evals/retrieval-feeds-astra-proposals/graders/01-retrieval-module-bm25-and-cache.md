---
type: llm
focus: trace
weight: 1
---

orchestrator/retrieval.py implements chunking plus a BM25 ranker in pure Python, caches the index under var/retrieval keyed by a hash of the file tree, and excludes .env, var, dist, node_modules, and .git from the index
