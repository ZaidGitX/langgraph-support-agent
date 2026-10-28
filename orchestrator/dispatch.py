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


def workflow_command(
    stage: str,
    answers: dict | None,
    public_url: str,
    *,
    workflow_file: str | None = None,
    ref: str | None = None,
    inputs: dict | None = None,
) -> list[str]:
    """``gh workflow run`` for a fixed stage, or for a generated workflow on a branch."""
    workflow = workflow_file or STAGE_WORKFLOWS[stage]
    command = ["gh", "workflow", "run", workflow, "--ref", ref or _branch()]
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if repo:
        command.extend(["--repo", repo])
    if workflow_file is None:
        if stage == "dast":
            command.extend(["-f", f"app_url={public_url}"])
        if stage == "deploy":
            command.extend(["-f", f"environment={target_of(answers)}"])
    for key, value in (inputs or {}).items():
        command.extend(["-f", f"{key}={value}"])
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


def run_workflow(
    stage: str,
    answers: dict | None,
    public_url: str,
    *,
    workflow_file: str | None = None,
    ref: str | None = None,
    inputs: dict | None = None,
) -> dict:
    """Dispatch a workflow, find its run, watch it, and read its job steps and log.

    Fixed stages pass only ``stage``; a generated workflow passes ``workflow_file`` (which
    must already exist on ``ref``) and its ``inputs``.
    """
    answers = answers or {}
    public_url = (public_url or os.getenv("APP_PUBLIC_URL", "")).strip()
    if stage == "dast" and not public_url:
        return _failed(stage, "DAST was approved but the public app URL is empty.")
    if stage == "deploy" and target_of(answers) in FORBIDDEN_TARGETS:
        return _failed(stage, f"No path past UAT. Refusing target {target_of(answers)}.")
    workflow = workflow_file or STAGE_WORKFLOWS[stage]
    command = workflow_command(stage, answers, public_url, workflow_file=workflow_file, ref=ref, inputs=inputs)
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
        return _failed(stage, f"could not dispatch {workflow}: {exc}")
    if dispatched.returncode != 0:
        return _failed(stage, f"{dispatched.stdout}\n{dispatched.stderr}")
    try:
        run = _find_run(workflow, started)
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
        return _failed(stage, f"timed out waiting for {run.get('url') or workflow}")
    except OSError as exc:
        return _failed(stage, str(exc))
    evidence = redact(
        f"workflow {workflow} run {run.get('url') or run.get('databaseId')} "
        f"exit {watched.returncode}\n{watched.stdout}\n{watched.stderr}"
    )
    checks = _workflow_checks(str(run.get("databaseId") or ""))
    if not checks:
        checks = checks_from_commands(
            [{"label": workflow, "exit_code": int(watched.returncode)}]
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


def execute_stage_v2(
    spec: dict,
    commands: list[dict],
    cwd: str,
    answers: dict | None = None,
    readiness: dict | None = None,
    records: list[dict] | None = None,
) -> dict:
    """Run a generated stage through its runner's provider, in plan or apply mode.

    The policy checks the commands one last time; a provider that is not ready, or a cloud
    runner without ``ORCHESTRATOR_CLOUD_APPLY=1``, returns ``planned`` with the exact
    command it would have run and the names it is missing.
    """
    from . import findings as findings_module
    from . import policy
    from .providers import registry as providers

    name = str(spec.get("name") or "stage")
    runner = str(spec.get("runner") or "local")
    try:
        provider = providers.for_runner(runner)
    except KeyError as exc:
        return _failed(name, str(exc))
    verdict = policy.check_commands(commands or [])
    if not verdict.ok:
        reasons = "; ".join(f"{item.rule} line {item.line}: {item.message}" for item in verdict.findings if item.severity == "error")
        failed = _failed(name, f"policy blocked: {reasons}")
        failed["blocked"] = True
        return failed
    status = providers.status_from_readiness(readiness, runner) or provider.detect()
    if status.mode == "apply" and runner not in providers.LOCAL_RUNNERS:
        status = provider.detect()  # confirm live before anything leaves this host
    stage = dict(spec)
    stage.setdefault("commands", list(commands or []))
    run = provider.execute(stage, records or [], cwd, mode=status.mode, answers=answers or {})
    result = run.as_dict()
    key = str(spec.get("provider") or "")
    if key and result.get("status") in {"pass", "fail"} and result.get("checks"):
        lines = str(result.get("evidence") or "").splitlines()
        text, step, needs = findings_module.extract_result(key, lines, failed=result["status"] == "fail")
        first = dict(result["checks"][0])
        first.update({"tool": key, "findings": text, "next_step": step, "needs_astra": bool(needs)})
        result["checks"][0] = first
    return result
