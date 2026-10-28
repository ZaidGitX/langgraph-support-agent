"""Docker runner: run the stored script, or a catalog scanner, inside a container with the
checkout mounted at /work."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .. import scanners
from ..catalog import scanner_argv
from .base import BaseProvider, ProviderStatus, run_id_of, stored_script

DEFAULT_IMAGE = "python:3.12-slim"
_DAEMON = {"checked_at": 0.0, "ok": False}
_DAEMON_TTL = 30.0


def daemon_reachable(timeout: float = 5.0) -> bool:
    """One ``docker info`` per 30 s; the only network-free call a detect() makes."""
    now = time.time()
    if now - _DAEMON["checked_at"] < _DAEMON_TTL:
        return bool(_DAEMON["ok"])
    try:
        completed = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        ok = completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    _DAEMON.update({"checked_at": now, "ok": ok})
    return ok


class DockerProvider(BaseProvider):
    runner = "docker"
    platform = "docker"
    cli = "docker"
    credential_group = "docker"
    artifact_kinds = ("shell", "dockerfile")

    def detect(self) -> ProviderStatus:
        status = super().detect()
        if status.cli and not daemon_reachable():
            status.mode = "plan"
            status.notes = "docker daemon unreachable"
        return status

    @staticmethod
    def generic_image() -> str:
        return os.getenv("ORCHESTRATOR_DOCKER_IMAGE", "").strip() or DEFAULT_IMAGE

    def _base(self, stage: dict, cwd: str, answers: dict) -> list[str]:
        return [
            "docker", "run", "--rm",
            "-v", f"{Path(cwd).resolve()}:/work", "-w", "/work",
            "-e", f"RUN_ID={run_id_of(stage, answers)}", "-e", f"STAGE={stage.get('name')}",
        ]

    def would_run(self, stage: dict, records: list[dict], cwd: str, answers: dict) -> list[list[str]]:
        base = self._base(stage, cwd, answers)
        script = stored_script(records)
        if script:
            inside = script
            try:
                inside = str(Path(script).resolve().relative_to(Path(cwd).resolve()))
            except ValueError:
                pass
            return [[*base, self.generic_image(), "bash", inside]]
        key = str(stage.get("provider") or "")
        image = scanners.docker_image(key)
        if image:
            argv = scanner_argv(key, str(stage.get("name") or ""), root=".", image=f"langgraph-support-app:{run_id_of(stage, answers)}",
                                app_url=os.getenv("APP_PUBLIC_URL", "http://127.0.0.1:8000"), run_id=run_id_of(stage, answers))
            # Scanner images use the tool as their entrypoint, so the tool name is dropped.
            return [[*base, image, *argv[1:]]]
        return [[*base, self.generic_image(), "bash", "-c", f"echo 'no generated script for stage {stage.get('name')}'; exit 1"]]
