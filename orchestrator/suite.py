"""Platform suites (Sprint 4, phase 9): the whole pipeline in one platform's own format.

With ``ORCHESTRATOR_DISPATCH=platform`` and a platform answer (jenkins, aws, gcp, azure) the
fixed stages run on that platform instead of the local host or GitHub Actions. Astra
translates the repository's fixed workflows into the platform's native files: a Jenkinsfile,
Cloud Build config, or Azure Pipelines YAML with one stage per fixed stage, or one CodeBuild
buildspec per stage, plus the provisioning document the platform needs (a CodeBuild project,
a Jenkins job) and a deploy stack. The policy checks every file, Jev reviews the set, and the
person sees it before approval. Files are written under ``var/generated/<run>/pipeline/``.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from . import credentials, generator, policy, scanners, templates
from .schemas import ArtifactReview, PipelineSuite, PolicyResult
from .state import STAGE_NAMES, platform_of, target_of

SUITE_STAGE = "pipeline"
SUITE_PLATFORMS = ("jenkins", "aws", "gcp", "azure")
WORKFLOW_FOR_STAGE = {
    "build": "build.yml",
    "test": "test.yml",
    "scanners": "scanners.yml",
    "dast": "dast.yml",
    "package": "package.yml",
    "container": "ci.yml",
    "release": "release.yml",
    "deploy": "deploy.yml",
}
WORKFLOW_CHARS = 6000

# Portable bash for each fixed stage: what the GitHub workflow does, without GitHub. Astra
# rewrites these for the platform; they are the hints in every skeleton. Scanner lines use
# the catalog's docker images so the platform needs Docker, not a dozen installs.
STAGE_COMMAND_HINTS: dict[str, list[str]] = {
    "build": [
        "python -m pip install -r requirements-dev.txt",
        "python -m compileall -q app mcp_server orchestrator",
        "ruff check app mcp_server orchestrator tests scripts",
        "pip-audit -r requirements.txt --format json -o audit.json || true",
        "python scripts/audit_gate.py audit.json",
    ],
    "test": [
        "python -m pip install -r requirements-dev.txt",
        "python -m pytest -q",
    ],
    "scanners": [],  # filled from the scanner catalog
    "dast": [
        'python -c "import os, urllib.request; urllib.request.urlopen(os.environ[\'APP_PUBLIC_URL\'].rstrip(\'/\') + \'/status\', timeout=10).read()"',
    ],
    "package": ["python -m orchestrator.local_ops package"],
    "container": ['docker build -t "$APP_NAME:$RUN_ID" .'],
    "release": ["python -m orchestrator.local_ops release"],
    "deploy": [
        'case "$TIER" in production|prod) echo "No path past UAT."; exit 2;; esac',
        'python -m orchestrator.local_ops deploy --target "$TIER"',
    ],
}
SCANNER_TOOLS = {"scanners": ("semgrep", "trivy", "gitleaks", "snyk"), "dast": ("zap", "stackhawk")}


def wants_suite(state: dict) -> bool:
    """Platform dispatch is on and the answers name a suite platform."""
    if os.getenv("ORCHESTRATOR_DISPATCH", "local").strip().lower() != "platform":
        return False
    return platform_of(state.get("answers") or {}) in SUITE_PLATFORMS


def platform_for(state: dict) -> str:
    return platform_of(state.get("answers") or {})


def suite_stages(pipeline: dict) -> list[dict]:
    """The approved fixed stages the suite must carry, in pipeline order. UAT stays local."""
    return [
        dict(stage)
        for stage in (pipeline or {}).get("stages") or []
        if stage.get("kind", "fixed") == "fixed" and stage.get("name") in STAGE_NAMES and stage.get("name") != "uat"
    ]


def _docker_lines(stage_name: str) -> list[str]:
    lines: list[str] = []
    for tool in SCANNER_TOOLS.get(stage_name, ()):
        item = scanners.entry(tool) or {}
        image = str(item.get("docker_image") or "")
        if not image:
            continue
        env_names = [name for name in item.get("env") or [] if name in credentials.all_names()]
        env_flags = " ".join(f"-e {name}" for name in env_names)
        argv = " ".join(str(part) for part in ((item.get("argv") or {}).get(stage_name) or [])[1:])
        lines.append(f'docker run --rm {env_flags} -v "$PWD:/src" -w /src {image} {argv}'.replace("  ", " ").rstrip())
    return lines


def command_hints(stage_name: str) -> list[str]:
    hints = list(STAGE_COMMAND_HINTS.get(stage_name) or [])
    if stage_name in SCANNER_TOOLS:
        hints.extend(_docker_lines(stage_name))
    return hints or [f'echo "stage {stage_name}: translate .github/workflows/{WORKFLOW_FOR_STAGE.get(stage_name, stage_name + ".yml")}"']


def _indent(lines: list[str], spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in lines)


def _jenkins_block(stage: dict) -> str:
    name = stage["name"]
    steps = "\n".join(f"        sh '{line.replace(chr(39), chr(34))}'" for line in command_hints(name))
    return (
        f"    stage('{name}') {{\n"
        f"      when {{ expression {{ params.STAGE == '' || params.STAGE == '{name}' }} }}\n"
        f"      steps {{\n"
        f"        // --- ASTRA: {name} steps --- translate .github/workflows/{WORKFLOW_FOR_STAGE.get(name, name + '.yml')} for Jenkins.\n"
        f"{steps}\n"
        f"        echo \"stage {name} finished for run ${{params.RUN_ID}}\"\n"
        f"      }}\n"
        f"    }}"
    )


def _cloudbuild_block(stage: dict, previous: str) -> str:
    name = stage["name"]
    body = "\n".join(f"        {line}" for line in command_hints(name))
    return (
        f"  - id: {name}\n"
        f"    name: python:{templates.PYTHON_VERSION}\n"
        f"    entrypoint: bash\n"
        f"    env: [\"RUN_ID=$_RUN_ID\", \"TIER=$_TIER\", \"STAGE=$_STAGE\", \"APP_NAME=$_APP_NAME\"]\n"
        f"    args:\n"
        f"      - -c\n"
        f"      - |\n"
        f"        if [ -n \"$${{STAGE}}\" ] && [ \"$${{STAGE}}\" != \"{name}\" ]; then echo \"skip {name}\"; exit 0; fi\n"
        f"        # --- ASTRA: {name} steps --- translate .github/workflows/{WORKFLOW_FOR_STAGE.get(name, name + '.yml')} for Cloud Build.\n"
        f"{body}\n"
        f"        echo \"stage {name} finished for run $${{RUN_ID}}\"\n"
        f"    waitFor: [\"{previous}\"]"
    )


def _azure_block(stage: dict, previous: str) -> str:
    name = stage["name"]
    body = "\n".join(f"              {line}" for line in command_hints(name))
    return (
        f"  - stage: {name}\n"
        f"    dependsOn: {previous}\n"
        f"    condition: and(succeeded(), or(eq('${{{{ parameters.stage }}}}', ''), eq('${{{{ parameters.stage }}}}', '{name}')))\n"
        f"    jobs:\n"
        f"      - job: {name}\n"
        f"        steps:\n"
        f"          - task: UsePythonVersion@0\n"
        f"            inputs:\n"
        f"              versionSpec: '{templates.PYTHON_VERSION}'\n"
        f"          - bash: |\n"
        f"              # --- ASTRA: {name} steps --- translate .github/workflows/{WORKFLOW_FOR_STAGE.get(name, name + '.yml')} for Azure Pipelines.\n"
        f"{body}\n"
        f"              echo \"stage {name} finished for run ${{{{ parameters.runId }}}}\"\n"
        f"            displayName: {name}\n"
        f"            env:\n"
        f"              RUN_ID: ${{{{ parameters.runId }}}}\n"
        f"              TIER: ${{{{ parameters.tier }}}}"
    )


def _secrets_manager_lines(names: list[str]) -> str:
    secret_names = [name for name in names if credentials.kind_of(name) == "secret"]
    if not secret_names:
        return "  # secrets-manager references go here by NAME when a stage needs one"
    lines = ["  secrets-manager:"]
    lines.extend(f"    {name}: \"{templates.APP_NAME}/{name}:{name}\"" for name in secret_names)
    return "\n".join(lines)


def _container_env_lines(names: list[str]) -> str:
    config_names = [name for name in names if credentials.kind_of(name) != "secret"][:8]
    return "\n".join(f"            - {{Name: {name}, Value: \"${name}\"}}" for name in config_names)


def render_skeletons(platform: str, stages: list[dict], environment: dict, *, run_id: str, placeholders: list[str]) -> dict[str, str]:
    """Suite skeletons keyed by artifact path (not by kind: CodeBuild has one buildspec per stage)."""
    names = [stage["name"] for stage in stages]
    base_stage = {"name": SUITE_STAGE, "family": "custom", "purpose": "the whole pipeline", "environment": environment}
    skeletons: dict[str, str] = {}
    branch = os.getenv("GITHUB_REF_NAME", "").strip() or "main"
    if platform == "jenkins":
        blocks = "\n".join(_jenkins_block(stage) for stage in stages)
        skeletons["ci/pipeline.Jenkinsfile"] = templates.render_template(
            "jenkinsfile", base_stage, environment, runner="jenkins", run_id=run_id, placeholders=placeholders,
            extra={"STAGE_BLOCKS": blocks, "STAGE_NAMES": ", ".join(names)}, template=templates.SUITE_TEMPLATES["jenkinsfile"],
        )
        skeletons["deploy/jenkins-job.xml"] = templates.render_template(
            "jenkins_job", base_stage, environment, runner="jenkins", run_id=run_id, placeholders=placeholders, extra={"BRANCH": branch},
        )
    elif platform == "aws":
        for stage in stages:
            name = stage["name"]
            skeletons[f"ci/buildspec-{name}.yml"] = templates.render_template(
                "buildspec", stage, environment, runner="aws_codebuild", run_id=run_id, placeholders=placeholders,
                extra={
                    "STAGE_COMMANDS": "\n".join(f"      - {line}" for line in command_hints(name)),
                    "WORKFLOW_FILE": WORKFLOW_FOR_STAGE.get(name, f"{name}.yml"),
                    "SECRETS_MANAGER_LINES": _secrets_manager_lines(placeholders),
                },
                template=templates.SUITE_TEMPLATES["buildspec"],
            )
        skeletons["deploy/codebuild-project.json"] = templates.render_template(
            "codebuild_project", base_stage, environment, runner="aws_codebuild", run_id=run_id, placeholders=placeholders,
        )
        if any(stage["name"] == "deploy" for stage in stages):
            skeletons["deploy/ecs-stack.yaml"] = templates.render_template(
                "cloudformation", base_stage, environment, runner="aws_codebuild", run_id=run_id, placeholders=placeholders,
                extra={"CONTAINER_ENV_LINES": _container_env_lines(placeholders)},
            )
    elif platform == "gcp":
        blocks = []
        previous = "refuse-production"
        for stage in stages:
            blocks.append(_cloudbuild_block(stage, previous))
            previous = stage["name"]
        skeletons["ci/cloudbuild.yaml"] = templates.render_template(
            "cloudbuild", base_stage, environment, runner="gcp_cloudbuild", run_id=run_id, placeholders=placeholders,
            extra={"STAGE_BLOCKS": "\n".join(blocks)}, template=templates.SUITE_TEMPLATES["cloudbuild"],
        )
    elif platform == "azure":
        blocks = []
        previous = "refuse_production"
        for stage in stages:
            blocks.append(_azure_block(stage, previous))
            previous = stage["name"]
        skeletons["ci/azure-pipelines.yml"] = templates.render_template(
            "azure_pipeline", base_stage, environment, runner="azure_pipelines", run_id=run_id, placeholders=placeholders,
            extra={"STAGE_BLOCKS": "\n".join(blocks)}, template=templates.SUITE_TEMPLATES["azure_pipeline"],
        )
    return skeletons


def workflow_texts(root: str, stages: list[dict]) -> dict[str, str]:
    """The fixed GitHub workflows the suite translates, trimmed for the prompt."""
    texts: dict[str, str] = {}
    folder = Path(root) / ".github" / "workflows"
    for stage in stages:
        name = WORKFLOW_FOR_STAGE.get(stage["name"])
        if not name or name in texts:
            continue
        path = folder / name
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            texts[name] = text if len(text) <= WORKFLOW_CHARS else text[:WORKFLOW_CHARS] + "\n# …[truncated]"
    return texts


def context_for(state: dict, provider, *, policy_findings: list[dict] | None = None) -> dict:
    """The payload for ``planner.generate_suite``. Names only; never a credential value."""
    root = str((state.get("repo") or {}).get("root") or ".")
    run_id = str(state.get("run_id") or "local")
    pipeline = state.get("pipeline") or {}
    stages = suite_stages(pipeline)
    platform = platform_for(state)
    environment = dict(pipeline.get("environment") or {})
    # A cloud platform never runs the "local" tier: the answered target (dev, staging, uat) wins.
    if environment.get("tier", "local") == "local":
        environment["tier"] = target_of(state.get("answers") or {})
        if environment["tier"] == "local":
            environment["tier"] = "dev"
    environment["platform"] = platform
    names = list(provider.placeholders())
    for stage_name in (stage["name"] for stage in stages):
        for tool in SCANNER_TOOLS.get(stage_name, ()):
            for extra_name in scanners.env_names(tool):
                if extra_name in credentials.all_names() and extra_name not in names:
                    names.append(extra_name)
    skeletons = render_skeletons(platform, stages, environment, run_id=run_id, placeholders=names)
    runner_status = ((state.get("readiness") or {}).get("runners") or {}).get(provider.runner) or {}
    return {
        "platform": platform,
        "runner": provider.runner,
        "run_id": run_id,
        "environment": environment,
        "stages": [
            {
                "name": stage["name"],
                "family": stage.get("family") or "",
                "purpose": stage.get("purpose") or "",
                "depends_on": list(stage.get("depends_on") or []),
                "workflow": WORKFLOW_FOR_STAGE.get(stage["name"], ""),
                "command_hints": command_hints(stage["name"]),
            }
            for stage in stages
        ],
        "workflows": workflow_texts(root, stages),
        "skeletons": skeletons,
        "suggested_paths": sorted(skeletons),
        "placeholders": names,
        "scanners": {
            tool: {"docker_image": (scanners.entry(tool) or {}).get("docker_image", ""), "env": (scanners.entry(tool) or {}).get("env", [])}
            for stage in stages
            for tool in SCANNER_TOOLS.get(stage["name"], ())
        },
        "answers": dict(state.get("answers") or {}),
        "readiness": {"runner": provider.runner, "mode": runner_status.get("mode") or "plan", "missing": list(runner_status.get("missing") or [])},
        "policy_rules": policy.rules_summary(),
        "policy_findings": list(policy_findings or []),
    }


def with_manifest(suite_obj: PipelineSuite, environment: dict, placeholders: list[str]) -> PipelineSuite:
    """Append the env manifest (names only) unless Astra already wrote one."""
    if not any(item.kind == "env_example" for item in suite_obj.artifacts):
        suite_obj.artifacts.append(generator.env_manifest({"name": SUITE_STAGE, "environment": environment}, placeholders))
    return suite_obj


def check(suite: PipelineSuite) -> PolicyResult:
    """Every artifact through the text and path rules (the suite may carry more than six files)."""
    findings = []
    for artifact in suite.artifacts:
        findings.extend(policy.check_artifact(artifact).findings)
    return PolicyResult(ok=not any(item.severity == "error" for item in findings), findings=findings)


def as_artifact_set(suite: PipelineSuite) -> SimpleNamespace:
    """A ``generator.write``-compatible object (duck-typed: ``.artifacts`` and ``.stage``)."""
    return SimpleNamespace(stage=SUITE_STAGE, artifacts=list(suite.artifacts), notes=suite.notes)


def stage_file(records: list[dict], stage_name: str, platform: str) -> str:
    """The artifact path that runs one fixed stage on this platform."""
    if platform == "aws":
        for record in records:
            if str(record.get("path") or "").endswith(f"buildspec-{stage_name}.yml"):
                return str(record["path"])
    wanted = {"jenkins": "ci/pipeline.Jenkinsfile", "gcp": "ci/cloudbuild.yaml", "azure": "ci/azure-pipelines.yml"}.get(platform, "")
    for record in records:
        if str(record.get("path") or "") == wanted:
            return wanted
    return ""


def generate(state: dict, planner, provider, review, *, max_revisions: int = 1) -> dict:
    """Astra writes the suite, the policy checks it, Jev reviews it, the files are stored.

    ``review(result, revisions_left, paths)`` is the graph's artifact-review callback and
    returns an ``ArtifactReview``. Returns ``records``, ``review``, ``policy_rows``, ``ok``,
    ``platform``, ``stage_files``.
    """
    root = str((state.get("repo") or {}).get("root") or ".")
    run_id = str(state.get("run_id") or "local")
    platform = platform_for(state)
    context = context_for(state, provider)
    suite = planner.generate_suite(context)
    suite = with_manifest(suite, context["environment"], context["placeholders"])
    result = check(suite)
    revisions_left = max_revisions
    verdict: ArtifactReview = review(result, revisions_left, [item.path for item in suite.artifacts])
    if verdict.verdict == "revise" and revisions_left > 0:
        revisions_left -= 1
        try:
            revised = planner.generate_suite(context_for(state, provider, policy_findings=[item.model_dump() for item in result.findings]))
            suite = with_manifest(revised, context["environment"], context["placeholders"])
        except (RuntimeError, ValueError, TypeError, OSError):
            pass
        result = check(suite)
        verdict = review(result, revisions_left, [item.path for item in suite.artifacts])
    records = generator.write(run_id, {"name": SUITE_STAGE, "environment": context["environment"]}, as_artifact_set(suite), root)
    stage_files = {stage["name"]: stage_file(records, stage["name"], platform) for stage in suite_stages(state.get("pipeline") or {})}
    return {
        "records": records,
        "review": {"verdict": verdict.verdict, "rationale": verdict.rationale, "revisions": max_revisions - revisions_left, "ok": result.ok},
        "policy_rows": generator.policy_rows(records, SUITE_STAGE),
        "ok": result.ok and verdict.verdict != "reject",
        "platform": platform,
        "stage_files": stage_files,
    }
