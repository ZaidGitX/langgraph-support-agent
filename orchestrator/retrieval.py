"""Retrieval over the checkout (Sprint 3, phase 2).

A local BM25 index over line-window chunks of the checked-out files. It is deterministic
and offline by default, so tests need no key and no network. OpenAI embeddings are an
opt-in add-on (``ORCHESTRATOR_EMBEDDINGS=openai`` with ``OPENAI_API_KEY`` set) that never
raises: any failure falls back to BM25 and is noted once. Astra reads the excerpts this
module returns; it never writes a question, a plan, or a command.

The index is cached under ``<root>/var/retrieval/<tree_hash>.json`` and rebuilt only when a
file's path, size, or mtime changes. ``.env`` files, generated trees, binaries, lock files,
and files over 200 KB are never indexed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from app import config

from .runner import note
from .scan import SKIP_DIRS as _SCAN_SKIP_DIRS

SKIP_DIRS = set(_SCAN_SKIP_DIRS) | {"build", ".idea", ".vscode", ".claude", ".cursor"}
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".pdf", ".whl", ".tgz", ".gz", ".zip",
    ".sqlite", ".db", ".pyc", ".pyo", ".so", ".dylib", ".woff", ".woff2", ".ttf", ".lock",
    ".DS_Store",
}
SKIP_NAMES = {"package-lock.json", "yarn.lock", "poetry.lock", "Pipfile.lock", ".DS_Store"}
MAX_FILE_BYTES = 200_000
WINDOW = 60
OVERLAP = 10
K1 = 1.5
B = 0.75
EXCERPT_CHARS = 600
EMBED_MODEL = "text-embedding-3-small"
EMBED_BATCH = 64
EMBED_CHARS = 4000
STOP_WORDS = {
    "the", "and", "for", "with", "this", "that", "from", "import", "return", "self", "def",
    "class", "none", "true", "false", "not", "are", "was", "were", "into", "than", "then",
}

TOPIC_QUERIES: dict[str, str] = {
    "build": "build compile install dependencies requirements package.json vite npm pip ruff compileall",
    "test": "pytest tests fixtures test client httpx unit integration smoke",
    "deploy": "deploy environment target ecs cloud run container apps kubernetes uvicorn port 8000 healthcheck",
    "docker": "Dockerfile FROM COPY RUN CMD EXPOSE docker compose services volumes image",
    "env": "environment variables os.getenv .env config load_env settings",
    "workflows": "github actions workflow_dispatch jobs steps uses runs-on secrets permissions",
    "secrets": "token api key secret credential password placeholder",
    "scan": "semgrep trivy snyk gitleaks codeql sonar zap stackhawk scan vulnerability audit",
}
QUESTION_TOPICS = ("build", "test", "deploy", "docker", "env", "workflows")

_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass
class Index:
    root: str
    index_id: str
    tree_hash: str
    chunks: list[dict]
    files: int
    retriever: str
    cache_path: str
    vectors: list[list[float]] | None = None
    doc_tf: list[dict[str, int]] = field(default_factory=list)
    doc_len: list[int] = field(default_factory=list)
    df: dict[str, int] = field(default_factory=dict)
    avgdl: float = 0.0

    def summary(self) -> dict:
        return {
            "index_id": self.index_id,
            "tree_hash": self.tree_hash,
            "files": self.files,
            "chunks": len(self.chunks),
            "retriever": self.retriever,
            "cache_path": self.cache_path,
        }


_LOADED: dict[tuple[str, str], Index] = {}


def tokenize(text: str) -> list[str]:
    spaced = _CAMEL.sub(r"\1 \2", text or "").replace("_", " ")
    return [tok for tok in _TOKEN.findall(spaced.lower()) if len(tok) >= 2 and tok not in STOP_WORDS]


def _skip_path(root: Path, path: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in SKIP_DIRS for part in relative.parts[:-1]):
        return True
    name = path.name
    if name in SKIP_NAMES:
        return True
    if name.startswith(".env") and name != ".env.example":
        return True
    return path.suffix.lower() in SKIP_SUFFIXES


def iter_files(root: str) -> list[Path]:
    base = Path(root)
    if not base.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if _skip_path(base, path):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        found.append(path)
    return found


def tree_hash(root: str) -> str:
    digest = hashlib.sha256()
    base = Path(root)
    for path in iter_files(root):
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(f"{path.relative_to(base).as_posix()}\t{stat.st_size}\t{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def chunk_text(text: str) -> list[tuple[int, int, str]]:
    lines = text.splitlines()
    if not lines:
        return []
    chunks: list[tuple[int, int, str]] = []
    step = max(1, WINDOW - OVERLAP)
    start = 0
    while True:
        end = min(len(lines), start + WINDOW)
        body = "\n".join(lines[start:end]).strip()
        if body:
            chunks.append((start + 1, end, body))
        if end >= len(lines):
            break
        start += step
    return chunks


def _read_text(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:4096]:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def _build_chunks(root: str) -> tuple[list[dict], int]:
    base = Path(root)
    chunks: list[dict] = []
    files = 0
    for path in iter_files(root):
        text = _read_text(path)
        if text is None:
            continue
        relative = path.relative_to(base).as_posix()
        pieces = chunk_text(text)
        if not pieces:
            continue
        files += 1
        for start, end, body in pieces:
            chunks.append({"path": relative, "start": start, "end": end, "text": body})
    return chunks, files


def _prepare(index: Index) -> Index:
    doc_tf: list[dict[str, int]] = []
    doc_len: list[int] = []
    df: dict[str, int] = {}
    for chunk in index.chunks:
        counts: dict[str, int] = {}
        tokens = tokenize(chunk["path"] + "\n" + chunk["text"])
        for tok in tokens:
            counts[tok] = counts.get(tok, 0) + 1
        for tok in counts:
            df[tok] = df.get(tok, 0) + 1
        doc_tf.append(counts)
        doc_len.append(len(tokens))
    index.doc_tf = doc_tf
    index.doc_len = doc_len
    index.df = df
    index.avgdl = (sum(doc_len) / len(doc_len)) if doc_len else 0.0
    return index


def _cache_dir(root: str) -> Path:
    return Path(root) / "var" / "retrieval"


def _embeddings_enabled() -> bool:
    mode = os.getenv("ORCHESTRATOR_EMBEDDINGS", "").strip().lower()
    return mode == "openai" and bool(config.get_openai_api_key())


def _embed(texts: list[str]) -> list[list[float]] | None:
    """Embed chunk texts with OpenAI. Returns None (and notes why) on any failure."""
    try:
        from openai import OpenAI

        client = OpenAI(api_key=config.get_openai_api_key(), timeout=60)
        vectors: list[list[float]] = []
        for offset in range(0, len(texts), EMBED_BATCH):
            batch = [text[:EMBED_CHARS] for text in texts[offset : offset + EMBED_BATCH]]
            response = client.embeddings.create(model=EMBED_MODEL, input=batch)
            vectors.extend([[round(value, 6) for value in item.embedding] for item in response.data])
        if len(vectors) != len(texts):
            raise ValueError("embedding count mismatch")
        return vectors
    except Exception as exc:  # noqa: BLE001 - embeddings are optional; BM25 is the fallback
        note(f"retrieval: embeddings unavailable ({type(exc).__name__}); using BM25 only")
        return None


def _write_cache(index: Index) -> None:
    payload = {
        "index_id": index.index_id,
        "tree_hash": index.tree_hash,
        "root": index.root,
        "built_at": time.time(),
        "files": index.files,
        "retriever": index.retriever,
        "chunks": index.chunks,
        "vectors": index.vectors,
    }
    path = Path(index.cache_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def _read_cache(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("chunks"), list) else None


def load_index(root: str, *, force: bool = False) -> Index:
    root = str(Path(root))
    digest = tree_hash(root)
    key = (root, digest)
    if not force and key in _LOADED:
        index = _LOADED[key]
        if _embeddings_enabled() and index.vectors is None:
            index.vectors = _embed([chunk["text"] for chunk in index.chunks])
            if index.vectors is not None:
                index.retriever = "bm25+openai"
                _write_cache(index)
        return index
    cache_path = _cache_dir(root) / f"{digest}.json"
    cached = None if force else _read_cache(cache_path)
    if cached is not None:
        index = Index(
            root=root,
            index_id=str(cached.get("index_id") or digest[:12]),
            tree_hash=digest,
            chunks=list(cached["chunks"]),
            files=int(cached.get("files") or 0),
            retriever=str(cached.get("retriever") or "bm25"),
            cache_path=str(cache_path),
            vectors=cached.get("vectors") if isinstance(cached.get("vectors"), list) else None,
        )
    else:
        chunks, files = _build_chunks(root)
        index = Index(
            root=root,
            index_id=digest[:12],
            tree_hash=digest,
            chunks=chunks,
            files=files,
            retriever="bm25",
            cache_path=str(cache_path),
        )
        _write_cache(index)
    if _embeddings_enabled() and index.vectors is None and index.chunks:
        index.vectors = _embed([chunk["text"] for chunk in index.chunks])
        if index.vectors is not None:
            index.retriever = "bm25+openai"
            _write_cache(index)
    _prepare(index)
    _LOADED[key] = index
    return index


def build_index(root: str, *, force: bool = False) -> dict:
    """Build or load the index for a checkout and return its summary (no chunk text)."""
    return load_index(root, force=force).summary()


def _bm25(index: Index, query_tokens: list[str]) -> list[float]:
    total = len(index.chunks)
    if not total or not query_tokens:
        return [0.0] * total
    scores = [0.0] * total
    unique = []
    seen: set[str] = set()
    for tok in query_tokens:
        if tok not in seen:
            seen.add(tok)
            unique.append(tok)
    for tok in unique:
        df = index.df.get(tok, 0)
        if not df:
            continue
        idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
        for position, counts in enumerate(index.doc_tf):
            tf = counts.get(tok, 0)
            if not tf:
                continue
            length = index.doc_len[position]
            denominator = tf + K1 * (1 - B + B * (length / index.avgdl if index.avgdl else 1.0))
            scores[position] += idf * (tf * (K1 + 1)) / denominator
    return scores


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(a * a for a in left)) or 1.0
    right_norm = math.sqrt(sum(b * b for b in right)) or 1.0
    return dot / (left_norm * right_norm)


def _hybrid(index: Index, query: str, bm25: list[float]) -> list[float]:
    if not index.vectors:
        return bm25
    query_vector = _embed([query])
    if not query_vector:
        return bm25
    low, high = min(bm25), max(bm25)
    span = (high - low) or 1.0
    scores = []
    for position, score in enumerate(bm25):
        lexical = (score - low) / span
        semantic = _cosine(query_vector[0], index.vectors[position]) if position < len(index.vectors) else 0.0
        scores.append(0.5 * lexical + 0.5 * max(0.0, semantic))
    return scores


def _excerpt(chunk: dict, score: float, topic: str) -> dict:
    text = chunk["text"]
    if len(text) > EXCERPT_CHARS:
        text = text[:EXCERPT_CHARS].rstrip() + " …"
    return {
        "path": chunk["path"],
        "lines": f"{chunk['start']}-{chunk['end']}",
        "score": round(float(score), 4),
        "text": text,
        "topic": topic,
    }


def retrieve(root: str, query: str, k: int = 6, *, topic: str = "") -> list[dict]:
    """Top-k excerpts for a query, sorted by score then path and line (stable)."""
    index = load_index(root)
    if not index.chunks:
        return []
    tokens = tokenize(query)
    scores = _bm25(index, tokens)
    if index.vectors:
        scores = _hybrid(index, query, scores)
    ranked = sorted(
        (
            (score, chunk["path"], chunk["start"], position)
            for position, (score, chunk) in enumerate(zip(scores, index.chunks, strict=False))
            if score > 0
        ),
        key=lambda item: (-item[0], item[1], item[2]),
    )
    return [_excerpt(index.chunks[position], score, topic) for score, _path, _start, position in ranked[: max(0, k)]]


def context_for(root: str, topics: list[str] | tuple[str, ...], per_topic: int = 4) -> dict[str, list[dict]]:
    """Excerpts per topic from TOPIC_QUERIES (an unknown topic is used as its own query)."""
    context: dict[str, list[dict]] = {}
    for topic in topics:
        query = TOPIC_QUERIES.get(topic, topic)
        hits = retrieve(root, query, k=per_topic, topic=topic)
        if hits:
            context[topic] = hits
    return context


def evidence(context: dict[str, list[dict]] | None) -> list[dict]:
    """Unique (path, lines, topic) rows for the UI and the report, sorted by path then lines."""
    rows: dict[tuple[str, str], dict] = {}
    for topic, hits in (context or {}).items():
        for hit in hits:
            key = (hit["path"], hit["lines"])
            if key not in rows:
                rows[key] = {"path": hit["path"], "lines": hit["lines"], "topic": topic}
    return sorted(rows.values(), key=lambda row: (row["path"], int(row["lines"].split("-")[0])))


def trim_context(context: dict[str, list[dict]] | None, max_chars: int = 9000, per_topic: int = 3, text_chars: int = 500) -> dict[str, list[dict]]:
    """Bound the excerpts that go into a prompt: fewer per topic, shorter text, size-capped."""
    trimmed: dict[str, list[dict]] = {}
    for topic, hits in (context or {}).items():
        trimmed[topic] = [
            {**hit, "text": (hit["text"][:text_chars].rstrip() + " …") if len(hit["text"]) > text_chars else hit["text"]}
            for hit in hits[:per_topic]
        ]
    while trimmed and len(json.dumps(trimmed)) > max_chars:
        longest = max(trimmed, key=lambda name: len(trimmed[name]))
        if len(trimmed[longest]) > 1:
            trimmed[longest] = trimmed[longest][:-1]
        else:
            del trimmed[longest]
    return trimmed
