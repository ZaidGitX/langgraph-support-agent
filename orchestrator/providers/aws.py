"""AWS runner: CodeBuild for build/test/scan stages; ECS, CloudFormation, S3, and ECR helpers
for deploy-family stages. Every argv names config variables (``$AWS_REGION``); secrets are
read by the CLI from the environment and never appear on a command line."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import zipfile
from pathlib import Path

from ..runner import redact, run_commands
from ..templates import APP_NAME as APP
from ..templates import CODEBUILD_ENV_NAMES
from .base import (
    BaseProvider,
    ProvisionResult,
    StageRun,
    artifact_for_stage,
    artifact_of_kind,
    expand_env,
    run_id_of,
    stored_path,
    tier_of,
)

APP_IMAGE = "langgraph-support-app"
POLL_SECONDS = 10
ROLE_SUFFIX = "-codebuild-role"
PROJECT_RETRIES = 6  # a role created seconds ago takes a moment before CodeBuild may assume it
LOG_TAIL_LINES = 60  # CloudWatch lines fetched for a failed build's evidence (live run 8)
_UNRESOLVED = re.compile(r"^\$\{?[A-Z][A-Z0-9_]*\}?$")


def build_env_override(run_id: str, tier: str = "dev") -> str:
    """The ``start-build`` environment override every stage carries: RUN_ID, TIER, PLATFORM,
    APP_NAME, and each AWS config name the contracts read, as placeholders the apply expands
    from the host environment (``ensure_platform`` exports them before the first build).

    Live run 18: the project document is rendered from what the model wrote and the floored
    buildspecs declare only RUN_ID, TIER, PLATFORM, and APP_NAME, so a name missing from the
    project reached ``ci/container.sh`` unset. JSON, not the shorthand form: AWS_SUBNET_IDS holds
    commas the shorthand parser would split on.
    """
    entries = [{"name": "RUN_ID", "value": run_id, "type": "PLAINTEXT"}, {"name": "PLATFORM", "value": "aws", "type": "PLAINTEXT"}]
    for name in CODEBUILD_ENV_NAMES:
        if name == "APP_NAME":
            value = APP
        elif name == "TIER":
            value = tier
        else:
            value = f"${name}"
        entries.append({"name": name, "value": value, "type": "PLAINTEXT"})
    return json.dumps(entries, separators=(",", ":"))


def resolve_build_env(argv: list[str]) -> list[str]:
    """After ``expand_env``: drop an override entry whose value is still a placeholder or empty,
    so the build sees the project's value or nothing, never the literal ``$AWS_VPC_ID``."""
    if "--environment-variables-override" not in argv:
        return argv
    position = argv.index("--environment-variables-override") + 1
    if position >= len(argv):
        return argv
    try:
        entries = json.loads(argv[position])
    except json.JSONDecodeError:
        return argv
    if not isinstance(entries, list):
        return argv
    kept = [
        entry for entry in entries
        if isinstance(entry, dict) and str(entry.get("value") or "").strip() and not _UNRESOLVED.match(str(entry.get("value")).strip())
    ]
    resolved = list(argv)
    resolved[position] = json.dumps(kept, separators=(",", ":"))
    return resolved


_CREDENTIAL_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".kdbx")


def _holds_credentials(name: str) -> bool:
    """Files that never travel in the source upload: .env and its variants (not the examples),
    private keys, credential stores."""
    lower = name.lower()
    if lower == ".env" or (lower.startswith(".env.") and not lower.endswith(".example")):
        return True
    if lower in {"credentials", "id_rsa", "id_ed25519", "id_ecdsa", "secrets.enc"}:
        return True
    return lower.endswith(_CREDENTIAL_SUFFIXES)


def trust_policy() -> dict:
    return {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "codebuild.amazonaws.com"}, "Action": "sts:AssumeRole"}]}


def role_policy(account: str, region: str, *, bucket: str, repository: str, project: str) -> dict:
    """The scoped service-role policy for the orchestrator's CodeBuild project: its logs, the
    source bucket, the image repository, the app's secrets, and what a Fargate deploy of the
    generated stack needs (stacks, clusters, services, task roles, and log groups whose names
    start with the app name, plus the EC2 describe and security-group calls). Nothing else;
    ``scripts/aws_bootstrap.sh`` prints the same document for a manual bootstrap."""
    app = APP
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
             "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/codebuild/{project}*"},
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:ListBucket"],
             "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]},
            # A buildspec's `reports:` section (pytest JUnit, coverage) creates a report group named
            # after the project; live run 8's test stage passed every phase and failed on this.
            {"Effect": "Allow", "Action": ["codebuild:CreateReportGroup", "codebuild:CreateReport", "codebuild:UpdateReport", "codebuild:BatchPutTestCases", "codebuild:BatchPutCodeCoverages"],
             "Resource": f"arn:aws:codebuild:{region}:{account}:report-group/{project}-*"},
            {"Effect": "Allow", "Action": ["ecr:GetAuthorizationToken"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["ecr:BatchCheckLayerAvailability", "ecr:CompleteLayerUpload", "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
                                           "ecr:DescribeImages", "ecr:ListImages", "ecr:DescribeRepositories", "ecr:BatchDeleteImage"],
             "Resource": f"arn:aws:ecr:{region}:{account}:repository/{repository}"},
            {"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"], "Resource": f"arn:aws:secretsmanager:{region}:{account}:secret:{app}/*"},
            {"Effect": "Allow", "Action": ["cloudformation:*"], "Resource": f"arn:aws:cloudformation:{region}:{account}:stack/{app}-*/*"},
            {"Effect": "Allow", "Action": ["cloudformation:ValidateTemplate", "cloudformation:ListStacks", "cloudformation:DescribeStacks"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["ecs:*"],
             "Resource": [f"arn:aws:ecs:{region}:{account}:cluster/{app}-*", f"arn:aws:ecs:{region}:{account}:service/{app}-*/*",
                          f"arn:aws:ecs:{region}:{account}:task-definition/{app}-*:*", f"arn:aws:ecs:{region}:{account}:task/{app}-*/*"]},
            {"Effect": "Allow", "Action": ["ecs:RegisterTaskDefinition", "ecs:DeregisterTaskDefinition", "ecs:DescribeTaskDefinition", "ecs:ListTaskDefinitions", "ecs:ListClusters", "ecs:DescribeClusters", "ecs:DescribeServices", "ecs:ListServices", "ecs:ListTasks", "ecs:DescribeTasks", "ecs:TagResource"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["ec2:Describe*", "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup", "ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress", "ec2:CreateTags"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy", "logs:TagResource", "logs:DescribeLogGroups"],
             "Resource": f"arn:aws:logs:{region}:{account}:log-group:/ecs/{app}-*"},
            {"Effect": "Allow", "Action": ["iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:TagRole", "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:PassRole"],
             "Resource": f"arn:aws:iam::{account}:role/{app}-*"},
        ],
    }


class AwsProvider(BaseProvider):
    runner = "aws_codebuild"
    platform = "aws"
    cli = "aws"
    credential_group = "aws"
    artifact_kinds = ("buildspec", "shell")
    timeout = 1800  # the live-run cap: a build still running after 30 minutes is stopped and fails (the project's timeoutInMinutes)

    def would_run(self, stage: dict, records: list[dict], cwd: str, answers: dict) -> list[list[str]]:
        provider = str(stage.get("provider") or "")
        run_id = run_id_of(stage, answers)
        tier = tier_of(stage, answers)
        region = ["--region", "$AWS_REGION"]
        image = f"$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$AWS_ECR_REPOSITORY:{run_id}"
        buildspec = artifact_for_stage(records, str(stage.get("name") or ""), "buildspec", cwd=cwd) or artifact_of_kind(records, "buildspec", cwd=cwd)
        if buildspec:
            # A stage with a buildspec runs inside CodeBuild whatever its provider: live run 8
            # ran infra_plan's CloudFormation command on the orchestrator host, where the
            # generated template path did not exist.
            return [[
                "aws", "codebuild", "start-build", "--project-name", "$AWS_CODEBUILD_PROJECT", *region,
                "--buildspec-override", f"file://{buildspec}",
                "--environment-variables-override", build_env_override(run_id, tier),
            ]]
        if provider == "aws_ecs":
            return [["aws", "ecs", "update-service", "--cluster", "$AWS_ECS_CLUSTER", "--service", "$AWS_ECS_SERVICE", "--force-new-deployment", *region]]
        if provider == "aws_cfn":
            template = artifact_of_kind(records, "kubernetes_manifest", "script", cwd=cwd) or "deploy/stack.yaml"
            return [["aws", "cloudformation", "deploy", "--stack-name", "$AWS_CFN_STACK", "--template-file", template, "--no-execute-changeset", *region]]
        if provider == "aws_s3":
            return [["aws", "s3", "cp", "dist/langgraph-support-app.tgz", f"s3://$AWS_S3_BUCKET/{run_id}/", *region]]
        if provider == "ecr":
            return [
                ["docker", "build", "-t", image, "."],
                ["bash", "-c", "aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"],
                ["docker", "push", image],
            ]
        command = ["aws", "codebuild", "start-build", "--project-name", "$AWS_CODEBUILD_PROJECT", *region]
        command.extend(["--environment-variables-override", build_env_override(run_id, tier)])
        return [command]

    def apply(self, stage: dict, argvs: list[list[str]], cwd: str, records: list[dict], answers: dict) -> StageRun:
        if not argvs or argvs[0][:3] != ["aws", "codebuild", "start-build"]:
            return super().apply(stage, argvs, cwd, records, answers)
        return self._codebuild(stage, resolve_build_env(expand_env(argvs[0])), cwd)

    # --- Sprint 4 phase 9: provisioning and secret injection ---------------------------------

    injection_target = "AWS Secrets Manager (APP_NAME/NAME)"

    def provision_commands(self, records: list[dict], cwd: str, answers: dict) -> list[list[str]]:
        """Upload the checkout as the CodeBuild source, check the ECR repository, create the project.

        The project document is the generated ``deploy/codebuild-project.json`` with its config
        placeholders expanded (names such as the bucket and the service role, never a secret).
        At apply time ``provision`` first creates whatever is missing (``ensure_platform``) and
        turns ``create-project`` into ``update-project`` when the project already exists.
        """
        run_id = str((answers or {}).get("run_id") or "local")
        project_doc = artifact_of_kind(records, "codebuild_project", cwd=cwd)
        if not project_doc:
            return []
        region = ["--region", "$AWS_REGION"]
        rendered = self._rendered_path(project_doc, cwd)
        return [
            ["aws", "s3", "cp", self._source_zip(cwd, run_id), f"s3://$AWS_S3_BUCKET/orchestrator/{run_id}/source.zip", *region],
            ["aws", "ecr", "describe-repositories", "--repository-names", "$AWS_ECR_REPOSITORY", *region],
            ["aws", "codebuild", "create-project", "--cli-input-json", f"file://{rendered}", *region],
        ]

    @staticmethod
    def deploy_defaults(tier: str = "dev") -> dict[str, str]:
        """The deploy names a run uses when the registry leaves them unset: one CloudFormation
        stack, ECS cluster, and ECS service per app and tier (``<app>-<tier>``), the names the
        generated stack and ``scripts/aws_teardown.sh`` agree on. Exported so the project's
        environment and every buildspec see them (live run 8 had none of them set)."""
        name = f"{APP}-{str(tier or 'dev').strip().lower()}"
        defaults = {"AWS_CFN_STACK": name, "AWS_ECS_CLUSTER": name, "AWS_ECS_SERVICE": name}
        for key, value in defaults.items():
            if not os.environ.get(key, "").strip():
                os.environ[key] = value
        return defaults

    def ensure_platform(self, cwd: str, run_id: str, tier: str = "dev") -> dict:
        """Create what the platform run needs and is missing, with the vault credentials: the
        account id and default names, the source bucket, the ECR repository, and the scoped
        CodeBuild service role (Sprint 4, after live run 2: the person runs no script).

        Idempotent. Names come from the registry when set and are derived otherwise
        (``langgraph-orchestrator-<account>``, the app name, ``<app>-orchestrator``); a role the
        person configured under another name is left alone. Returns ``status``, ``evidence``,
        ``created``, ``role_created``.
        """
        from .. import credentials as registry_env

        # An empty AWS_PROFILE or AWS_SESSION_TOKEN from a Compose placeholder makes every aws
        # call fail ("The config profile () could not be found", live run 7): unset means unset.
        registry_env.drop_empty_env()
        self.deploy_defaults(tier)
        region = os.environ.get("AWS_REGION", "").strip()
        if not region:
            return {"status": "fail", "evidence": "AWS_REGION is not set", "created": [], "role_created": False}
        reg = ["--region", region]
        code, identity, error = self._aws_json(["aws", "sts", "get-caller-identity", *reg], cwd)
        account = str((identity or {}).get("Account") or "")
        if code != 0 or not account:
            return {"status": "fail", "evidence": f"sts get-caller-identity failed: {redact(error, limit=300)}", "created": [], "role_created": False}
        os.environ.setdefault("AWS_ACCOUNT_ID", account)
        project = os.environ.get("AWS_CODEBUILD_PROJECT", "").strip() or f"{APP}-orchestrator"
        bucket = os.environ.get("AWS_S3_BUCKET", "").strip() or f"langgraph-orchestrator-{account}"
        repository = os.environ.get("AWS_ECR_REPOSITORY", "").strip() or APP
        os.environ.setdefault("AWS_CODEBUILD_PROJECT", project)
        os.environ.setdefault("AWS_S3_BUCKET", bucket)
        os.environ.setdefault("AWS_ECR_REPOSITORY", repository)
        lines = [f"account {account}, region {region}"]
        created: list[str] = []

        def failed(message: str) -> dict:
            return {"status": "fail", "evidence": "; ".join([*lines, message]), "created": created, "role_created": False}

        code, _payload, _error = self._aws_json(["aws", "s3api", "head-bucket", "--bucket", bucket, *reg], cwd)
        if code != 0:
            argv = ["aws", "s3api", "create-bucket", "--bucket", bucket, *reg]
            if region != "us-east-1":
                argv.extend(["--create-bucket-configuration", f"LocationConstraint={region}"])
            code, _payload, error = self._aws_json(argv, cwd)
            if code != 0:
                return failed(f"create-bucket {bucket} failed: {redact(error, limit=300)}")
            self._aws_json(["aws", "s3api", "put-public-access-block", "--bucket", bucket, *reg, "--public-access-block-configuration",
                            "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"], cwd)
            created.append(f"bucket {bucket}")
        else:
            lines.append(f"bucket {bucket} exists")
        code, _payload, _error = self._aws_json(["aws", "ecr", "describe-repositories", "--repository-names", repository, *reg], cwd)
        if code != 0:
            code, _payload, error = self._aws_json(["aws", "ecr", "create-repository", "--repository-name", repository, "--image-scanning-configuration", "scanOnPush=true", *reg], cwd)
            if code != 0:
                return failed(f"create-repository {repository} failed: {redact(error, limit=300)}")
            created.append(f"repository {repository}")
        else:
            lines.append(f"repository {repository} exists")
        configured = os.environ.get("AWS_CODEBUILD_ROLE_ARN", "").strip()
        role_name = f"{project}{ROLE_SUFFIX}"
        role_created = False
        if configured and configured.rsplit("/", 1)[-1] != role_name:
            lines.append(f"role {configured.rsplit('/', 1)[-1]} was configured by the person and is left as is")
        else:
            folder = Path(cwd) / "var" / "generated" / run_id / "pipeline"
            folder.mkdir(parents=True, exist_ok=True)
            code, payload, _error = self._aws_json(["aws", "iam", "get-role", "--role-name", role_name], cwd)
            if code != 0:
                trust = folder / "codebuild-trust.json"
                trust.write_text(json.dumps(trust_policy(), indent=2), encoding="utf-8")
                code, payload, error = self._aws_json(["aws", "iam", "create-role", "--role-name", role_name, "--assume-role-policy-document", f"file://{trust}",
                                                      "--description", f"CodeBuild service role for the orchestrator ({APP})"], cwd)
                if code != 0:
                    return failed(f"create-role {role_name} failed: {redact(error, limit=300)}")
                created.append(f"role {role_name}")
                role_created = True
            arn = str(((payload or {}).get("Role") or {}).get("Arn") or f"arn:aws:iam::{account}:role/{role_name}")
            policy_path = folder / "codebuild-role-policy.json"
            policy_path.write_text(json.dumps(role_policy(account, region, bucket=bucket, repository=repository, project=project), indent=2), encoding="utf-8")
            code, _payload, error = self._aws_json(["aws", "iam", "put-role-policy", "--role-name", role_name, "--policy-name", f"{role_name}-policy", "--policy-document", f"file://{policy_path}"], cwd)
            if code != 0:
                return failed(f"put-role-policy {role_name} failed: {redact(error, limit=300)}")
            lines.append(f"role {role_name}: policy set (logs, bucket, repository, app secrets, scoped deploy)")
            os.environ["AWS_CODEBUILD_ROLE_ARN"] = arn
        # Networking for the Fargate service: the names the registry carries, else the account's
        # default VPC and its public subnets, exported so the project hands them to every build
        # (ci/deploy.sh repeats the lookup inside CodeBuild when they are still empty).
        network = self.discover_network(cwd, reg)
        if network:
            lines.append(network)
        if created:
            lines.append("created: " + ", ".join(created))
        return {"status": "pass", "evidence": "; ".join(lines), "created": created, "role_created": role_created}

    def discover_network(self, cwd: str, reg: list[str]) -> str:
        """Fill AWS_VPC_ID and AWS_SUBNET_IDS from the default VPC when unset; a one-line note."""
        vpc = os.environ.get("AWS_VPC_ID", "").strip()
        subnets = os.environ.get("AWS_SUBNET_IDS", "").strip()
        if not vpc:
            code, payload, _error = self._aws_json(["aws", "ec2", "describe-vpcs", "--filters", "Name=isDefault,Values=true", *reg], cwd)
            vpcs = (payload or {}).get("Vpcs") or []
            vpc = str(vpcs[0].get("VpcId") or "") if code == 0 and vpcs else ""
            if vpc:
                os.environ["AWS_VPC_ID"] = vpc
        if vpc and not subnets:
            code, payload, _error = self._aws_json(
                ["aws", "ec2", "describe-subnets", "--filters", f"Name=vpc-id,Values={vpc}", "Name=map-public-ip-on-launch,Values=true", *reg], cwd
            )
            found = [str(item.get("SubnetId")) for item in ((payload or {}).get("Subnets") or []) if item.get("SubnetId")] if code == 0 else []
            if not found:
                code, payload, _error = self._aws_json(["aws", "ec2", "describe-subnets", "--filters", f"Name=vpc-id,Values={vpc}", *reg], cwd)
                found = [str(item.get("SubnetId")) for item in ((payload or {}).get("Subnets") or []) if item.get("SubnetId")] if code == 0 else []
            subnets = ",".join(sorted(found))
            if subnets:
                os.environ["AWS_SUBNET_IDS"] = subnets
        if not vpc:
            return "network: no default VPC; set AWS_VPC_ID and AWS_SUBNET_IDS before a deploy stage"
        return f"network: {vpc} with {len(subnets.split(',')) if subnets else 0} subnet(s)"

    def provision(self, records: list[dict], cwd: str, *, mode: str, answers: dict, bundle: list[dict] | None = None) -> ProvisionResult:
        """Apply: bootstrap what is missing, upload the source, then create or update the project.
        Plan: the base listing, with a note that the bootstrap happens on apply."""
        status = self.detect()
        argvs = self.provision_commands(records, cwd, answers or {})
        if mode != "apply" or status.mode != "apply" or not argvs:
            result = super().provision(records, cwd, mode=mode, answers=answers, bundle=bundle)
            if argvs and result.status == "planned":
                result.evidence += "; the source bucket, the image repository, and the service role are created when absent"
            return result
        run_id = str((answers or {}).get("run_id") or "local")
        ensured = self.ensure_platform(cwd, run_id, tier=str((answers or {}).get("env_target") or "dev"))
        if ensured.get("status") != "pass":
            return ProvisionResult(self.runner, "provision", "fail", "apply", argvs, evidence=str(ensured.get("evidence") or ""))
        note = self.prepare_provision(records, cwd, answers or {}, bundle)
        argvs = self.provision_commands(records, cwd, answers or {})
        project_argv, uploads = argvs[-1], argvs[:-1]
        commands = [
            {"argv": expand_env(argv), "timeout": 600, "label": f"{self.runner}:provision:{position + 1}"}
            for position, argv in enumerate(uploads)
        ]
        ran = run_commands(commands, cwd)
        evidence = [str(ensured.get("evidence") or ""), note, str(ran.get("evidence") or "")]
        if ran.get("status") != "pass":
            return ProvisionResult(self.runner, "provision", "fail", "apply", argvs, evidence="\n".join(part for part in evidence if part))
        ok, project_note = self._create_or_update_project(expand_env(project_argv), cwd, wait_for_role=bool(ensured.get("role_created")))
        evidence.append(project_note)
        return ProvisionResult(self.runner, "provision", "pass" if ok else "fail", "apply", argvs, evidence="\n".join(part for part in evidence if part))

    def _create_or_update_project(self, argv: list[str], cwd: str, *, wait_for_role: bool = False) -> tuple[bool, str]:
        """``create-project`` for a new project, ``update-project`` for one that exists; a role
        created moments ago is retried until CodeBuild may assume it."""
        project = os.environ.get("AWS_CODEBUILD_PROJECT", "")
        region = expand_env(["--region", "$AWS_REGION"])
        code, payload, _error = self._aws_json(["aws", "codebuild", "batch-get-projects", "--names", project, *region], cwd)
        exists = bool((payload or {}).get("projects"))
        if exists:
            argv = ["update-project" if part == "create-project" else part for part in argv]
        error = ""
        for attempt in range(PROJECT_RETRIES):
            code, _payload, error = self._aws_json(argv, cwd)
            if code == 0:
                return True, f"codebuild {'update' if exists else 'create'}-project {project}: ok"
            propagating = "assumerole" in error.lower() or "not authorized" in error.lower()
            if not propagating or attempt == PROJECT_RETRIES - 1:
                break
            time.sleep(5 if wait_for_role or attempt else 2)
        return False, f"codebuild project {project}: {redact(error, limit=400)}"

    @staticmethod
    def _source_zip(cwd: str, run_id: str) -> str:
        return str(Path(cwd) / "var" / "generated" / str(run_id) / "pipeline" / "source.zip")

    @staticmethod
    def _rendered_path(stored: str, cwd: str) -> str:
        return str(Path(stored).with_name(Path(stored).stem + ".rendered.json"))

    def prepare_provision(self, records: list[dict], cwd: str, answers: dict, bundle: list[dict] | None = None) -> str:
        """Zip the checkout (without var, node_modules, .git) and expand the project document.

        The suite and every generated stage's files (``bundle``) travel inside the zip at their
        relative paths, so a buildspec override that runs ``ci/<stage>.sh`` finds it.
        """
        run_id = (answers or {}).get("run_id") or "local"
        zip_path = Path(self._source_zip(cwd, run_id))
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        root = Path(cwd)
        skip = {"var", "node_modules", ".git", "dist", "__pycache__", ".venv", ".aws", ".ssh"}
        count = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in root.rglob("*"):
                if path.is_dir() or any(part in skip for part in path.relative_to(root).parts):
                    continue
                if _holds_credentials(path.name):
                    # A laptop checkout has .env and key files; they never travel to the platform.
                    continue
                archive.write(path, path.relative_to(root).as_posix())
                count += 1
            # The suite and the generated stages' files travel with the source so CodeBuild
            # finds the buildspecs and the scripts they run.
            packed: set[str] = set()
            for record in bundle or records:
                stored = stored_path(record, cwd)
                relative = str(record.get("path") or "")
                if stored and relative and relative not in packed and Path(stored).is_file():
                    archive.write(stored, relative)
                    packed.add(relative)
        project_doc = artifact_of_kind(records, "codebuild_project", cwd=cwd)
        if project_doc:
            rendered = Path(self._rendered_path(project_doc, cwd))
            expanded = expand_env([Path(project_doc).read_text(encoding="utf-8")])[0]
            # A config name the account could not supply (no default VPC, say) must reach the
            # build as an empty value, not as the literal "$AWS_VPC_ID" the gap marker leaves.
            expanded = re.sub(r'"\$\{?(AWS_[A-Z0-9_]+)\}?"', '""', expanded)
            rendered.write_text(expanded, encoding="utf-8")
        return f"source.zip holds {count} checkout files plus {len(packed)} generated file(s)"

    def inject_commands(self, names: list[str], answers: dict) -> list[list[str]]:
        region = ["--region", "$AWS_REGION"]
        return [
            ["aws", "secretsmanager", "put-secret-value", "--secret-id", f"{APP}/{name}", "--secret-string", "file:///dev/stdin", *region]
            for name in names
        ]

    def run_injection(self, name: str, argv: list[str], cwd: str) -> bool:
        """``put-secret-value`` needs an existing secret: create ``APP/NAME`` first when absent."""
        secret_id = f"{APP}/{name}"
        region = expand_env(["--region", "$AWS_REGION"])
        code, _payload, _error = self._aws_json(["aws", "secretsmanager", "describe-secret", "--secret-id", secret_id, *region], cwd)
        if code != 0:
            argv = ["aws", "secretsmanager", "create-secret", "--name", secret_id, "--secret-string", "file:///dev/stdin", *region]
        return super().run_injection(name, argv, cwd)

    def _log_tail(self, logs: dict, region: str, cwd: str, limit: int = LOG_TAIL_LINES) -> str:
        """The last lines of a build's CloudWatch log stream, or "" when there is none."""
        group = str(logs.get("groupName") or "")
        stream = str(logs.get("streamName") or "")
        if not group or not stream:
            return ""
        query = ["aws", "logs", "get-log-events", "--log-group-name", group, "--log-stream-name", stream, "--limit", str(limit), "--no-start-from-head"]
        if region:
            query.extend(["--region", region])
        code, payload, _error = self._aws_json(query, cwd, timeout=60)
        if code != 0 or not payload:
            return ""
        lines = [str(event.get("message") or "").rstrip() for event in payload.get("events") or []]
        return "\n".join(line for line in lines if line)[-3000:]

    def _aws_json(self, argv: list[str], cwd: str, timeout: int = 120) -> tuple[int, dict | None, str]:
        try:
            completed = subprocess.run([*argv, "--output", "json"], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, None, f"{type(exc).__name__}: {exc}"
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError:
            payload = None
        return int(completed.returncode), payload, (completed.stderr or "")

    def _codebuild(self, stage: dict, argv: list[str], cwd: str) -> StageRun:
        label = f"aws_codebuild:{stage.get('name')}"
        code, payload, error = self._aws_json(argv, cwd)
        build_id = str(((payload or {}).get("build") or {}).get("id") or "")
        if code != 0 or not build_id:
            evidence = redact(f"start-build exit {code}: {error}", limit=2000)
            return StageRun(status="fail", exit_code=code or 1, blocked=False,
                            commands=[{"label": label, "argv": argv, "exit_code": code or 1, "evidence": evidence}],
                            checks=[{"name": label, "status": "fail", "job": ""}], evidence=evidence, plan={})
        deadline = time.time() + self.timeout
        status = "IN_PROGRESS"
        phases: list[str] = []
        logs: dict = {}
        region = argv[argv.index("--region") + 1] if "--region" in argv else ""
        while time.time() < deadline and status == "IN_PROGRESS":
            time.sleep(POLL_SECONDS)
            query = ["aws", "codebuild", "batch-get-builds", "--ids", build_id]
            if region:
                query.extend(["--region", region])
            code, payload, _error = self._aws_json(query, cwd)
            builds = (payload or {}).get("builds") or []
            if builds:
                status = str(builds[0].get("buildStatus") or "IN_PROGRESS")
                phases = [f"{phase.get('phaseType')}={phase.get('phaseStatus')}" for phase in builds[0].get("phases") or [] if phase.get("phaseStatus")]
                logs = dict(builds[0].get("logs") or {})
        if status == "IN_PROGRESS":
            # Past the cap: stop the build so it cannot bill on, and fail the stage with the reason.
            stop = ["aws", "codebuild", "stop-build", "--id", build_id]
            if region:
                stop.extend(["--region", region])
            self._aws_json(stop, cwd)
            status = f"STOPPED by the orchestrator after {int(self.timeout)}s"
        passed = status == "SUCCEEDED"
        evidence = redact(f"codebuild {build_id} status {status}\nphases: {', '.join(phases)}", limit=4000)
        if not passed:
            # The phases alone say nothing about why (live run 8: "BUILD=FAILED"); the build's
            # CloudWatch log tail does, for the person and for Astra's diagnosis.
            tail = self._log_tail(logs, region, cwd)
            if tail:
                evidence = redact(f"{evidence}\nlog tail:\n{tail}", limit=6000)
        return StageRun(
            status="pass" if passed else "fail",
            exit_code=0 if passed else 1,
            blocked=False,
            commands=[{"label": label, "argv": argv, "exit_code": 0 if passed else 1, "evidence": evidence}],
            checks=[{"name": label, "status": "pass" if passed else "fail", "job": ""}],
            evidence=evidence,
            plan={},
        )
