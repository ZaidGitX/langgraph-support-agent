"""Run fixed shell commands and keep a redacted evidence record."""

from __future__ import annotations

import json
import subprocess
from contextvars import ContextVar
from pathlib import Path

from . import redaction

_printer: ContextVar = ContextVar("orchestrator_printer", default=None)


def set_printer(printer) -> object:
    return _printer.set(printer)


def reset_printer(token) -> None:
    _printer.reset(token)


def note(message: str) -> None:
    printer = _printer.get()
    if printer is not None:
        printer(message)


def redact_secrets(text: str) -> str:
    """Remove credential material (registry secret values and credential-shaped tokens)."""
    return redaction.redact(text)


def redact(text: str, limit: int = 2000) -> str:
    return redaction.redact(text, limit=limit)


def _payload(text: str) -> dict | None:
    raw = (text or "").strip()
    if not raw:
        return None
    candidates = [raw]
    if "\n" in raw:
        candidates.append(raw.splitlines()[-1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def run_commands(commands: list[dict], cwd: str) -> dict:
    root = Path(cwd)
    (root / "var").mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    blocked = False
    worst = 0
    for command in commands:
        argv = [str(part) for part in command["argv"]]
        label = command.get("label") or argv[0]
        timeout = int(command.get("timeout") or 60)
        note(f"  $ {' '.join(argv)}")
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            exit_code = int(completed.returncode)
            evidence = redact(f"{completed.stdout}\n{completed.stderr}")
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            evidence = redact(f"timed out after {timeout}s\n{exc.stdout or ''}\n{exc.stderr or ''}")
        except OSError as exc:
            exit_code = 127
            evidence = redact(str(exc))
        payload = _payload(evidence)
        if payload and payload.get("refused"):
            blocked = True
        if exit_code != 0:
            worst = exit_code
        records.append(
            {
                "label": label,
                "argv": argv,
                "exit_code": exit_code,
                "evidence": evidence,
            }
        )
        if exit_code != 0:
            break
    status = "pass" if worst == 0 and not blocked else "fail"
    evidence = "\n".join(
        f"[{record['label']}] exit={record['exit_code']}\n{record['evidence']}" for record in records
    )
    return {
        "status": status,
        "exit_code": worst,
        "blocked": blocked,
        "commands": records,
        "checks": [
            {
                "name": record["label"],
                "status": "pass" if record["exit_code"] == 0 else "fail",
                "job": "",
            }
            for record in records
        ],
        "evidence": redact(evidence, limit=8000),
    }
