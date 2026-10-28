"""Azure runner: Azure Pipelines for build/test/scan stages; Container Apps, ACR, and Blob
helpers for deploy-family stages."""

from __future__ import annotations

import os

from .base import BaseProvider, run_id_of

APP = "langgraph-support-app"


class AzureProvider(BaseProvider):
    runner = "azure_pipelines"
    platform = "azure"
    cli = "az"
    credential_group = "azure"
    artifact_kinds = ("azure_pipeline", "shell")
    timeout = 2400

    def would_run(self, stage: dict, records: list[dict], cwd: str, answers: dict) -> list[list[str]]:
        provider = str(stage.get("provider") or "")
        run_id = run_id_of(stage, answers)
        image = f"$AZURE_ACR_NAME.azurecr.io/{APP}:{run_id}"
        if provider == "azure_container_apps":
            return [["az", "containerapp", "update", "--name", "$AZURE_CONTAINER_APP", "--resource-group", "$AZURE_RESOURCE_GROUP", "--image", image]]
        if provider == "acr":
            return [["az", "acr", "build", "--registry", "$AZURE_ACR_NAME", "--image", f"{APP}:{run_id}", "."]]
        if provider == "azure_blob":
            return [["az", "storage", "blob", "upload", "--account-name", "$AZURE_STORAGE_ACCOUNT", "--container-name", "artifacts", "--name", f"{run_id}/langgraph-support-app.tgz", "--file", "dist/langgraph-support-app.tgz"]]
        branch = os.getenv("GITHUB_REF_NAME", "").strip() or "main"
        return [[
            "az", "pipelines", "run", "--id", "$AZURE_PIPELINE_ID", "--branch", branch,
            "--org", "$AZURE_DEVOPS_ORG_URL", "--project", "$AZURE_DEVOPS_PROJECT",
            "--parameters", f"runId={run_id}",
        ]]
