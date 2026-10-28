from __future__ import annotations

import json
import os
from pathlib import Path

from orchestrator import retrieval

FAKE_SECRET = "sk-" + "notarealkey" + "0123456789abcdef"


def _repo(tmp_path: Path) -> str:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text(
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n@app.get('/status')\ndef status():\n    return {'ok': True}\n",
        encoding="utf-8",
    )
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.12-slim\nEXPOSE 8000\n"
        "HEALTHCHECK CMD python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/status')\"\n"
        'CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]\n',
        encoding="utf-8",
    )
    # Built at runtime so the test file itself never holds a credential-shaped literal.
    (tmp_path / ".env").write_text("OPENAI_API_KEY=" + FAKE_SECRET + "\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# demo\n\nRun uvicorn and open the status page.\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.js").write_text("uvicorn healthcheck port 8000 " * 20, encoding="utf-8")
    return str(tmp_path)


def test_index_excludes_env_generated_trees_and_caches(tmp_path):
    root = _repo(tmp_path)
    summary = retrieval.build_index(root)
    assert summary["files"] == 3
    assert summary["chunks"] >= 3
    assert summary["retriever"] == "bm25"
    cache = Path(summary["cache_path"])
    assert cache.is_file() and cache.parent == tmp_path / "var" / "retrieval"
    paths = {chunk["path"] for chunk in retrieval.load_index(root).chunks}
    assert ".env" not in paths
    assert not any(path.startswith("node_modules") for path in paths)
    assert FAKE_SECRET not in cache.read_text(encoding="utf-8")


def test_retrieve_ranks_the_relevant_file_first_and_never_env(tmp_path):
    root = _repo(tmp_path)
    hits = retrieval.retrieve(root, "healthcheck uvicorn port 8000")
    assert hits
    assert hits[0]["path"] in {"Dockerfile", "app/main.py"}
    assert all(hit["path"] != ".env" for hit in hits)
    for hit in hits:
        assert set(hit) == {"path", "lines", "score", "text", "topic"}
        start, end = hit["lines"].split("-")
        assert int(start) >= 1 and int(end) >= int(start)
        assert len(hit["text"]) <= retrieval.EXCERPT_CHARS + 2
    assert retrieval.retrieve(root, "zzzz_no_such_token") == []


def test_index_is_cached_and_follows_the_tree(tmp_path):
    root = _repo(tmp_path)
    first = retrieval.build_index(root)
    second = retrieval.build_index(root)
    assert first["index_id"] == second["index_id"]
    assert len(list((tmp_path / "var" / "retrieval").glob("*.json"))) == 1
    readme = tmp_path / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\nA new line changes the tree hash.\n", encoding="utf-8")
    os.utime(readme, None)
    third = retrieval.build_index(root)
    assert third["index_id"] != first["index_id"]
    assert len(list((tmp_path / "var" / "retrieval").glob("*.json"))) == 2


def test_context_for_evidence_and_trim(tmp_path):
    root = _repo(tmp_path)
    context = retrieval.context_for(root, ["docker", "deploy", "no_such_topic_zzz"])
    assert set(context) <= {"docker", "deploy"}
    for hits in context.values():
        assert 1 <= len(hits) <= 4
        assert all({"path", "lines", "text", "topic"} <= set(hit) for hit in hits)
    rows = retrieval.evidence(context)
    assert rows == sorted(rows, key=lambda row: (row["path"], int(row["lines"].split("-")[0])))
    assert len({(row["path"], row["lines"]) for row in rows}) == len(rows)

    fat = {f"topic{i}": [{"path": "p", "lines": "1-2", "score": 1.0, "text": "x" * 2000, "topic": f"topic{i}"} for _ in range(6)] for i in range(6)}
    trimmed = retrieval.trim_context(fat, max_chars=4000)
    assert len(json.dumps(trimmed)) <= 4000
    assert all(len(hits) <= 3 for hits in trimmed.values())
    assert all(len(hit["text"]) <= 502 for hits in trimmed.values() for hit in hits)


def test_tokenize_splits_identifiers_and_drops_noise():
    tokens = retrieval.tokenize("loadEnvFiles OPENAI_API_KEY the self import")
    assert {"load", "env", "files", "openai", "api", "key"} <= set(tokens)
    assert "the" not in tokens and "self" not in tokens and "import" not in tokens


def test_empty_root_is_harmless(tmp_path):
    summary = retrieval.build_index(str(tmp_path))
    assert summary["files"] == 0 and summary["chunks"] == 0
    assert retrieval.retrieve(str(tmp_path), "anything") == []
    assert retrieval.context_for(str(tmp_path), ["build"]) == {}


def test_embeddings_are_opt_in_and_need_a_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ORCHESTRATOR_EMBEDDINGS", raising=False)
    assert retrieval._embeddings_enabled() is False
    monkeypatch.setenv("ORCHESTRATOR_EMBEDDINGS", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert retrieval._embeddings_enabled() is False
    summary = retrieval.build_index(_repo(tmp_path))
    assert summary["retriever"] == "bm25"
