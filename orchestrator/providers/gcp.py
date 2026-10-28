"""Google Cloud runner: Cloud Build for build/test/scan stages; Cloud Run, Artifact Registry,
and Cloud Storage helpers for deploy-family stages."""

from __future__ import annotations

from .base import BaseProvider, artifact_of_kind, run_id_of, tier_of

APP = "langgraph-support-app"


class GcpProvider(BaseProvider):
    runner = "gcp_cloudbuild"
    platform = "gcp"
    cli = "gcloud"
    credential_group = "gcp"
    artifact_kinds = ("cloudbuild", "shell")
    timeout = 2400

    def would_run(self, stage: dict, records: list[dict], cwd: str, answers: dict) -> list[list[str]]:
        provider = str(stage.get("provider") or "")
        run_id = run_id_of(stage, answers)
        image = f"$GCP_REGION-docker.pkg.dev/$GCP_PROJECT_ID/$GCP_ARTIFACT_REPO/{APP}:{run_id}"
        project = ["--project", "$GCP_PROJECT_ID"]
        if provider == "gcp_cloud_run":
            return [["gcloud", "run", "deploy", "$GCP_CLOUD_RUN_SERVICE", "--image", image, "--region", "$GCP_REGION", *project, "--quiet"]]
        if provider == "gcs":
            return [["gcloud", "storage", "cp", "dist/langgraph-support-app.tgz", f"gs://$GCS_BUCKET/{run_id}/", *project]]
        if provider == "artifact_registry":
            return [["gcloud", "builds", "submit", "--tag", image, *project, "."]]
        config = artifact_of_kind(records, "cloudbuild", cwd=cwd) or f"ci/cloudbuild-{stage.get('name')}.yaml"
        return [[
            "gcloud", "builds", "submit", "--config", config, *project,
            "--substitutions", f"_RUN_ID={run_id},_TIER={tier_of(stage, answers)}", ".",
        ]]
