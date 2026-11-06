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
import re
import shutil
import subprocess
from pathlib import Path

import yaml

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


PLATFORM_GROUPS = ("jenkins", "aws", "gcp", "azure")


def placeholders_for(stage: dict, provider) -> list[str]:
    """Credential names this stage may reference: the provider's group plus its scanner's.

    A stage that runs inside a platform (CodeBuild, Cloud Build, Azure Pipelines, Jenkins) never
    gets that platform's identity secrets: the service role or the agent is the identity there,
    and a file that named them made live run 9 inject a key pair and a session token the host
    does not have.
    """
    known = set(credentials.all_names())
    names: list[str] = list(provider.placeholders()) if provider is not None else []
    for name in scanners.env_names(str(stage.get("provider") or "")):
        if name in known and name not in names:
            names.append(name)
    group = credentials.group_for_runner(str(getattr(provider, "runner", "") or ""))
    if group in PLATFORM_GROUPS:
        hidden = set(credentials.identity_secrets(group))
        names = [name for name in names if name not in hidden]
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
        # The scan and test providers are a menu (Sprint 4): the answers choose, the catalog says
        # how each one runs on any agent (image, argv, env names, when to skip it).
        "scan_selection": scanners.selection_from_answers(state.get("answers") or {}),
        "scanner_catalog": scanners.catalog_for_prompt() if str(stage.get("family") or "") in {"scan", "security", "verify", "test"} else [],
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


_EMPTY_REGION = re.compile(r"^[ \t]*AWS_REGION:[ \t]*(\"\"|'')?[ \t]*$\n?", re.MULTILINE)
# A top-level ``artifacts:`` section and everything indented under it.
_ARTIFACTS_BLOCK = re.compile(r"^artifacts:[ \t]*(?:#[^\n]*)?\n(?:(?:[ \t]+[^\n]*|[ \t]*)\n?)*", re.MULTILINE)


def harden(artifact: GeneratedArtifact) -> GeneratedArtifact:
    """Code-level fixes to a model-written file that would break a platform build.

    A buildspec that sets ``AWS_REGION`` to nothing overrides the CodeBuild project's region
    and breaks every aws call (two files in live run 3); the project supplies the region. A
    buildspec with an ``artifacts:`` section fails at UPLOAD_ARTIFACTS because the project has
    no artifacts destination (live run 8: the test stage passed every phase and then failed).
    """
    if artifact.kind != "buildspec":
        return artifact
    content = artifact.content
    if _EMPTY_REGION.search(content):
        content = _EMPTY_REGION.sub("", content)
    if _ARTIFACTS_BLOCK.search(content):
        content = _ARTIFACTS_BLOCK.sub("", content).rstrip("\n") + "\n"
    content = ensure_bash_shell(content)
    content = strip_unresolvable_bindings(content)
    return artifact if content == artifact.content else artifact.model_copy(update={"content": content})


_ENV_LINE = re.compile(r"^env:[ \t]*(?:#[^\n]*)?\n", re.MULTILINE)
_SHELL_LINE = re.compile(r"^([ \t]+)shell:[^\n]*$", re.MULTILINE)
_VERSION_LINE = re.compile(r"^version:[^\n]*\n", re.MULTILINE)


def ensure_bash_shell(content: str) -> str:
    """Every buildspec says ``env.shell: bash``. CodeBuild runs each command with sh otherwise,
    and a model-written multi-line command that starts with ``set -euo pipefail`` dies there
    ("Illegal option -o pipefail": live run 11's sca stage, twice). Text edits keep comments."""
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError:
        return content
    if not isinstance(document, dict):
        return content
    env = document.get("env")
    if isinstance(env, dict) and str(env.get("shell") or "").strip() == "bash":
        return content
    if isinstance(env, dict) and env.get("shell") is not None:
        return _SHELL_LINE.sub(r"\1shell: bash", content, count=1)
    if isinstance(env, dict):
        match = _ENV_LINE.search(content)
        if match:
            return content[: match.end()] + "  shell: bash\n" + content[match.end():]
        env["shell"] = "bash"
        return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)
    match = _VERSION_LINE.search(content)
    if match:
        return content[: match.end()] + "\nenv:\n  shell: bash\n" + content[match.end():]
    return "env:\n  shell: bash\n" + content


def strip_unresolvable_bindings(content: str, platform: str = "aws") -> str:
    """Drop the ``env.secrets-manager`` entries CodeBuild could never resolve: the platform's own
    identity secrets (the role is the identity inside the build) and registry secrets with no
    value on the orchestrator host (nothing would be injected, and CodeBuild fails the build
    before its first command). Live run 9 escalated at injection on AWS_SESSION_TOKEN. A value
    that names no registry secret is left for the policy."""
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError:
        return content
    if not isinstance(document, dict):
        return content
    env = document.get("env")
    if not isinstance(env, dict) or not isinstance(env.get("secrets-manager"), dict):
        return content
    hidden = set(credentials.identity_secrets(platform))
    registry = credentials.secret_names()
    kept: dict = {}
    for key, value in env["secrets-manager"].items():
        named = {name for name in registry if name == str(key) or name in str(value)}
        if any(name in hidden or not credentials.is_present(name) for name in named):
            continue
        kept[key] = value
    if kept == env["secrets-manager"]:
        return content
    if kept:
        env["secrets-manager"] = kept
    else:
        del env["secrets-manager"]
    if not env:
        del document["env"]
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


_SCRIPT_SUFFIXES = (".sh", ".bash", ".py", ".ps1", ".js", ".ts", ".rb")
_WORKFLOW_DIR = ".github/workflows/"


def relocate(artifact: GeneratedArtifact, stage_name: str) -> GeneratedArtifact:
    """Move a model-written file that landed outside the allowed prefixes to where the policy
    (and the stage's commands) expect it, and say so in its purpose.

    Live run 5 lost two stages to ``scripts/ci/*.py`` and ``.github/workflows/integration.generated.yml``:
    scripts and other loose files go under ``ci/``, a workflow becomes
    ``.github/workflows/gen-<stage>.yml``, a Dockerfile or Compose file goes under ``docker/``.
    A path that already passes the policy is left alone; a path escaping the checkout is left
    for the policy to refuse.
    """
    original = (artifact.path or "").replace("\\", "/")
    while original.startswith("./"):
        original = original[2:]
    if not original or original.startswith("/") or ".." in original.split("/") or policy.check_path(original).ok:
        return artifact
    name = original.rsplit("/", 1)[-1]
    slug = re.sub(r"[^a-z0-9_]+", "_", str(stage_name or "stage").lower()).strip("_") or "stage"
    if original.startswith(_WORKFLOW_DIR) or artifact.kind == "github_workflow":
        target = f"{_WORKFLOW_DIR}gen-{slug}.yml"
    elif name.startswith(("Dockerfile", "docker-compose")) or artifact.kind in ("dockerfile", "compose"):
        target = f"docker/{name}"
    else:
        # Scripts, loose files, and anything under scripts/ or another folder of the model's choosing.
        target = f"ci/{name}"
    if not policy.check_path(target).ok:
        return artifact
    purpose = f"{artifact.purpose} (moved from {original} to {target})".strip()
    return artifact.model_copy(update={"path": target, "purpose": purpose[:400]})


# Generated-stage floors on AWS CodeBuild (live run 8: the deploy stage came back as the
# untouched skeleton). A stage of these families gets the orchestrator's contract script when
# its buildspec is missing, still a placeholder, or does not do the family's job.
_FLOOR_BY_FAMILY = {"deploy": "deploy", "verify": "smoke", "container": "container"}
# What a floored stage must call, and what counts as already doing the job. A stack mutation is
# the contract's alone: live run 12's infra_plan ran its own change set without the stack's
# parameters, so for deploy and plan stages only the contract call counts.
_FLOOR_COMMANDS = {
    "deploy": "bash ci/deploy.sh",
    "deploy_plan": "DEPLOY_MODE=plan bash ci/deploy.sh",
    "smoke": "bash ci/smoke.sh",
    "container": "bash ci/container.sh",
}
_FLOOR_MARKERS = {"deploy": ("ci/deploy.sh",), "deploy_plan": ("ci/deploy.sh",), "smoke": ("curl", "http", "urlopen", "requests"), "container": ("docker build", "docker buildx")}
_STACK_MUTATION = re.compile(r"\baws\s+cloudformation\s+(deploy|create-stack|update-stack|create-change-set|execute-change-set)\b")
_PLACEHOLDER_MARK = re.compile(r"echo \"stage [A-Za-z0-9_$-]+ placeholder\"")


def floor_for(stage: dict) -> str | None:
    """The contract command key a generated stage falls back to, or None. An infrastructure plan
    stage plans with the deploy contract (nothing created); infra_apply deploys with it."""
    family = str(stage.get("family") or "")
    name = str(stage.get("name") or "").lower()
    if "infra" in name or name.endswith("_plan") or name.startswith("plan_"):
        return "deploy" if "apply" in name else "deploy_plan"
    if family in _FLOOR_BY_FAMILY:
        return _FLOOR_BY_FAMILY[family]
    if "deploy" in name:
        return "deploy"
    if "smoke" in name:
        return "smoke"
    if "build_image" in name or "push_image" in name or name.endswith("_image"):
        return "container"
    return None


def platform_floor(artifacts: list[GeneratedArtifact], stage: dict) -> list[GeneratedArtifact]:
    """On aws_codebuild, make sure a deploy, verify, or image stage's buildspec does its job:
    the contract script is called when the model left the placeholder or wrote none."""
    if str(stage.get("runner") or "") != "aws_codebuild":
        return artifacts
    key = floor_for(stage)
    if not key:
        return artifacts
    command = _FLOOR_COMMANDS[key]
    markers = _FLOOR_MARKERS[key]
    specs = [item for item in artifacts if item.kind == "buildspec"]
    others = [item for item in artifacts if item.kind != "buildspec"]
    if not specs:
        environment = dict(stage.get("environment") or {})
        skeleton = templates.render_template("buildspec", stage, environment, runner="aws_codebuild", run_id="", placeholders=[])
        skeleton = re.sub(r'^(\s*)- echo "stage [^"]* placeholder"[ \t]*$', lambda m: f"{m.group(1)}- {command}", skeleton, flags=re.MULTILINE)
        specs = [GeneratedArtifact(path=templates.artifact_path("buildspec", str(stage.get("name")), environment), kind="buildspec",
                                   purpose=f"orchestrator floor: {command}", content=skeleton, secrets_referenced=[])]
    fixed: list[GeneratedArtifact] = []
    for spec in specs:
        content = spec.content
        lowered = content.lower()
        placeholder = bool(_PLACEHOLDER_MARK.search(content))
        does_job = any(marker in lowered for marker in markers) or command in content
        mutates = key in ("deploy", "deploy_plan") and bool(_STACK_MUTATION.search(content))
        if placeholder or not does_job or mutates:
            lines = [line for line in content.splitlines() if not _PLACEHOLDER_MARK.search(line)]
            content = "\n".join(lines).rstrip("\n") + "\n"
            if mutates:
                content = _drop_stack_mutations(content)
            if command not in content:
                content = _append_build_command(content, command)
            spec = spec.model_copy(update={"content": content, "purpose": f"{spec.purpose} (orchestrator floor: {command})".strip()})
        fixed.append(spec)
    return others + fixed


def _drop_stack_mutations(content: str) -> str:
    """Remove buildspec commands that create, update, or change-set the stack themselves; the
    contract script does that with every parameter resolved."""
    import yaml

    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError:
        return content
    if not isinstance(document, dict) or not isinstance(document.get("phases"), dict):
        return content
    changed = False
    for phase in document["phases"].values():
        if not isinstance(phase, dict) or not isinstance(phase.get("commands"), list):
            continue
        kept = [item for item in phase["commands"] if not (isinstance(item, str) and _STACK_MUTATION.search(item))]
        if len(kept) != len(phase["commands"]):
            phase["commands"] = kept
            changed = True
    if not changed:
        return content
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


def _append_build_command(content: str, command: str) -> str:
    import yaml

    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError:
        document = None
    if not isinstance(document, dict):
        return content.rstrip("\n") + f"\nphases:\n  build:\n    commands:\n      - {command}\n"
    phases = document.get("phases") if isinstance(document.get("phases"), dict) else {}
    build = phases.get("build") if isinstance(phases.get("build"), dict) else {}
    commands = [item for item in (build.get("commands") or []) if isinstance(item, str) and "placeholder" not in item] if isinstance(build.get("commands"), list) else []
    commands.append(command)
    build["commands"] = commands
    phases["build"] = build
    document["phases"] = phases
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


def with_env_manifest(artifact_set: ArtifactSet, stage: dict, placeholders: list[str]) -> ArtifactSet:
    """Replace any model-written env manifest with the code-written one (names only);
    relocate a file written outside the allowed prefixes; harden every file; on aws_codebuild
    give a deploy, verify, or image stage the orchestrator's floor when the model's file
    would do nothing."""
    kept = [harden(relocate(artifact, str(stage.get("name") or ""))) for artifact in artifact_set.artifacts if artifact.kind != "env_example"]
    kept = platform_floor(kept, stage)
    kept = scope_scan_contract(kept, stage)
    kept.append(env_manifest(stage, placeholders))
    return ArtifactSet(stage=artifact_set.stage, artifacts=kept[:6], notes=artifact_set.notes)


_SCANNERS_CALL = re.compile(r"(?<![\w\"'=/])bash ci/scanners\.sh")
_SCOPED_KINDS = ("buildspec", "shell", "script", "github_workflow", "jenkinsfile", "cloudbuild", "azure_pipeline")


def scope_scan_contract(artifacts: list[GeneratedArtifact], stage: dict) -> list[GeneratedArtifact]:
    """A generated scan stage runs only its own provider through the scanners contract. Live run
    13's sca stage called ``bash ci/scanners.sh`` with no selection, re-ran the whole menu, and
    died on a tool it did not own; the fixed scanners stage is the one that runs the full menu."""
    if str(stage.get("family") or "") != "scan":
        return artifacts
    provider = str(stage.get("provider") or "").strip().lower().replace("-", "_")
    if provider not in scanners.SCAN_STAGE_TOOLS:
        return artifacts
    prefix = f'SCAN_TOOLS="{provider.replace("_", "-")}" '
    scoped: list[GeneratedArtifact] = []
    for item in artifacts:
        if item.kind not in _SCOPED_KINDS or "ci/scanners.sh" not in item.content:
            scoped.append(item)
            continue
        lines = item.content.splitlines(keepends=True)
        changed = False
        for index, line in enumerate(lines):
            if "ci/scanners.sh" in line and "SCAN_TOOLS=" not in line:
                replaced = _SCANNERS_CALL.sub(prefix + "bash ci/scanners.sh", line)
                changed = changed or replaced != line
                lines[index] = replaced
        scoped.append(item.model_copy(update={"content": "".join(lines)}) if changed else item)
    return scoped


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
