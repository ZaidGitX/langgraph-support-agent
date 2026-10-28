"""Kubernetes runner: kubectl apply with a client-side dry run in plan mode."""

from __future__ import annotations

import shutil
from pathlib import Path

from ..runner import run_commands
from .base import BaseProvider, StageRun, artifact_of_kind, expand_env, plan_result

APP = "langgraph-support-app"


class KubernetesProvider(BaseProvider):
    runner = "kubernetes"
    platform = "kubernetes"
    cli = "kubectl"
    credential_group = "kubernetes"
    artifact_kinds = ("kubernetes_manifest",)
    timeout = 600

    def _manifest(self, stage: dict, records: list[dict], cwd: str) -> str:
        return artifact_of_kind(records, "kubernetes_manifest", cwd=cwd) or f"deploy/k8s/{stage.get('name')}.yaml"

    def would_run(self, stage: dict, records: list[dict], cwd: str, answers: dict) -> list[list[str]]:
        manifest = self._manifest(stage, records, cwd)
        return [
            ["kubectl", "apply", "-f", manifest, "-n", "$K8S_NAMESPACE"],
            ["kubectl", "rollout", "status", f"deployment/{APP}", "-n", "$K8S_NAMESPACE", "--timeout=180s"],
        ]

    def dry_run(self, stage: dict, records: list[dict], cwd: str) -> list[str]:
        return ["kubectl", "apply", "--dry-run=client", "-f", self._manifest(stage, records, cwd)]

    def execute(self, stage: dict, records: list[dict], cwd: str, *, mode: str, answers: dict) -> StageRun:
        status = self.detect()
        argvs = self.would_run(stage, records, cwd, answers or {})
        if mode != "apply" or status.mode != "apply":
            plan = plan_result(self.runner, status.missing, argvs, records)
            manifest = self._manifest(stage, records, cwd)
            manifest_path = Path(manifest) if Path(manifest).is_absolute() else Path(cwd) / manifest
            if shutil.which("kubectl") and manifest_path.is_file():
                # A client-side dry run validates the manifest locally; it is allowed in plan mode.
                ran = run_commands([{"argv": self.dry_run(stage, records, cwd), "timeout": 60, "label": "kubectl dry-run"}], cwd)
                plan.plan["dry_run"] = {"exit_code": ran["exit_code"], "evidence": ran["evidence"][:1000]}
                plan.evidence += f"\nclient dry-run exit {ran['exit_code']}"
                plan.commands.extend(ran["commands"])
            return plan
        return self.apply(stage, [expand_env(argv) for argv in argvs], cwd, records, answers or {})
