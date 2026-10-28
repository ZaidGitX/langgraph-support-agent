"""Remote host over SSH: copy the stored script (or compose file) and run it."""

from __future__ import annotations

from .base import BaseProvider, artifact_of_kind, run_id_of, stored_script


class SshProvider(BaseProvider):
    runner = "ssh_remote"
    platform = "remote"
    cli = "ssh"
    credential_group = "ssh"
    artifact_kinds = ("shell", "compose")

    @staticmethod
    def _ssh() -> list[str]:
        return ["ssh", "-i", "$SSH_KEY_PATH", "-p", "$SSH_PORT", "-o", "BatchMode=yes", "$SSH_USER@$SSH_HOST"]

    @staticmethod
    def _scp(source: str, target: str) -> list[str]:
        return ["scp", "-i", "$SSH_KEY_PATH", "-P", "$SSH_PORT", "-o", "BatchMode=yes", source, f"$SSH_USER@$SSH_HOST:{target}"]

    def would_run(self, stage: dict, records: list[dict], cwd: str, answers: dict) -> list[list[str]]:
        run_id = run_id_of(stage, answers)
        name = str(stage.get("name") or "stage")
        if str(stage.get("provider") or "") == "ssh_compose":
            compose = artifact_of_kind(records, "compose", cwd=cwd) or "docker-compose.generated.yml"
            remote = f"/tmp/{run_id}-docker-compose.generated.yml"
            return [self._scp(compose, remote), [*self._ssh(), f"docker compose -f {remote} up -d"]]
        script = stored_script(records, cwd) or f"ci/{name}.sh"
        remote = f"/tmp/{run_id}-{name}.sh"
        return [self._scp(script, remote), [*self._ssh(), f"bash {remote}"]]
