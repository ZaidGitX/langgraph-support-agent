"""AWS runner: CodeBuild for build/test/scan stages; ECS, CloudFormation, S3, and ECR helpers
for deploy-family stages. Every argv names config variables (``$AWS_REGION``); secrets are
read by the CLI from the environment and never appear on a command line."""

from __future__ import annotations

import json
import subprocess
import time
import zipfile
from pathlib import Path

from ..runner import redact
from ..templates import APP_NAME as APP
from .base import (
    BaseProvider,
    StageRun,
    artifact_for_stage,
    artifact_of_kind,
    expand_env,
    run_id_of,
    stored_path,
)

APP_IMAGE = "langgraph-support-app"
POLL_SECONDS = 15


class AwsProvider(BaseProvider):
    runner = "aws_codebuild"
    platform = "aws"
    cli = "aws"
    credential_group = "aws"
    artifact_kinds = ("buildspec", "shell")
    timeout = 2400

    def would_run(self, stage: dict, records: list[dict], cwd: str, answers: dict) -> list[list[str]]:
        provider = str(stage.get("provider") or "")
        run_id = run_id_of(stage, answers)
        region = ["--region", "$AWS_REGION"]
        image = f"$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$AWS_ECR_REPOSITORY:{run_id}"
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
        buildspec = artifact_for_stage(records, str(stage.get("name") or ""), "buildspec", cwd=cwd) or artifact_of_kind(records, "buildspec", cwd=cwd)
        command = ["aws", "codebuild", "start-build", "--project-name", "$AWS_CODEBUILD_PROJECT", *region]
        if buildspec:
            command.extend(["--buildspec-override", f"file://{buildspec}"])
        command.extend(["--environment-variables-override", f"name=RUN_ID,value={run_id},type=PLAINTEXT"])
        return [command]

    def apply(self, stage: dict, argvs: list[list[str]], cwd: str, records: list[dict], answers: dict) -> StageRun:
        if not argvs or argvs[0][:3] != ["aws", "codebuild", "start-build"]:
            return super().apply(stage, argvs, cwd, records, answers)
        return self._codebuild(stage, expand_env(argvs[0]), cwd)

    # --- Sprint 4 phase 9: provisioning and secret injection ---------------------------------

    injection_target = "AWS Secrets Manager (APP_NAME/NAME)"

    def provision_commands(self, records: list[dict], cwd: str, answers: dict) -> list[list[str]]:
        """Upload the checkout as the CodeBuild source, create the ECR repository and the project.

        The project document is the generated ``deploy/codebuild-project.json`` with its config
        placeholders expanded (names such as the bucket and the service role, never a secret).
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
    def _source_zip(cwd: str, run_id: str) -> str:
        return str(Path(cwd) / "var" / "generated" / str(run_id) / "pipeline" / "source.zip")

    @staticmethod
    def _rendered_path(stored: str, cwd: str) -> str:
        return str(Path(stored).with_name(Path(stored).stem + ".rendered.json"))

    def prepare_provision(self, records: list[dict], cwd: str, answers: dict) -> str:
        """Zip the checkout (without var, node_modules, .git) and expand the project document."""
        run_id = (answers or {}).get("run_id") or "local"
        zip_path = Path(self._source_zip(cwd, run_id))
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        root = Path(cwd)
        skip = {"var", "node_modules", ".git", "dist", "__pycache__", ".venv"}
        count = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in root.rglob("*"):
                if path.is_dir() or any(part in skip for part in path.relative_to(root).parts):
                    continue
                archive.write(path, path.relative_to(root).as_posix())
                count += 1
            # The generated suite travels with the source so CodeBuild finds the buildspecs.
            for record in records:
                stored = stored_path(record, cwd)
                relative = str(record.get("path") or "")
                if stored and relative and Path(stored).is_file():
                    archive.write(stored, relative)
        project_doc = artifact_of_kind(records, "codebuild_project", cwd=cwd)
        if project_doc:
            rendered = Path(self._rendered_path(project_doc, cwd))
            rendered.write_text(expand_env([Path(project_doc).read_text(encoding="utf-8")])[0], encoding="utf-8")
        return f"source.zip holds {count} files plus the generated suite"

    def inject_commands(self, names: list[str], answers: dict) -> list[list[str]]:
        region = ["--region", "$AWS_REGION"]
        return [
            ["aws", "secretsmanager", "put-secret-value", "--secret-id", f"{APP}/{name}", "--secret-string", "file:///dev/stdin", *region]
            for name in names
        ]

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
        passed = status == "SUCCEEDED"
        evidence = redact(f"codebuild {build_id} status {status}\nphases: {', '.join(phases)}", limit=4000)
        return StageRun(
            status="pass" if passed else "fail",
            exit_code=0 if passed else 1,
            blocked=False,
            commands=[{"label": label, "argv": argv, "exit_code": 0 if passed else 1, "evidence": evidence}],
            checks=[{"name": label, "status": "pass" if passed else "fail", "job": ""}],
            evidence=evidence,
            plan={},
        )
