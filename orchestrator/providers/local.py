"""Local runner: the stored script, or the stage's fixed commands, through run_commands."""

from __future__ import annotations

from ..runner import run_commands
from .base import (
    BaseProvider,
    ProviderStatus,
    StageRun,
    from_run_commands,
    stored_script,
)


class LocalProvider(BaseProvider):
    runner = "local"
    platform = "local"
    cli = None
    credential_group = None
    artifact_kinds = ("shell",)

    def detect(self) -> ProviderStatus:
        return ProviderStatus(runner=self.runner, cli=True, cli_path="", present=[], missing=[], mode="apply")

    def commands_for(self, stage: dict, records: list[dict]) -> list[dict]:
        script = stored_script(records)
        if script:
            return [{"argv": ["bash", script], "timeout": self.timeout, "label": str(stage.get("name") or "stage")}]
        return [dict(command) for command in stage.get("commands") or []]

    def would_run(self, stage: dict, records: list[dict], cwd: str, answers: dict) -> list[list[str]]:
        return [list(command["argv"]) for command in self.commands_for(stage, records)]

    def execute(self, stage: dict, records: list[dict], cwd: str, *, mode: str, answers: dict) -> StageRun:
        # Local never plans: there is nothing to wait for and no credential to miss.
        commands = self.commands_for(stage, records)
        if not commands:
            return StageRun(
                status="fail",
                exit_code=1,
                blocked=False,
                commands=[],
                checks=[{"name": str(stage.get("name") or "stage"), "status": "fail", "job": ""}],
                evidence="no generated script and no fixed commands for this stage",
                plan={},
            )
        return from_run_commands(run_commands(commands, cwd))
