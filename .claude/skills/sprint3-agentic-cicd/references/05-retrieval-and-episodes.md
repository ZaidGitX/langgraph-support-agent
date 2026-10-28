# 05 — Retrieval (RAG over the checkout) and episodic memory (Phase 2)

Sprint 2 called the file scan "the retrieval stand-in". Sprint 3 gives Astra real retrieval
over the checked-out code plus a memory of previous runs. Both are local, deterministic by
default, and need no network, so tests stay offline.

## `orchestrator/retrieval.py`

### Index
- Walk the checkout with the same `SKIP_DIRS` as `scan.py` plus `frontend/dist`, `var`,
  `dist`, `.git`. Skip binaries (by suffix: png, jpg, whl, tgz, sqlite, pyc, lock files over
  200 KB) and any file whose name starts with `.env` except `.env.example`. Skip files over
  200 KB.
- Chunk each text file into windows of 60 lines with 10 lines of overlap. Record
  `{path, start, end, text}`.
- Tokenize: lower-case, split on non-alphanumerics, split `snake_case` and `camelCase`,
  drop tokens shorter than 2 characters, keep a small stop list (`the, and, for, with, this,
  that, from, import, return, self, def, class`).
- BM25 (k1 = 1.5, b = 0.75) in pure Python over the chunk tokens. No third-party package.
- Cache: `var/retrieval/<tree_hash>.json` where `tree_hash = sha256` over the sorted list of
  `(relative_path, size, mtime_ns)`. `index_id` is the first 12 characters. Rebuild only when
  the hash changes.
- Optional embeddings: when `ORCHESTRATOR_EMBEDDINGS=openai` and `OPENAI_API_KEY` is set,
  embed chunks with `text-embedding-3-small` (batches of 64, `openai` client already in the
  env, `tiktoken` to cap chunks at 800 tokens) and store vectors in the same cache. Score =
  `0.5 * minmax(bm25) + 0.5 * cosine`. Any embedding failure logs one line and falls back to
  BM25 only; it never raises. `retriever` in the state says which path ran.

### API
```python
def build_index(root: str) -> dict            # {"index_id", "files", "chunks", "retriever", "cache_path"}
def retrieve(root: str, query: str, k: int = 6, *, topic: str = "") -> list[dict]
    # [{path, lines: "start-end", score, text (≤ 600 chars), topic}], sorted by (score desc, path, start)
def context_for(root: str, topics: list[str], per_topic: int = 4) -> dict[str, list[dict]]
def evidence(context: dict) -> list[dict]     # unique [{path, lines, topic}] for the UI and the report

TOPIC_QUERIES = {
    "build": "build compile install dependencies requirements package.json vite npm pip ruff compileall",
    "test": "pytest tests fixtures test client httpx unit integration smoke",
    "deploy": "deploy environment target ecs cloud run container apps kubernetes uvicorn port 8000 healthcheck",
    "docker": "Dockerfile FROM COPY RUN CMD EXPOSE docker compose services volumes image",
    "env": "environment variables os.getenv .env config load_env settings",
    "workflows": "github actions workflow_dispatch jobs steps uses runs-on secrets permissions",
    "secrets": "token api key secret credential password placeholder",
    "scan": "semgrep trivy snyk gitleaks codeql sonar zap stackhawk scan vulnerability audit",
}
```
Excerpts are passed to Astra as `retrieved: {topic: [excerpt, ...]}`. Keep the total under
~6 KB per prompt (truncate `text` to 600 chars, 4 excerpts per topic, at most 6 topics per
call). `discover_ecosystem` uses all topics; `generate_artifacts` uses the topics relevant to
the stage family (`build, docker` for images; `test` for tests; `deploy, env, workflows` for
deploys; `scan, docker` for scans).

### Tests (`tests/test_retrieval.py`)
- Build a tmp repo with `app/main.py` (FastAPI `/status`), `Dockerfile`, `.env` (must be
  excluded), `README.md`. `retrieve(root, "healthcheck uvicorn port")` returns `Dockerfile`
  or `app/main.py` first, never `.env`.
- `build_index` twice → same `index_id`, one cache file.
- `context_for(root, ["docker"])` returns ≤ 4 excerpts each with `path`, `lines`, `text`.
- Changing a file changes `index_id`.

## `orchestrator/episodes.py`

One SQLite table in the same database file the checkpointer uses (`ORCHESTRATOR_DB`, default
`var/orchestrator.sqlite`), opened with the stdlib `sqlite3` module on its own connection.

```sql
CREATE TABLE IF NOT EXISTS episodes (
  run_id TEXT PRIMARY KEY, repo_root TEXT, branch TEXT, sha TEXT,
  started_at REAL, finished_at REAL, status TEXT, platform TEXT, tier TEXT,
  chosen_id TEXT, mode TEXT, stages_json TEXT, failures_json TEXT, planned_json TEXT,
  jev_json TEXT, artifacts_json TEXT, policy_errors INTEGER, lesson TEXT)
```

```python
def record(state: dict, status: str, db_path: str | None = None) -> dict   # returns the stored row as a dict
def recall(repo_root: str, limit: int = 3, db_path: str | None = None) -> list[dict]
    # newest first: [{run_id, finished_at, status, platform, tier, chosen: {id, mode, stages},
    #                 failures: [{stage, tool, finding}], planned: [stage], lesson}]
def lesson_for(state: dict, status: str) -> str
    # one sentence composed in code, e.g. "container_scan was planned only: AWS_ACCESS_KEY_ID missing"
    # or "test failed on attempt 1 and passed on attempt 2" or "all stages passed on local"
```
`branch` and `sha` come from `GITHUB_REF_NAME`/`GITHUB_SHA` or `git rev-parse --abbrev-ref
HEAD` / `git rev-parse HEAD` (subprocess, 5 s timeout, empty string on failure).
`failures_json` keeps at most 200 characters of a finding per stage, already redacted.

Where episodes are used:
- `discover_ecosystem` → `ecosystem["episodes"]` → Astra questions (do not re-ask what a
  prior run settled when the same branch answered it; the person still sees the questions).
- `plan_pipeline` → Astra proposals ("prior episodes tell you what ran and failed").
- `report` → `episodes.record(...)`; `machine_report["episodes"]`.
- UI Episodes panel; `GET /api/orchestrator/episodes`.

### Tests (`tests/test_episodes.py`)
- `record` then `recall` returns the row with `chosen.stages`, `lesson` non-empty.
- `recall(limit=1)` after two records returns the newest.
- A different `repo_root` is not recalled.
- `record` is idempotent for the same `run_id` (upsert).

## Prompt budget

Retrieval excerpts + episodes add roughly 2–4 K tokens to the question and plan prompts.
That is intended; the Responses API with structured output handles it. Keep `timeout=120`.
