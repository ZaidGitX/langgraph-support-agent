"""Artifact skeletons (Sprint 3, phase 4).

``orchestrator/templates/*.tpl`` are copies of the skill's templates, rendered with
``string.Template.safe_substitute`` (``$NAME`` placeholders; ``$$`` is a literal dollar, so
``$${{ secrets.X }}`` becomes ``${{ secrets.X }}``). A skeleton keeps the trigger,
permissions, checkout, and placeholder block fixed and leaves ``# --- ASTRA: steps ---``
where Astra writes the stage's steps. Unknown ``$VARS`` are left untouched so a missing value
is visible instead of silently blank.
"""

from __future__ import annotations

import time
from pathlib import Path
from string import Template

TEMPLATE_DIR = Path(__file__).with_name("templates")
APP_NAME = "langgraph-support-app"
PORT = "8000"
PYTHON_VERSION = "3.12"
NODE_VERSION = "22"

KIND_FILES = {
    "github_workflow": "github-workflow.generated.yml.tpl",
    "jenkinsfile": "Jenkinsfile.tpl",
    "buildspec": "buildspec.yml.tpl",
    "cloudbuild": "cloudbuild.yaml.tpl",
    "azure_pipeline": "azure-pipelines.yml.tpl",
    "shell": "stage.sh.tpl",
    "script": "stage.sh.tpl",
    "dockerfile": "Dockerfile.generated.tpl",
    "compose": "docker-compose.generated.yml.tpl",
    "kubernetes_manifest": "kubernetes-deployment.yaml.tpl",
    "env_example": "env.target.example.tpl",
    "doc": "deploy-doc.md.tpl",
}

# Where a rendered artifact of each kind lives inside the checkout.
KIND_PATHS = {
    "github_workflow": ".github/workflows/gen-{stage}.yml",
    "jenkinsfile": "ci/{stage}.Jenkinsfile",
    "buildspec": "ci/buildspec-{stage}.yml",
    "cloudbuild": "ci/cloudbuild-{stage}.yaml",
    "azure_pipeline": "ci/azure-pipelines-{stage}.yml",
    "shell": "ci/{stage}.sh",
    "script": "ci/{stage}.sh",
    "dockerfile": "Dockerfile.generated",
    "compose": "docker-compose.generated.yml",
    "kubernetes_manifest": "deploy/k8s/{stage}.yaml",
    "env_example": ".env.{tier}.example",
    "doc": "docs/deploy/{platform}-{tier}.md",
}


def template_text(kind: str) -> str:
    name = KIND_FILES.get(kind)
    if not name:
        raise KeyError(f"no template for artifact kind {kind!r}")
    return (TEMPLATE_DIR / name).read_text(encoding="utf-8")


def artifact_path(kind: str, stage: str, environment: dict | None = None) -> str:
    environment = environment or {}
    return KIND_PATHS[kind].format(
        stage=stage, tier=environment.get("tier") or "local", platform=environment.get("platform") or "local"
    )


def _env_lines(kind: str, names: list[str]) -> str:
    """Placeholder lines in the format each skeleton expects (names only).

    These are substituted as values, which ``string.Template`` does not re-scan, so they
    carry the final ``${{ secrets.NAME }}`` / ``${NAME:-}`` text, not the ``$$`` escape the
    template files use.
    """
    if kind == "github_workflow":
        return "\n".join(f"  {name}: ${{{{ secrets.{name} }}}}" for name in names)
    if kind == "jenkinsfile":
        return "\n".join(f"    {name} = credentials('{name}')" for name in names)
    if kind in {"compose"}:
        return "\n".join(f"      {name}: ${{{name}:-}}" for name in names)
    if kind == "kubernetes_manifest":
        return "\n".join(
            f"            - name: {name}\n              valueFrom:\n                secretKeyRef:\n                  name: {APP_NAME}-secrets\n                  key: {name}"
            for name in names
        )
    if kind == "env_example":
        return "\n".join(f"{name}=" for name in names)
    if kind in {"shell", "script"}:
        return "\n".join(f': "${{{name}:=}}"' for name in names)
    return "\n".join(f"# {name}" for name in names)


def variables(stage: dict, environment: dict | None = None, *, runner: str = "local", run_id: str = "", placeholders: list[str] | None = None, extra: dict | None = None) -> dict:
    environment = environment or stage.get("environment") or {}
    names = list(placeholders or [])
    tier = str(environment.get("tier") or "local")
    platform = str(environment.get("platform") or "local")
    kind_hint = str((extra or {}).get("kind") or "")
    values = {
        "STAGE": str(stage.get("name") or "stage"),
        "FAMILY": str(stage.get("family") or "custom"),
        "PURPOSE": str(stage.get("purpose") or ""),
        "RUN_ID": run_id or "local",
        "TIER": tier,
        "PLATFORM": platform,
        "REGION": str(environment.get("region") or ""),
        "RUNNER": runner,
        "PROVIDER": str(stage.get("provider") or ""),
        "APP_NAME": APP_NAME,
        "PORT": PORT,
        "PYTHON_VERSION": PYTHON_VERSION,
        "NODE_VERSION": NODE_VERSION,
        "IMAGE": f"{APP_NAME}:{run_id or 'local'}",
        "GENERATED_AT": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "PLACEHOLDER_NAMES": ", ".join(names) if names else "none",
        "ENV_LINES": _env_lines(kind_hint, names),
        "SECRETS_LINES": _env_lines("github_workflow", names),
        "STAGE_TABLE": "",
        "READINESS_SUMMARY": "",
    }
    values.update({str(k): str(v) for k, v in (extra or {}).items() if k != "kind"})
    return values


def render_template(kind: str, stage: dict, environment: dict | None = None, *, runner: str = "local", run_id: str = "", placeholders: list[str] | None = None, extra: dict | None = None) -> str:
    """Render one skeleton. Unknown placeholders stay as ``$NAME`` so they are visible."""
    merged = dict(extra or {})
    merged["kind"] = kind
    values = variables(stage, environment, runner=runner, run_id=run_id, placeholders=placeholders, extra=merged)
    return Template(template_text(kind)).safe_substitute(values)
