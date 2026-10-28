"""Stage vocabulary: families, runners, platforms, statuses, and named stage templates.

The nine fixed stages keep their LangGraph nodes and their catalog commands. Every other
registry name, and any validated custom slug, is data that the generic generated stage runs
in a later phase. Names are data, not graph nodes.

This module imports nothing from the package, so `state.py`, `schemas.py`, `catalog.py`, and
`graph.py` can all depend on it without an import cycle.

Two rules are enforced here and nowhere else: no stage name may refer to a production target,
and `uat` is the only name with a fixed place in the DAG (last).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STAGE_FAMILIES = (
    "build",
    "test",
    "scan",
    "package",
    "container",
    "release",
    "deploy",
    "verify",
    "uat",
    "custom",
)
RUNNERS = (
    "local",
    "docker",
    "github_actions",
    "jenkins",
    "aws_codebuild",
    "gcp_cloudbuild",
    "azure_pipelines",
    "ssh_remote",
    "kubernetes",
)
PLATFORMS = (
    "local",
    "docker",
    "github",
    "jenkins",
    "aws",
    "gcp",
    "azure",
    "remote",
    "kubernetes",
)
STATUSES = ("pending", "pass", "fail", "skipped", "planned", "blocked")
ARTIFACT_KINDS = (
    "github_workflow",
    "jenkinsfile",
    "buildspec",
    "cloudbuild",
    "azure_pipeline",
    "shell",
    "dockerfile",
    "compose",
    "env_example",
    "doc",
    "kubernetes_manifest",
    "script",
)

# Runners that execute on each platform, most natural first. `local` can also use docker.
PLATFORM_RUNNERS: dict[str, tuple[str, ...]] = {
    "local": ("local", "docker"),
    "docker": ("docker",),
    "github": ("github_actions",),
    "jenkins": ("jenkins",),
    "aws": ("aws_codebuild",),
    "gcp": ("gcp_cloudbuild",),
    "azure": ("azure_pipelines",),
    "remote": ("ssh_remote",),
    "kubernetes": ("kubernetes",),
}

CUSTOM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
# Exact names nobody may use as a stage name.
RESERVED_NAMES = ("production", "prod", "promote", "main")
# Any underscore-separated word that names a production target. `deploy_prod` and
# `release_to_production` are refused; `product_catalog_check` is not. Keep this equal to
# state.FORBIDDEN_TARGETS (a test pins that).
FORBIDDEN_NAME_TOKENS = ("production", "prod")


@dataclass(frozen=True)
class StageTemplate:
    name: str
    family: str
    summary: str  # under 100 characters; Astra sees it in the stage menu
    default_runners: tuple[str, ...] = ("local", "docker", "github_actions")
    providers: tuple[str, ...] = ()  # scanner catalog keys or provider names
    produces: tuple[str, ...] = ("shell",)  # artifact kinds Astra usually generates
    after: tuple[str, ...] = ()  # typical depends_on hint, not enforced
    env_names: tuple[str, ...] = ()  # credential registry keys; filled in with credentials.py


_BUILD_RUNNERS = ("local", "docker", "github_actions")
_TEST_RUNNERS = (
    "local",
    "docker",
    "github_actions",
    "aws_codebuild",
    "gcp_cloudbuild",
    "azure_pipelines",
    "jenkins",
)
_SCAN_RUNNERS = ("local", "docker", "github_actions", "jenkins")
_PACKAGE_RUNNERS = (
    "local",
    "github_actions",
    "aws_codebuild",
    "gcp_cloudbuild",
    "azure_pipelines",
)
_CONTAINER_RUNNERS = (
    "docker",
    "github_actions",
    "aws_codebuild",
    "gcp_cloudbuild",
    "azure_pipelines",
)
_RELEASE_RUNNERS = ("local", "github_actions")
_DEPLOY_RUNNERS = (
    "aws_codebuild",
    "gcp_cloudbuild",
    "azure_pipelines",
    "kubernetes",
    "ssh_remote",
    "docker",
)
_VERIFY_RUNNERS = (
    "local",
    "docker",
    "aws_codebuild",
    "gcp_cloudbuild",
    "azure_pipelines",
)
_DEPLOY_PROVIDERS = (
    "aws_ecs",
    "gcp_cloud_run",
    "azure_container_apps",
    "kubectl",
    "ssh_compose",
)


def _t(
    name: str,
    family: str,
    summary: str,
    runners: tuple[str, ...],
    providers: tuple[str, ...] = (),
    produces: tuple[str, ...] = ("shell",),
    after: tuple[str, ...] = (),
) -> StageTemplate:
    return StageTemplate(
        name=name,
        family=family,
        summary=summary,
        default_runners=runners,
        providers=providers,
        produces=produces,
        after=after,
    )


# The fixed stages run through catalog.commands_for (or their Sprint 2 workflow). They also
# run on `local`, so the two whose family row omits it list it first.
_TEMPLATES: tuple[StageTemplate, ...] = (
    # build
    _t("build", "build", "Compile, lint, audit dependencies, and scan for secrets (fixed catalog).", _BUILD_RUNNERS),
    _t("lint", "build", "Ruff and ESLint style checks.", _BUILD_RUNNERS),
    _t("compile", "build", "Byte-compile Python and type-check TypeScript without running tests.", _BUILD_RUNNERS),
    _t("frontend_build", "build", "Build the Vite frontend bundle.", _BUILD_RUNNERS),
    _t(
        "build_image",
        "build",
        "Build the Docker image locally. Nothing is pushed.",
        _BUILD_RUNNERS,
        providers=("hadolint",),
        produces=("dockerfile", "shell"),
    ),
    _t("dependency_install", "build", "Install pinned Python and npm dependencies with pip and npm ci.", _BUILD_RUNNERS),
    # test
    _t("test", "test", "Run the pytest suite (fixed catalog).", _TEST_RUNNERS, after=("build",)),
    _t("unit_test", "test", "Run unit tests only, without services or network.", _TEST_RUNNERS, after=("build",)),
    _t(
        "integration_test",
        "test",
        "Bring the compose stack up and exercise it over HTTP.",
        _TEST_RUNNERS,
        produces=("compose", "shell"),
        after=("build",),
    ),
    _t("e2e_test", "test", "Drive a browser or API flow end to end against the running app.", _TEST_RUNNERS, after=("build",)),
    _t(
        "contract_test",
        "test",
        "Check responses against the OpenAPI schema.",
        _TEST_RUNNERS,
        providers=("schemathesis",),
        after=("build",),
    ),
    _t(
        "perf_test",
        "test",
        "Run a short k6 or locust load profile and report latency.",
        _TEST_RUNNERS,
        providers=("k6", "locust"),
        after=("build",),
    ),
    _t("smoke_test", "test", "Probe the status endpoint of the running app.", _TEST_RUNNERS, after=("build",)),
    # scan
    _t(
        "scanners",
        "scan",
        "CodeQL, Sonar, Semgrep, Trivy, Snyk, and Gitleaks (fixed catalog).",
        _SCAN_RUNNERS,
        providers=("codeql", "sonar", "semgrep", "trivy", "snyk", "gitleaks"),
        after=("build",),
    ),
    _t(
        "dast",
        "scan",
        "Smoke the running app, then OWASP ZAP and StackHawk (fixed catalog).",
        _SCAN_RUNNERS,
        providers=("zap", "stackhawk"),
        after=("test",),
    ),
    _t("sast", "scan", "Static analysis of the source code.", _SCAN_RUNNERS, providers=("semgrep", "bandit", "codeql", "sonar"), after=("build",)),
    _t("sca", "scan", "Dependency vulnerability audit.", _SCAN_RUNNERS, providers=("pip_audit", "npm_audit", "osv_scanner", "snyk"), after=("build",)),
    _t("secrets_scan", "scan", "Search the tree and history for committed secrets.", _SCAN_RUNNERS, providers=("gitleaks", "trufflehog")),
    _t(
        "container_scan",
        "scan",
        "Scan a built image for known vulnerabilities.",
        _SCAN_RUNNERS,
        providers=("trivy", "grype"),
        after=("build_image",),
    ),
    _t("iac_scan", "scan", "Scan Terraform, CloudFormation, and Kubernetes files for misconfiguration.", _SCAN_RUNNERS, providers=("checkov", "tfsec")),
    _t("license_scan", "scan", "List dependency licenses and flag disallowed ones.", _SCAN_RUNNERS, providers=("trivy", "syft"), after=("build",)),
    _t("sbom", "scan", "Generate a software bill of materials.", _SCAN_RUNNERS, providers=("syft", "trivy"), after=("build",)),
    _t(
        "dast_scan",
        "scan",
        "Dynamic scan of the running app with a catalog scanner.",
        _SCAN_RUNNERS,
        providers=("zap", "stackhawk", "nuclei"),
        after=("test",),
    ),
    _t("dockerfile_lint", "scan", "Lint Dockerfiles for unsafe or wasteful instructions.", _SCAN_RUNNERS, providers=("hadolint",)),
    _t("workflow_lint", "scan", "Lint GitHub Actions workflows.", _SCAN_RUNNERS, providers=("actionlint",)),
    # package
    _t("package", "package", "Create the tar artifact. Nothing is uploaded (fixed catalog).", _PACKAGE_RUNNERS, after=("build",)),
    _t("package_artifact", "package", "Build a wheel or an npm pack archive.", _PACKAGE_RUNNERS, after=("build",)),
    _t(
        "publish_artifact",
        "package",
        "Upload an artifact to S3, GCS, Azure Blob, or GitHub. Plans without credentials.",
        _PACKAGE_RUNNERS,
        providers=("aws_s3", "gcs", "azure_blob", "github_release"),
        after=("package_artifact",),
    ),
    # container
    _t("container", "container", "Check the Dockerfile. No image is pushed (fixed catalog).", ("local", *_CONTAINER_RUNNERS)),
    _t(
        "push_image",
        "container",
        "Push an image to ECR, Artifact Registry, ACR, or GHCR. Plans without credentials.",
        _CONTAINER_RUNNERS,
        providers=("ecr", "artifact_registry", "acr", "ghcr"),
        produces=("shell", "dockerfile"),
        after=("build_image",),
    ),
    # release
    _t("release", "release", "Write the release plan. No registry is called (fixed catalog).", _RELEASE_RUNNERS, after=("package",)),
    _t("release_notes", "release", "Draft release notes from the git log.", _RELEASE_RUNNERS),
    _t("tag_release", "release", "Create an annotated tag. It is pushed only in apply mode.", _RELEASE_RUNNERS),
    _t("sign_artifact", "release", "Sign an artifact with cosign. Plans when no key is present.", _RELEASE_RUNNERS, providers=("cosign",)),
    # deploy
    _t("deploy", "deploy", "Gated deploy plan for local, dev, staging, or uat (fixed catalog).", ("local", *_DEPLOY_RUNNERS), after=("release",)),
    _t(
        "infra_plan",
        "deploy",
        "Plan infrastructure changes with Terraform, CDK, or CloudFormation.",
        _DEPLOY_RUNNERS,
        providers=("aws_cfn",),
    ),
    _t(
        "infra_apply",
        "deploy",
        "Apply infrastructure changes. Needs ORCHESTRATOR_CLOUD_APPLY=1, otherwise it plans.",
        _DEPLOY_RUNNERS,
        providers=("aws_cfn",),
        after=("infra_plan",),
    ),
    _t(
        "deploy_dev",
        "deploy",
        "Deploy to the dev tier on ECS, Cloud Run, Container Apps, Kubernetes, or ssh compose.",
        _DEPLOY_RUNNERS,
        providers=_DEPLOY_PROVIDERS,
        produces=("shell", "env_example", "doc"),
        after=("push_image",),
    ),
    _t(
        "deploy_staging",
        "deploy",
        "Deploy to the staging tier on ECS, Cloud Run, Container Apps, Kubernetes, or ssh compose.",
        _DEPLOY_RUNNERS,
        providers=_DEPLOY_PROVIDERS,
        produces=("shell", "env_example", "doc"),
        after=("push_image",),
    ),
    _t(
        "deploy_uat",
        "deploy",
        "Deploy to the uat tier on ECS, Cloud Run, Container Apps, Kubernetes, or ssh compose.",
        _DEPLOY_RUNNERS,
        providers=_DEPLOY_PROVIDERS,
        produces=("shell", "env_example", "doc"),
        after=("push_image",),
    ),
    _t("migrate_db", "deploy", "Plan or run database migrations on a non-production tier.", _DEPLOY_RUNNERS),
    _t(
        "rollback_plan",
        "deploy",
        "Write the rollback steps for the last deploy.",
        _DEPLOY_RUNNERS,
        providers=("aws_ecs", "kubectl"),
        produces=("shell", "doc"),
    ),
    # verify
    _t("post_deploy_smoke", "verify", "Probe the deployed service right after a deploy.", _VERIFY_RUNNERS),
    _t("health_check", "verify", "Poll the health endpoint until it is ready or times out.", _VERIFY_RUNNERS),
    _t(
        "observability_check",
        "verify",
        "Query CloudWatch, Cloud Logging, or Monitor for errors. Plans without credentials.",
        _VERIFY_RUNNERS,
        providers=("cloudwatch", "cloud_logging", "azure_monitor"),
        produces=("shell", "doc"),
    ),
    # uat
    _t("uat", "uat", "Checklist plus human sign-off. Always last, always local (fixed catalog).", ("local",)),
)

_FIXED_ORDER = (
    "build",
    "test",
    "scanners",
    "dast",
    "package",
    "container",
    "release",
    "deploy",
    "uat",
)

STAGE_TEMPLATES: dict[str, StageTemplate] = {item.name: item for item in _TEMPLATES}
FIXED_STAGES: dict[str, StageTemplate] = {name: STAGE_TEMPLATES[name] for name in _FIXED_ORDER}


def _clean(name: object) -> str | None:
    if not isinstance(name, str):
        return None
    return name.strip().lower()


def is_fixed(name: str) -> bool:
    return _clean(name) in FIXED_STAGES


def template_for(name: str) -> StageTemplate | None:
    cleaned = _clean(name)
    return STAGE_TEMPLATES.get(cleaned) if cleaned is not None else None


def family_of(name: str) -> str:
    template = template_for(name)
    return template.family if template else "custom"


def validate_stage_name(name: str) -> str:
    """Return the lower-case slug for a fixed name, a template name, or a custom slug.

    Anything else raises ValueError, including every name that refers to a production target.
    """
    slug = _clean(name)
    if slug is None:
        raise ValueError(f"stage name {name!r} is not allowed")
    if slug in RESERVED_NAMES or any(part in FORBIDDEN_NAME_TOKENS for part in slug.split("_")):
        raise ValueError(f"stage name {name!r} is not allowed")
    if slug in STAGE_TEMPLATES or CUSTOM_NAME_RE.match(slug):
        return slug
    raise ValueError(f"stage name {name!r} is not allowed")


def stage_menu() -> list[dict]:
    """The menu Astra sees: one row per template, JSON-safe."""
    return [
        {
            "name": item.name,
            "family": item.family,
            "summary": item.summary,
            "default_runners": list(item.default_runners),
            "providers": list(item.providers),
        }
        for item in STAGE_TEMPLATES.values()
    ]


def runners_for(platform: str) -> list[str]:
    """Runners that execute on a platform. Unknown platforms have none."""
    return list(PLATFORM_RUNNERS.get(str(platform).strip().lower(), ()))
