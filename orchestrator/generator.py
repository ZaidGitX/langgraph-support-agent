"""Artifact generation for generated stages (Sprint 3, phase 5).

Astra completes provider-rendered skeletons; this module builds its payload, adds the
environment manifest (names only, written by code rather than by the model), writes the
files under ``var/generated/<run_id>/<stage>/``, and materializes them into the checkout
only after approval and only with ``ORCHESTRATOR_WRITE_ARTIFACTS=1``. Every artifact is
redacted before it is written and policy-checked before anyone sees it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from . import credentials, policy, redaction, retrieval, scanners, templates
from .schemas import ArtifactSet, GeneratedArtifact
from .state import MAX_ARTIFACT_REVISIONS

GENERATED_DIR = "generated"
CONTENT_LIMIT = 20_000
TOPICS_BY_FAMILY = {
    "build": ["build", "docker"],
    "test": ["test", "build"],
    "scan": ["scan", "docker"],
    "package": ["build", "workflows"],
    "container": ["docker", "build"],
    "release": ["workflows", "build"],
    "deploy": ["deploy", "env", "workflows"],
    "verify": ["deploy", "test"],
    "uat": ["test"],
    "custom": ["build", "test"],
}


def generated_dir(root: str, run_id: str, stage: str) -> Path:
    return Path(root) / "var" / GENERATED_DIR / str(run_id) / str(stage)


def placeholders_for(stage: dict, provider) -> list[str]:
    """Credential names this stage may reference: the provider's group plus its scanner's."""
    known = set(credentials.all_names())
    names: list[str] = list(provider.placeholders()) if provider is not None else []
    for name in scanners.env_names(str(stage.get("provider") or "")):
        if name in known and name not in names:
            names.append(name)
    return names


def context_for(state: dict, stage: dict, provider, *, policy_findings: list[dict] | None = None) -> dict:
    """The payload for ``planner.generate_artifacts``. Names only; no credential value."""
    root = str((state.get("repo") or {}).get("root") or ".")
    run_id = str(state.get("run_id") or "local")
    environment = dict(stage.get("environment") or {})
    names = placeholders_for(stage, provider)
    skeletons = provider.render_skeleton(stage, environment, run_id=run_id, placeholders=names)
    suggested_paths = {kind: templates.artifact_path(kind, str(stage.get("name")), environment) for kind in skeletons}
    topics = TOPICS_BY_FAMILY.get(str(stage.get("family") or "custom"), TOPICS_BY_FAMILY["custom"])
    ecosystem = state.get("ecosystem") or {}
    retrieved = {topic: hits for topic, hits in (ecosystem.get("retrieved") or {}).items() if topic in topics}
    if not retrieved:
        try:
            retrieved = retrieval.context_for(root, topics, per_topic=3)
        except (OSError, ValueError):
            retrieved = {}
    runner = str(stage.get("runner") or "local")
    runner_status = ((state.get("readiness") or {}).get("runners") or {}).get(runner) or {}
    repo = state.get("repo") or {}
    return {
        "stage": {
            "name": stage.get("name"),
            "family": stage.get("family"),
            "kind": stage.get("kind"),
            "runner": runner,
            "provider": stage.get("provider") or "",
            "environment": environment,
            "purpose": stage.get("purpose") or "",
            "artifacts": list(stage.get("artifacts") or []),
        },
        "run_id": run_id,
        "skeletons": skeletons,
        "suggested_paths": suggested_paths,
        "placeholders": names,
        "retrieved": retrieval.trim_context(retrieved, max_chars=6000, per_topic=3),
        "answers": dict(state.get("answers") or {}),
        "repo": {
            "languages": repo.get("languages"),
            "build_tools": repo.get("build_tools"),
            "dependency_signals": repo.get("dependency_signals"),
        },
        "readiness": {"runner": runner, "mode": runner_status.get("mode") or "plan", "missing": list(runner_status.get("missing") or [])},
        "policy_rules": policy.rules_summary(),
        "policy_findings": list(policy_findings or []),
    }


def env_manifest(stage: dict, placeholders: list[str]) -> GeneratedArtifact:
    """The ``.env.<tier>.example`` for this stage: every placeholder name, every value empty."""
    environment = dict(stage.get("environment") or {})
    content = templates.render_template("env_example", stage, environment, runner=str(stage.get("runner") or "local"), placeholders=placeholders)
    return GeneratedArtifact(
        kind="env_example",
        path=templates.artifact_path("env_example", str(stage.get("name")), environment),
        purpose="placeholder names this stage reads; values are set in the environment, never here",
        content=content,
        secrets_referenced=[name for name in placeholders if credentials.kind_of(name) == "secret"],
    )


def with_env_manifest(artifact_set: ArtifactSet, stage: dict, placeholders: list[str]) -> ArtifactSet:
    """Replace any model-written env manifest with the code-written one (names only)."""
    kept = [artifact for artifact in artifact_set.artifacts if artifact.kind != "env_example"]
    kept.append(env_manifest(stage, placeholders))
    return ArtifactSet(stage=artifact_set.stage, artifacts=kept[:6], notes=artifact_set.notes)


def _safe_target(base: Path, relative: str) -> Path | None:
    target = (base / relative).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        return None
    return target


def write(run_id: str, stage: dict, artifact_set: ArtifactSet, root: str) -> list[dict]:
    """Write every artifact under var/generated/<run>/<stage>/ and return the records.

    A record never carries the content (the approval view reads it from ``stored_at``);
    it carries the sha256, the size, the policy summary, and the findings.
    """
    base = generated_dir(root, run_id, str(stage.get("name") or "stage"))
    base.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    for artifact in artifact_set.artifacts:
        verdict = policy.check_artifact(artifact)
        content = redaction.redact(artifact.content)
        target = _safe_target(base, artifact.path) if verdict.ok or not any(f.rule == "path.outside" for f in verdict.findings) else None
        stored_at = ""
        if target is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            stored_at = str(target)
        records.append(
            {
                "path": artifact.path,
                "kind": artifact.kind,
                "purpose": artifact.purpose,
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "bytes": len(content.encode("utf-8")),
                "stored_at": stored_at,
                "policy": policy.summary(verdict),
                "findings": [item.model_dump() for item in verdict.findings],
                "secrets_referenced": list(artifact.secrets_referenced or []),
            }
        )
    return records


def policy_rows(records: list[dict], stage: str) -> list[dict]:
    rows: list[dict] = []
    for record in records:
        for item in record.get("findings") or []:
            rows.append({"stage": stage, "path": record.get("path"), **{k: item.get(k) for k in ("rule", "severity", "message", "line")}})
    return rows


def content_of(record: dict, limit: int = CONTENT_LIMIT) -> str:
    stored = str(record.get("stored_at") or "")
    if not stored or not Path(stored).is_file():
        return ""
    try:
        text = Path(stored).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = redaction.redact(text)
    return text if len(text) <= limit else text[:limit] + "\n…[truncated]"


def records_for_view(records: list[dict], limit: int = CONTENT_LIMIT) -> list[dict]:
    """Records plus content for the approval screen and the artifacts endpoint."""
    return [{**record, "content": content_of(record, limit)} for record in records]


def write_allowed() -> bool:
    return os.getenv("ORCHESTRATOR_WRITE_ARTIFACTS", "").strip() == "1"


def materialize(run_id: str, stage: dict, root: str, records: list[dict]) -> list[str]:
    """Copy approved artifacts into the checkout. Only with ORCHESTRATOR_WRITE_ARTIFACTS=1."""
    if not write_allowed():
        return []
    written: list[str] = []
    base = Path(root).resolve()
    for record in records:
        stored = str(record.get("stored_at") or "")
        if not stored or not Path(stored).is_file() or not (record.get("policy") or {}).get("ok", False):
            continue
        if not policy.check_path(str(record.get("path") or "")).ok:
            continue
        target = _safe_target(base, str(record["path"]))
        if target is None:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(stored, target)
        written.append(str(target.relative_to(base)))
    return written


def publish_branch(run_id: str, root: str, paths: list[str]) -> str:
    """Commit materialized artifacts on orchestrator/generated/<run_id> and push it.

    Only when ORCHESTRATOR_DISPATCH=github and ORCHESTRATOR_WRITE_ARTIFACTS=1. Returns the
    branch name, or an empty string when nothing was published. Never raises.
    """
    if not paths or not write_allowed() or os.getenv("ORCHESTRATOR_DISPATCH", "").strip().lower() != "github":
        return ""
    branch = f"orchestrator/generated/{run_id}"
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "orchestrator")
    env.setdefault("GIT_AUTHOR_EMAIL", "orchestrator@local")
    env.setdefault("GIT_COMMITTER_NAME", "orchestrator")
    env.setdefault("GIT_COMMITTER_EMAIL", "orchestrator@local")
    steps = [
        ["git", "checkout", "-B", branch],
        ["git", "add", "--", *paths],
        ["git", "commit", "-m", f"Generated artifacts for run {run_id}"],
        ["git", "push", "--set-upstream", "origin", branch],
    ]
    for argv in steps:
        try:
            completed = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=120, check=False, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if completed.returncode != 0:
            return ""
    return branch


def doc_relative_path(environment: dict | None) -> str:
    """The deploy document's artifact path: ``docs/deploy/<platform>-<tier>.md`` (a policy prefix)."""
    environment = environment or {}
    return f"docs/deploy/{environment.get('platform') or 'local'}-{environment.get('tier') or 'local'}.md"


def docs_path(root: str, run_id: str, environment: dict | None) -> Path:
    """Where the run-level deploy document is stored: the pseudo-stage folder ``pipeline`` so
    the stored path reads ``var/generated/<run>/pipeline/docs/deploy/<platform>-<tier>.md``
    and the policy checker's stage-relative path is a ``docs/deploy/`` one."""
    return generated_dir(root, run_id, "pipeline") / doc_relative_path(environment)


def revisions_allowed() -> int:
    return MAX_ARTIFACT_REVISIONS
