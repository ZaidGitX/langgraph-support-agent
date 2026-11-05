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

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import yaml

from . import credentials, generator, policy, scanners, templates
from .llm import AstraQuotaError
from .schemas import ArtifactReview, GeneratedArtifact, PipelineSuite, PolicyResult
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
# The scan menu (Sprint 4): every provider the answers may select; their credential names
# join the placeholders when provided, so a suite can bind them by name.
SCANNER_TOOLS = {"scanners": scanners.SCAN_STAGE_TOOLS, "dast": scanners.DAST_STAGE_TOOLS}


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
        parts = [str(part) for part in ((item.get("argv") or {}).get(stage_name) or [])]
        if parts and parts[0] == "docker":  # the catalog already spells the docker invocation
            lines.append(" ".join(parts).replace("$ROOT", "$PWD"))
            continue
        env_names = [name for name in item.get("env") or [] if name in credentials.all_names()]
        env_flags = " ".join(f"-e {name}" for name in env_names)
        argv = " ".join(parts[1:])
        lines.append(f'docker run --rm {env_flags} -v "$PWD:/src" -w /src {image} {argv}'.replace("  ", " ").rstrip())
    return lines


def selection_prefix(stage_name: str, answers: dict | None) -> str:
    """``SCAN_TOOLS="..." SCAN_SEVERITY="..." `` (or ``DAST_TOOLS="..." ``) in front of a scan or
    DAST contract call, so the person's menu reaches the script on every platform. Empty when
    there are no answers (the script then runs every catalog tool) and for other stages."""
    if not answers:
        return ""
    env = scanners.selection_env(answers)
    key = CONTRACT_STAGE_KEYS.get(stage_name)
    if key == "scanners":
        return f'SCAN_TOOLS="{env["SCAN_TOOLS"]}" SCAN_SEVERITY="{env["SCAN_SEVERITY"]}" '
    if key == "dast":
        return f'DAST_TOOLS="{env["DAST_TOOLS"]}" '
    return ""


def command_hints(stage_name: str, platform: str = "", answers: dict | None = None) -> list[str]:
    """What a stage runs on the platform: the orchestrator's contract script when there is one
    for this stage and platform (with the person's scan and DAST selection in front when answers
    are given), else the portable lines (what the GitHub workflow does)."""
    contract = contract_command(stage_name, platform)
    if contract:
        return [selection_prefix(stage_name, answers) + contract]
    hints = list(STAGE_COMMAND_HINTS.get(stage_name) or [])
    if stage_name in SCANNER_TOOLS:
        hints.extend(_docker_lines(stage_name))
    return hints or [f'echo "stage {stage_name}: translate .github/workflows/{WORKFLOW_FOR_STAGE.get(stage_name, stage_name + ".yml")}"']


def _indent(lines: list[str], spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in lines)


def _jenkins_block(stage: dict, answers: dict | None = None) -> str:
    name = stage["name"]
    steps = "\n".join(f"        sh '{line.replace(chr(39), chr(34))}'" for line in command_hints(name, "jenkins", answers))
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


def _cloudbuild_block(stage: dict, previous: str, answers: dict | None = None) -> str:
    name = stage["name"]
    body = "\n".join(f"        {line}" for line in command_hints(name, "gcp", answers))
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


def _azure_block(stage: dict, previous: str, answers: dict | None = None) -> str:
    name = stage["name"]
    body = "\n".join(f"              {line}" for line in command_hints(name, "azure", answers))
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


def injectable_secrets(names: list[str], platform: str) -> list[str]:
    """The secret names a suite may reference: ones the orchestrator will inject before a build.

    A reference the platform cannot resolve fails the build at its start (CodeBuild resolves
    every ``secrets-manager`` entry first), so a stage file may name only a secret that has a
    value on the orchestrator host (vault or environment) and is not the platform's own CLI
    identity (inside the platform the service role or the agent is the identity, and static
    keys would override it).
    """
    own = set(credentials.identity_secrets(platform))
    return [
        name
        for name in dict.fromkeys(names)
        if credentials.kind_of(name) == "secret" and name not in own and credentials.is_present(name)
    ]


_REFERENCE_FORMS = (
    re.compile(re.escape(templates.APP_NAME) + r"/([A-Z][A-Z0-9_]*)"),  # CodeBuild secrets-manager, Secrets Manager ids
    re.compile(r"credentials\(['\"]([A-Z][A-Z0-9_]*)['\"]\)"),  # Jenkins
    re.compile(r"secrets/([A-Z][A-Z0-9_]*)/versions"),  # Cloud Build availableSecrets
    re.compile(r"\$\(([A-Z][A-Z0-9_]*)\)"),  # Azure Pipelines variable groups
)


def referenced_secrets(items: list) -> list[str]:
    """Secret names a set of suite files binds by reference (artifacts or stored records).

    Only an explicit platform binding counts (``APP/NAME``, ``credentials('NAME')``,
    ``secrets/NAME/versions``, ``$(NAME)``); an env manifest's ``NAME=`` line does not. This
    is what secret injection copies into the platform's store: exactly what the files use.
    """
    found: set[str] = set()
    for item in items:
        content = str(item.get("content") if isinstance(item, dict) else getattr(item, "content", "") or "")
        for pattern in _REFERENCE_FORMS:
            found.update(name for name in pattern.findall(content) if credentials.kind_of(name) == "secret")
    return sorted(found)


# The record kinds a platform resolves itself, per platform: only their declared bindings count
# for injection. An env manifest's ``NAME=`` line or a GitHub workflow's ``secrets.NAME`` is not a
# binding this platform resolves (live run 9 injected the AWS key pair and a session token
# because generated stages declared them in `.env.dev.example` and `gen-deploy_dev.yml`).
PLATFORM_BOUND_KINDS = {
    "aws": ("buildspec", "cloudformation", "codebuild_project"),
    "jenkins": ("jenkinsfile", "jenkins_job"),
    "gcp": ("cloudbuild",),
    "azure": ("azure_pipeline",),
    "github": ("github_workflow",),
}


def bound_secret_names(records: list, platform: str = "") -> list[str]:
    """Every secret name the run's platform files bind by reference: explicit bindings in any
    file's content plus the names a platform-resolved record declares (the ECS task's Secrets).
    Never the platform's own identity secrets."""
    names = set(referenced_secrets(records))
    kinds = PLATFORM_BOUND_KINDS.get(platform) if platform else None  # unknown platform: every declaration counts
    for record in records:
        if kinds is not None:
            kind = str(record.get("kind") if isinstance(record, dict) else getattr(record, "kind", "") or "")
            if kind not in kinds:
                continue
        declared = record.get("secrets_referenced") if isinstance(record, dict) else getattr(record, "secrets_referenced", None)
        names.update(str(name) for name in (declared or []))
    hidden = set(credentials.identity_secrets(platform))
    return sorted(name for name in names if credentials.kind_of(name) == "secret" and name not in hidden)


def _names_for_stage(stage_name: str, placeholders: list[str]) -> list[str]:
    """The placeholders minus the scanner names of tools this stage does not run."""
    mine = {name for tool in SCANNER_TOOLS.get(stage_name, ()) for name in scanners.env_names(tool)}
    others = {name for tools in SCANNER_TOOLS.values() for tool in tools for name in scanners.env_names(tool)} - mine
    return [name for name in placeholders if name not in others]


def _secrets_manager_lines(names: list[str]) -> str:
    if not names:
        return "  # secrets-manager references go here by NAME when a stage needs one"
    lines = ["  secrets-manager:"]
    lines.extend(f"    {name}: \"{templates.APP_NAME}/{name}\"" for name in names)
    return "\n".join(lines)


def _container_env_lines(names: list[str]) -> str:
    config_names = [name for name in names if credentials.kind_of(name) != "secret"][:8]
    return "\n".join(f"            - {{Name: {name}, Value: \"${name}\"}}" for name in config_names)


# The application's own secrets the deployed task reads from Secrets Manager by name
# (``<app>/NAME``): injected before the first build when present on the orchestrator host.
APP_TASK_SECRETS = ("OPENAI_API_KEY", "TYPESAFE_API_KEY", "LANGSMITH_API_KEY")


def _container_secret_lines(platform: str) -> str:
    """The task definition's ``Secrets`` block for the app secrets that can be injected, or ""."""
    names = injectable_secrets(list(APP_TASK_SECRETS), platform)
    if not names:
        return ""
    lines = ["          Secrets:"]
    lines.extend(f"            - {{Name: {name}, ValueFrom: \"{templates.APP_NAME}/{name}\"}}" for name in names)
    return "\n".join(lines)


# --- Platform contract files (Sprint 4, after live run 8) -----------------------------------
# The orchestrator's own scripts for the fixed stages travel with every suite: the platform-
# neutral ones on every platform, the AWS ones (ECR, CloudFormation, ECS) on AWS. A stage's
# file calls `bash ci/<stage>.sh`; Astra adds steps around it. ``with_manifest`` re-asserts the
# files after generation, so a run never depends on the model reproducing them.
CONTRACT_STAGE_KEYS = {"build": "build", "test": "test", "scanners": "scanners", "package": "package",
                       "container": "container", "release": "release", "deploy": "deploy", "dast": "dast"}


def contract_keys(platform: str) -> list[str]:
    keys = list(templates.CONTRACT_PLATFORM_NEUTRAL)
    if platform == "aws":
        keys.extend(templates.CONTRACT_AWS)
    return keys


def contract_files(platform: str) -> dict[str, str]:
    """path -> content of every contract script this platform's suite carries."""
    return {templates.contract_path(key): templates.contract_script(key) for key in contract_keys(platform)}


def contract_command(stage_name: str, platform: str) -> str | None:
    """The command a stage file runs when the orchestrator has a contract script for it."""
    key = CONTRACT_STAGE_KEYS.get(stage_name)
    if key and key in contract_keys(platform):
        return f"bash {templates.contract_path(key)}"
    return None


def render_skeletons(platform: str, stages: list[dict], environment: dict, *, run_id: str, placeholders: list[str], answers: dict | None = None) -> dict[str, str]:
    """Suite skeletons keyed by artifact path (not by kind: CodeBuild has one buildspec per stage)."""
    names = [stage["name"] for stage in stages]
    base_stage = {"name": SUITE_STAGE, "family": "custom", "purpose": "the whole pipeline", "environment": environment}
    skeletons: dict[str, str] = {}
    branch = os.getenv("GITHUB_REF_NAME", "").strip() or "main"
    if platform == "jenkins":
        blocks = "\n".join(_jenkins_block(stage, answers) for stage in stages)
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
                    "STAGE_COMMANDS": "\n".join(f"      - {line}" for line in command_hints(name, platform, answers)),
                    "WORKFLOW_FILE": WORKFLOW_FOR_STAGE.get(name, f"{name}.yml"),
                    "SECRETS_MANAGER_LINES": _secrets_manager_lines(injectable_secrets(_names_for_stage(stage["name"], placeholders), platform)),
                },
                template=templates.SUITE_TEMPLATES["buildspec"],
            )
        skeletons["deploy/codebuild-project.json"] = templates.render_template(
            "codebuild_project", base_stage, environment, runner="aws_codebuild", run_id=run_id, placeholders=placeholders,
        )
        # The stack and the deploy, smoke, and container scripts travel with every AWS suite: a
        # generated deploy_* or smoke stage relies on them as much as the fixed deploy stage does.
        skeletons["deploy/ecs-stack.yaml"] = templates.render_template(
            "cloudformation", base_stage, environment, runner="aws_codebuild", run_id=run_id, placeholders=placeholders,
            extra={"CONTAINER_ENV_LINES": _container_env_lines(placeholders), "CONTAINER_SECRET_LINES": _container_secret_lines(platform)},
        )
    elif platform == "gcp":
        blocks = []
        previous = "refuse-production"
        for stage in stages:
            blocks.append(_cloudbuild_block(stage, previous, answers))
            previous = stage["name"]
        skeletons["ci/cloudbuild.yaml"] = templates.render_template(
            "cloudbuild", base_stage, environment, runner="gcp_cloudbuild", run_id=run_id, placeholders=placeholders,
            extra={"STAGE_BLOCKS": "\n".join(blocks)}, template=templates.SUITE_TEMPLATES["cloudbuild"],
        )
    elif platform == "azure":
        blocks = []
        previous = "refuse_production"
        for stage in stages:
            blocks.append(_azure_block(stage, previous, answers))
            previous = stage["name"]
        skeletons["ci/azure-pipelines.yml"] = templates.render_template(
            "azure_pipeline", base_stage, environment, runner="azure_pipelines", run_id=run_id, placeholders=placeholders,
            extra={"STAGE_BLOCKS": "\n".join(blocks)}, template=templates.SUITE_TEMPLATES["azure_pipeline"],
        )
    if platform in SUITE_PLATFORMS:
        # The orchestrator's contract scripts for the fixed stages travel with every suite.
        skeletons.update(contract_files(platform))
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
    # The platform's identity secrets are never placeholders inside the platform (live run 9).
    hidden = set(credentials.identity_secrets(platform))
    names = [name for name in provider.placeholders() if name not in hidden]
    for stage_name in (stage["name"] for stage in stages):
        for tool in SCANNER_TOOLS.get(stage_name, ()):
            for extra_name in scanners.env_names(tool):
                if extra_name in credentials.all_names() and extra_name not in names:
                    names.append(extra_name)
    answers = dict(state.get("answers") or {})
    skeletons = render_skeletons(platform, stages, environment, run_id=run_id, placeholders=names, answers=answers)
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
        "available_secrets": injectable_secrets(names, platform),
        "placeholders": names,
        "scanners": {
            tool: {"docker_image": (scanners.entry(tool) or {}).get("docker_image", ""), "env": (scanners.entry(tool) or {}).get("env", [])}
            for stage in stages
            for tool in SCANNER_TOOLS.get(stage["name"], ())
        },
        "answers": answers,
        # Sprint 4: scans and tests are a menu. The selection says which providers the person
        # chose; the catalog says how each one runs on this platform's agent (names only).
        "scan_selection": scanners.selection_from_answers(answers),
        "scanner_catalog": scanners.catalog_for_prompt(),
        "readiness": {"runner": provider.runner, "mode": runner_status.get("mode") or "plan", "missing": list(runner_status.get("missing") or [])},
        "policy_rules": policy.rules_summary(),
        "policy_findings": list(policy_findings or []),
    }


_PRIVILEGED_OFF = re.compile(r'"privilegedMode"\s*:\s*false')
CODEBUILD_DESCRIPTION_MAX = 255  # CreateProject refuses a longer description (live run 3)


_SECRETS_BLOCK = re.compile(r"^([ \t]*)secrets-manager:[ \t]*$")
_BARE_SECRET_ID = re.compile(r"^([ \t]*)([A-Z][A-Z0-9_]*):[ \t]*[\"']?([A-Z][A-Z0-9_]*)[\"']?[ \t]*$")


def _qualify_secret_ids(content: str) -> str:
    """Inside a buildspec's ``secrets-manager`` block, a bare id that is a registry secret name
    (``SNYK_TOKEN: "SNYK_TOKEN"``) becomes the id the orchestrator injects
    (``SNYK_TOKEN: "langgraph-support-app/SNYK_TOKEN"``), so the build resolves what was
    copied. Live run 5's suite bound the bare name."""
    out: list[str] = []
    block_indent: int | None = None
    for line in content.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        indent = len(stripped) - len(stripped.lstrip(" \t"))
        if block_indent is not None and stripped.strip() and indent <= block_indent:
            block_indent = None
        if block_indent is None:
            start = _SECRETS_BLOCK.match(stripped)
            if start:
                block_indent = len(start.group(1))
            out.append(line)
            continue
        match = _BARE_SECRET_ID.match(stripped)
        if match and credentials.kind_of(match.group(3)) == "secret":
            out.append(f'{match.group(1)}{match.group(2)}: "{templates.APP_NAME}/{match.group(3)}"' + ("\n" if line.endswith("\n") else ""))
        else:
            out.append(line)
    return "".join(out)


def _harden(item):
    """The suite's files through the shared fixes, plus the CodeBuild project document:
    ``privilegedMode`` stays true because the image stages run docker inside CodeBuild (live
    run 3 was rejected at the gate for a suite that turned it off), and the description is
    cut to CodeBuild's limit (the same run's provisioning failed on 268 characters). A
    buildspec's bare secret ids are qualified with the app prefix (live run 5)."""
    item = generator.harden(item)
    if item.kind == "buildspec" and "secrets-manager" in item.content:
        qualified = _qualify_secret_ids(item.content)
        if qualified != item.content:
            item = item.model_copy(update={"content": qualified})
    if not item.path.endswith("codebuild-project.json"):
        return item
    content = _PRIVILEGED_OFF.sub('"privilegedMode": true', item.content)
    try:
        document = json.loads(content)
    except json.JSONDecodeError:
        document = None
    if isinstance(document, dict) and len(str(document.get("description") or "")) > CODEBUILD_DESCRIPTION_MAX:
        document["description"] = str(document["description"])[: CODEBUILD_DESCRIPTION_MAX - 3].rstrip() + "..."
        content = json.dumps(document, indent=2) + "\n"
    return item if content == item.content else item.model_copy(update={"content": content})


def with_manifest(suite_obj: PipelineSuite, environment: dict, placeholders: list[str], *, platform: str = "", stages: list[dict] | None = None, answers: dict | None = None) -> PipelineSuite:
    """Append the env manifest (names only) unless Astra already wrote one; relocate a file
    written outside the allowed prefixes; harden every file; re-assert the orchestrator's
    contract files and make sure every fixed stage's file runs its contract script."""
    suite_obj.artifacts = [_harden(generator.relocate(item, SUITE_STAGE)) for item in suite_obj.artifacts]
    if platform in SUITE_PLATFORMS:
        assert_contract(suite_obj, platform, stages or [], environment, placeholders, answers)
    if not any(item.kind == "env_example" for item in suite_obj.artifacts):
        suite_obj.artifacts.append(generator.env_manifest({"name": SUITE_STAGE, "environment": environment}, placeholders))
    return suite_obj


def assert_contract(suite_obj: PipelineSuite, platform: str, stages: list[dict], environment: dict, placeholders: list[str], answers: dict | None = None) -> list[str]:
    """The contract scripts (and on AWS the stack) are the orchestrator's: put back any the
    model changed or dropped, and make sure each fixed stage's file calls its script. Returns
    what was restored (paths) or appended (``<path>: <command>``)."""
    restored: list[str] = []
    wanted = dict(contract_files(platform))
    if platform == "aws":
        base_stage = {"name": SUITE_STAGE, "family": "custom", "purpose": "the whole pipeline", "environment": environment}
        wanted["deploy/ecs-stack.yaml"] = templates.render_template(
            "cloudformation", base_stage, environment, runner="aws_codebuild", run_id=str(environment.get("run_id") or "local"),
            placeholders=placeholders,
            extra={"CONTAINER_ENV_LINES": _container_env_lines(placeholders), "CONTAINER_SECRET_LINES": _container_secret_lines(platform)},
        )
    by_path = {item.path: item for item in suite_obj.artifacts}
    for path, content in wanted.items():
        kind = "cloudformation" if path.endswith(".yaml") else "shell"
        current = by_path.get(path)
        if current is None:
            suite_obj.artifacts.append(GeneratedArtifact(path=path, kind=kind, purpose=f"orchestrator contract: {path}", content=content, secrets_referenced=[]))
            restored.append(path)
        elif current.content != content:
            current.content = content
            current.kind = kind
            restored.append(path)
    if platform == "aws":
        for stage in stages:
            command = contract_command(stage["name"], platform)
            if not command:
                continue
            spec = by_path.get(f"ci/buildspec-{stage['name']}.yml")
            if spec is not None and command not in spec.content:
                command = selection_prefix(stage["name"], answers) + command
                spec.content = _with_build_command(spec.content, command)
                restored.append(f"{spec.path}: {command}")
    return restored


def _with_build_command(content: str, command: str) -> str:
    """Add ``command`` to a buildspec's build phase (the model dropped the contract call)."""
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError:
        document = None
    if not isinstance(document, dict):
        return content.rstrip("\n") + f"\n# orchestrator contract\n# phases.build.commands must run: {command}\n"
    phases = document.setdefault("phases", {}) if isinstance(document.get("phases"), dict) or "phases" not in document else {}
    build = phases.setdefault("build", {}) if isinstance(phases.get("build"), dict) or "build" not in phases else {}
    commands = build.get("commands")
    if not isinstance(commands, list):
        commands = []
    commands.append(command)
    build["commands"] = commands
    phases["build"] = build
    document["phases"] = phases
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


def check(suite: PipelineSuite) -> PolicyResult:
    """Every artifact through the text and path rules (the suite may carry more than six files),
    plus the placeholder check: a skeleton returned unchanged is an error."""
    findings = []
    for artifact in suite.artifacts:
        findings.extend(policy.check_artifact(artifact).findings)
        findings.extend(policy.placeholder_findings(artifact))
    return PolicyResult(ok=not any(item.severity == "error" for item in findings), findings=findings)


def as_artifact_set(suite: PipelineSuite) -> SimpleNamespace:
    """A ``generator.write``-compatible object (duck-typed: ``.artifacts`` and ``.stage``).

    Each artifact's ``secrets_referenced`` is what its own text binds, not what the model or
    the manifest listed: injection then copies exactly the names the stage files use.
    """
    artifacts = [artifact.model_copy(update={"secrets_referenced": referenced_secrets([artifact])}) for artifact in suite.artifacts]
    return SimpleNamespace(stage=SUITE_STAGE, artifacts=artifacts, notes=suite.notes)


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


def provision_bundle(generated: dict[str, list[dict]]) -> list[dict]:
    """Every generated record of the run, suite first, one per path.

    This is what travels with the source upload (``provider.provision(..., bundle=...)``) so a
    platform build finds the scripts a generated stage references (``ci/<stage>.sh``). The
    first live AWS run (2026-11-03) packed only the suite, which would have left every
    generated stage's build without its script.
    """
    names = [SUITE_STAGE, *sorted(name for name in generated if name != SUITE_STAGE)]
    seen: set[str] = set()
    bundle: list[dict] = []
    for name in names:
        for record in generated.get(name) or []:
            path = str(record.get("path") or "")
            if not path or path in seen:
                continue
            seen.add(path)
            bundle.append(dict(record))
    return bundle


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
    fixed = suite_stages(state.get("pipeline") or {})
    suite = planner.generate_suite(context)
    suite = with_manifest(suite, context["environment"], context["placeholders"], platform=platform, stages=fixed, answers=context["answers"])
    result = check(suite)
    revisions_left = max_revisions
    verdict: ArtifactReview = review(result, revisions_left, [item.path for item in suite.artifacts])
    if verdict.verdict == "revise" and revisions_left > 0:
        revisions_left -= 1
        try:
            revised = planner.generate_suite(context_for(state, provider, policy_findings=[item.model_dump() for item in result.findings]))
            suite = with_manifest(revised, context["environment"], context["placeholders"], platform=platform, stages=fixed, answers=context["answers"])
        except AstraQuotaError:
            raise
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
