"""GitHub Actions runner for generated workflows.

A generated workflow is dispatched from the branch ``orchestrator/generated/<run_id>`` where
``generator.publish_branch`` (phase 5) commits it; ``gh workflow run`` needs the file to exist
on that ref. The fixed stages keep using ``dispatch.run_workflow`` directly.
"""

from __future__ import annotations

import os

from .base import (
    BaseProvider,
    StageRun,
    from_run_commands,
    plan_result,
    run_id_of,
    tier_of,
)


class GithubActionsProvider(BaseProvider):
    runner = "github_actions"
    platform = "github"
    cli = "gh"
    credential_group = "github"
    artifact_kinds = ("github_workflow",)

    @staticmethod
    def branch_for(run_id: str) -> str:
        return f"orchestrator/generated/{run_id}"

    @staticmethod
    def workflow_file(stage: dict) -> str:
        return f"gen-{stage.get('name')}.yml"

    def would_run(self, stage: dict, records: list[dict], cwd: str, answers: dict) -> list[list[str]]:
        run_id = run_id_of(stage, answers)
        command = [
            "gh", "workflow", "run", self.workflow_file(stage),
            "--ref", self.branch_for(run_id),
            "-f", f"run_id={run_id}", "-f", f"tier={tier_of(stage, answers)}",
        ]
        repo = os.getenv("GITHUB_REPOSITORY", "").strip()
        if repo:
            command.extend(["--repo", repo])
        return [command]

    def execute(self, stage: dict, records: list[dict], cwd: str, *, mode: str, answers: dict) -> StageRun:
        status = self.detect()
        argvs = self.would_run(stage, records, cwd, answers or {})
        run_id = run_id_of(stage, answers)
        if mode != "apply" or status.mode != "apply":
            note = (
                f"planned: would publish {self.workflow_file(stage)} on branch {self.branch_for(run_id)} "
                f"and run: {' '.join(argvs[0])}"
                + (f"; missing {', '.join(status.missing)}" if status.missing else "")
            )
            return plan_result(self.runner, status.missing, argvs, records, note=note)
        from .. import dispatch

        ran = dispatch.run_workflow(
            str(stage.get("name")),
            answers or {},
            os.getenv("APP_PUBLIC_URL", "").strip(),
            workflow_file=self.workflow_file(stage),
            ref=self.branch_for(run_id),
            inputs={"run_id": run_id, "tier": tier_of(stage, answers)},
        )
        return from_run_commands(ran)
