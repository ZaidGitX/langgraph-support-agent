"""Run a designated stage as its GitHub workflow, or the fixed local commands.

Only stages Jev put in the approved pipeline get here. A stage with no
workflow file runs the local command list even when GitHub dispatch is on.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone

from .findings import enrich_checks
from .runner import redact, run_commands
from .state import FORBIDDEN_TARGETS, target_of

STAGE_WORKFLOWS = {
    "build": "build.yml",
    "test": "test.yml",
    "package": "package.yml",
    "scanners": "scanners.yml",
    "dast": "dast.yml",
    "release": "release.yml",
    "deploy": "deploy.yml",
    "uat": "uat.yml",
}


def workflows_for(stage_names: list[str]) -> list[str]:
    workflows: list[str] = []
    for name in stage_names:
        workflow = STAGE_WORKFLOWS.get(name)
        if workflow and workflow not in workflows:
            workflows.append(workflow)
    return workflows


def use_github(stage: str) -> bool:
    mode = os.getenv("ORCHESTRATOR_DISPATCH", "local").strip().lower()
    return mode == "github" and stage in STAGE_WORKFLOWS


def _branch() -> str:
    name = os.getenv("GITHUB_REF_NAME", "").strip()
    if name:
        return name
    ref = os.getenv("GITHUB_REF", "").strip()
    for prefix in ("refs/heads/", "refs/tags/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ref or "main"


def _gh_env() -> dict[str, str]:
    env = dict(os.environ)
    token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or ""
    if token:
        env["GH_TOKEN"] = token
    return env


def workflow_command(stage: str, answers: dict | None, public_url: str) -> list[str]:
    workflow = STAGE_WORKFLOWS[stage]
    command = ["gh", "workflow", "run", workflow, "--ref", _branch()]
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if repo:
        command.extend(["--repo", repo])
    if stage == "dast":
        command.extend(["-f", f"app_url={public_url}"])
    if stage == "deploy":
        command.extend(["-f", f"environment={target_of(answers)}"])
    return command


def commands_for_stages(
    stage_names: list[str],
    answers: dict | None = None,
    public_url: str = "",
) -> list[list[str]]:
    return [
        workflow_command(stage, answers, public_url)
        for stage in stage_names
        if stage in STAGE_WORKFLOWS
    ]


def checks_from_jobs(jobs: list[dict]) -> list[dict]:
    """Turn GitHub job steps into pass, fail, or skip checks for the results board."""
    checks: list[dict] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_name = str(job.get("name") or "job")
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            name = str(step.get("name") or "").strip()
            if not name or name in {"Set up job", "Complete job"} or name.startswith("Post "):
                continue
            conclusion = str(step.get("conclusion") or step.get("status") or "").lower()
            if conclusion == "success":
                status = "pass"
            elif conclusion in {"failure", "cancelled", "timed_out", "startup_failure"}:
                status = "fail"
            elif conclusion == "skipped":
                status = "skip"
            else:
                status = conclusion or "pending"
            checks.append({"name": name, "status": status, "job": job_name})
    return checks


def checks_from_commands(commands: list[dict]) -> list[dict]:
    checks: list[dict] = []
    for command in commands:
        if not isinstance(command, dict):
            continue
        code = command.get("exit_code")
        if code == 0:
            status = "pass"
        elif code is None:
            status = "pending"
        else:
            status = "fail"
        checks.append({"name": str(command.get("label") or "command"), "status": status, "job": ""})
    return checks


def _workflow_checks(run_id: str) -> list[dict]:
    command = ["gh", "run", "view", str(run_id), "--json", "jobs"]
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if repo:
        command.extend(["--repo", repo])
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=_gh_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return []
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        return []
    checks = checks_from_jobs(jobs)
    return enrich_checks(checks, _workflow_log(run_id))


def _workflow_log(run_id: str) -> str:
    command = ["gh", "run", "view", str(run_id), "--log"]
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if repo:
        command.extend(["--repo", repo])
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=_gh_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout or ""


def _failed(stage: str, message: str) -> dict:
    evidence = redact(message)
    workflow = STAGE_WORKFLOWS.get(stage, stage)
    return {
        "status": "fail",
        "exit_code": 1,
        "blocked": stage == "deploy" and "Refusing" in message,
        "commands": [
            {
                "label": stage,
                "argv": ["gh", "workflow", "run", workflow],
                "exit_code": 1,
                "evidence": evidence,
            }
        ],
        "evidence": evidence,
        "checks": checks_from_commands(
            [
                {
                    "label": stage,
                    "exit_code": 1,
                }
            ]
        ),
    }


def _created_after(raw: str | None, moment: float) -> bool:
    if not raw:
        return False
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp() >= moment


def _find_run(workflow: str, started: float) -> dict:
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    sha = os.getenv("GITHUB_SHA", "").strip()
    command = [
        "gh",
        "run",
        "list",
        "--workflow",
        workflow,
        "--limit",
        "10",
        "--json",
        "databaseId,createdAt,event,headSha,status,url,conclusion",
    ]
    if repo:
        command.extend(["--repo", repo])
    for _ in range(30):
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=_gh_env(),
        )
        rows: list[dict] = []
        if completed.returncode == 0:
            try:
                parsed = json.loads(completed.stdout or "[]")
            except json.JSONDecodeError:
                parsed = []
            if isinstance(parsed, list):
                rows = [row for row in parsed if isinstance(row, dict)]
        matches = []
        for row in rows:
            if row.get("event") != "workflow_dispatch":
                continue
            if sha and row.get("headSha") and row.get("headSha") != sha:
                continue
            if not _created_after(str(row.get("createdAt") or ""), started - 20):
                continue
            matches.append(row)
        if matches:
            matches.sort(key=lambda row: str(row.get("createdAt") or ""), reverse=True)
            return matches[0]
        time.sleep(2)
    raise RuntimeError(f"dispatched {workflow} did not appear in gh run list")


def run_workflow(stage: str, answers: dict | None, public_url: str) -> dict:
    answers = answers or {}
    public_url = (public_url or os.getenv("APP_PUBLIC_URL", "")).strip()
    if stage == "dast" and not public_url:
        return _failed(stage, "DAST was approved but the public app URL is empty.")
    if stage == "deploy" and target_of(answers) in FORBIDDEN_TARGETS:
        return _failed(stage, f"No path past UAT. Refusing target {target_of(answers)}.")
    command = workflow_command(stage, answers, public_url)
    started = time.time()
    try:
        dispatched = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=_gh_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _failed(stage, f"could not dispatch {STAGE_WORKFLOWS[stage]}: {exc}")
    if dispatched.returncode != 0:
        return _failed(stage, f"{dispatched.stdout}\n{dispatched.stderr}")
    try:
        run = _find_run(STAGE_WORKFLOWS[stage], started)
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        return _failed(stage, str(exc))
    watch = ["gh", "run", "watch", str(run.get("databaseId")), "--exit-status"]
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if repo:
        watch.extend(["--repo", repo])
    try:
        watched = subprocess.run(
            watch,
            capture_output=True,
            text=True,
            timeout=2400,
            check=False,
            env=_gh_env(),
        )
    except subprocess.TimeoutExpired:
        return _failed(stage, f"timed out waiting for {run.get('url') or STAGE_WORKFLOWS[stage]}")
    except OSError as exc:
        return _failed(stage, str(exc))
    evidence = redact(
        f"workflow {STAGE_WORKFLOWS[stage]} run {run.get('url') or run.get('databaseId')} "
        f"exit {watched.returncode}\n{watched.stdout}\n{watched.stderr}"
    )
    checks = _workflow_checks(str(run.get("databaseId") or ""))
    if not checks:
        checks = checks_from_commands(
            [{"label": STAGE_WORKFLOWS[stage], "exit_code": int(watched.returncode)}]
        )
    return {
        "status": "pass" if watched.returncode == 0 else "fail",
        "exit_code": int(watched.returncode),
        "blocked": False,
        "commands": [
            {
                "label": stage,
                "argv": command,
                "exit_code": int(watched.returncode),
                "evidence": evidence,
            }
        ],
        "checks": checks,
        "evidence": evidence,
    }


def execute_stage(stage: str, commands: list[dict], cwd: str, answers: dict | None = None) -> dict:
    if use_github(stage):
        return run_workflow(stage, answers or {}, os.getenv("APP_PUBLIC_URL", "").strip())
    return run_commands(commands, cwd)
