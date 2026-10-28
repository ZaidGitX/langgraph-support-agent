"""Episodic memory of previous orchestrator runs (Sprint 3, phase 2).

One SQLite table, ``episodes``, in the same database file the checkpointer uses
(``ORCHESTRATOR_DB``, else ``<repo>/var/orchestrator.sqlite``), opened on its own stdlib
connection. ``report`` records the finished run; ``discover_ecosystem`` recalls the newest
runs for the same checkout so Astra can avoid re-asking what a prior run settled and prefer
routes that fixed earlier failures. Nothing here reads a credential value, and every stored
finding is redacted and short.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from app import config

from .runner import redact
from .state import latest_results

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS episodes (
  run_id TEXT PRIMARY KEY,
  repo_root TEXT,
  branch TEXT,
  sha TEXT,
  started_at REAL,
  finished_at REAL,
  status TEXT,
  platform TEXT,
  tier TEXT,
  chosen_id TEXT,
  mode TEXT,
  stages_json TEXT,
  failures_json TEXT,
  planned_json TEXT,
  jev_json TEXT,
  artifacts_json TEXT,
  policy_errors INTEGER,
  lesson TEXT
)
"""
FINDING_CHARS = 200


def default_db_path(repo_root: str | None = None) -> str:
    configured = os.getenv("ORCHESTRATOR_DB", "").strip()
    if configured:
        return configured
    base = Path(repo_root) if repo_root else config.PROJECT_DIR
    return str(base / "var" / "orchestrator.sqlite")


def _connect(db_path: str) -> sqlite3.Connection:
    parent = Path(db_path).parent
    parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=5)
    connection.execute(TABLE_SQL)
    return connection


def _git(root: str, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _branch_and_sha(root: str) -> tuple[str, str]:
    branch = os.getenv("GITHUB_REF_NAME", "").strip() or _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    sha = os.getenv("GITHUB_SHA", "").strip() or _git(root, "rev-parse", "HEAD")
    return branch, sha


def _iso(stamp: float | None) -> str:
    if not stamp:
        return ""
    return datetime.fromtimestamp(float(stamp), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _failures(state: dict) -> list[dict]:
    rows: list[dict] = []
    for name, row in latest_results(state).items():
        if row.get("status") != "fail":
            continue
        checks = [check for check in row.get("checks") or [] if check.get("status") == "fail"]
        if checks:
            for check in checks:
                rows.append(
                    {
                        "stage": name,
                        "tool": str(check.get("tool") or check.get("name") or ""),
                        "finding": redact(str(check.get("findings") or ""), limit=FINDING_CHARS),
                    }
                )
        else:
            rows.append(
                {
                    "stage": name,
                    "tool": "",
                    "finding": redact(str(row.get("evidence") or ""), limit=FINDING_CHARS),
                }
            )
    return rows


def _planned(state: dict) -> list[dict]:
    rows: list[dict] = []
    for name, row in latest_results(state).items():
        if row.get("status") != "planned":
            continue
        plan = row.get("plan") or {}
        rows.append({"stage": name, "missing": list(plan.get("missing") or []), "runner": str(row.get("runner") or plan.get("runner") or "")})
    return rows


def _retried(state: dict) -> list[str]:
    attempts = state.get("attempt") or {}
    latest = latest_results(state)
    return sorted(
        name
        for name, count in attempts.items()
        if name != "plan" and int(count or 0) > 1 and latest.get(name, {}).get("status") == "pass"
    )


def lesson_for(state: dict, status: str) -> str:
    """One sentence composed in code; never a model opinion."""
    planned = _planned(state)
    failures = _failures(state)
    retried = _retried(state)
    env_state = state.get("env_state") or {}
    where = f"{env_state.get('platform') or 'local'}/{env_state.get('target') or 'local'}"
    parts: list[str] = []
    if planned:
        parts.append(
            "; ".join(
                f"{row['stage']} was planned only" + (f": missing {', '.join(row['missing'])}" if row["missing"] else "")
                for row in planned
            )
        )
    if failures:
        names = sorted({row["stage"] for row in failures})
        parts.append(f"{', '.join(names)} failed")
    if retried:
        parts.append(f"{', '.join(retried)} passed after a retry")
    if not parts:
        stages = (state.get("pipeline") or {}).get("stages") or []
        parts.append(f"all {len(stages)} stages passed on {where}")
    return f"{status}: " + "; ".join(parts)


def row_for(state: dict, status: str) -> dict:
    root = str((state.get("repo") or {}).get("root") or "")
    branch, sha = _branch_and_sha(root)
    pipeline = state.get("pipeline") or {}
    env_state = state.get("env_state") or {}
    answers = state.get("answers") or {}
    latest = latest_results(state)
    stages = [
        {
            "name": stage.get("name"),
            "depends_on": list(stage.get("depends_on") or []),
            "runner": stage.get("runner") or "local",
            "kind": stage.get("kind") or "fixed",
            "status": latest.get(stage.get("name"), {}).get("status") or "pending",
        }
        for stage in pipeline.get("stages") or []
    ]
    jev = [
        {"kind": item.get("kind"), "model": item.get("model"), "choice": item.get("choice")}
        for item in state.get("jev_decisions") or []
    ]
    artifacts = [
        {"stage": stage, "path": record.get("path"), "kind": record.get("kind"), "sha256": record.get("sha256")}
        for stage, records in (state.get("generated_artifacts") or {}).items()
        for record in records or []
        if isinstance(record, dict)
    ]
    policy_errors = sum(1 for item in state.get("policy") or [] if item.get("severity") == "error")
    return {
        "run_id": str(state.get("run_id")),
        "repo_root": root,
        "branch": branch,
        "sha": sha,
        "started_at": float(state.get("started_at") or time.time()),
        "finished_at": time.time(),
        "status": status,
        "platform": str(env_state.get("platform") or "local"),
        "tier": str(env_state.get("target") if env_state.get("target") not in (None, "unset") else answers.get("env_target") or "local"),
        "chosen_id": str(pipeline.get("id") or ""),
        "mode": str(pipeline.get("mode") or "deterministic"),
        "stages_json": json.dumps(stages),
        "failures_json": json.dumps(_failures(state)),
        "planned_json": json.dumps(_planned(state)),
        "jev_json": json.dumps(jev, default=str),
        "artifacts_json": json.dumps(artifacts),
        "policy_errors": int(policy_errors),
        "lesson": lesson_for(state, status),
    }


def record(state: dict, status: str, db_path: str | None = None) -> dict:
    """Upsert this run as an episode. Returns the stored row; never raises."""
    row = row_for(state, status)
    path = db_path or default_db_path(row["repo_root"] or None)
    try:
        with _connect(path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO episodes VALUES (" + ",".join("?" * len(row)) + ")",
                tuple(row.values()),
            )
    except sqlite3.Error as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
    return row


def _summary(row: sqlite3.Row) -> dict:
    def loads(name: str, default):
        try:
            value = json.loads(row[name] or "")
        except (TypeError, json.JSONDecodeError):
            return default
        return value if isinstance(value, type(default)) else default

    stages = loads("stages_json", [])
    return {
        "run_id": row["run_id"],
        "branch": row["branch"] or "",
        "sha": (row["sha"] or "")[:12],
        "finished_at": _iso(row["finished_at"]),
        "status": row["status"] or "",
        "platform": row["platform"] or "",
        "tier": row["tier"] or "",
        "chosen": {
            "id": row["chosen_id"] or "",
            "mode": row["mode"] or "",
            "stages": [stage.get("name") for stage in stages if isinstance(stage, dict)],
        },
        "stage_statuses": {stage.get("name"): stage.get("status") for stage in stages if isinstance(stage, dict)},
        "failures": loads("failures_json", []),
        "planned": loads("planned_json", []),
        "policy_errors": int(row["policy_errors"] or 0),
        "lesson": row["lesson"] or "",
    }


def recall(repo_root: str, limit: int = 3, db_path: str | None = None) -> list[dict]:
    """Newest episodes for this checkout, as prompt-sized summaries. Never raises."""
    path = db_path or default_db_path(repo_root)
    if not Path(path).is_file():
        return []
    try:
        with _connect(path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM episodes WHERE repo_root = ? ORDER BY finished_at DESC, run_id DESC LIMIT ?",
                (str(repo_root), int(limit)),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [_summary(row) for row in rows]
