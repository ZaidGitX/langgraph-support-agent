"""Jenkins runner over the REST API (no CLI): crumb, buildWithParameters, poll, consoleText."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from ..runner import redact
from .base import (
    BaseProvider,
    StageRun,
    artifact_of_kind,
    expand_env,
    plan_result,
    run_id_of,
    tier_of,
)

POLL_SECONDS = 10


class JenkinsProvider(BaseProvider):
    runner = "jenkins"
    platform = "jenkins"
    cli = None
    credential_group = "jenkins"
    artifact_kinds = ("jenkinsfile",)
    timeout = 1800

    def would_run(self, stage: dict, records: list[dict], cwd: str, answers: dict) -> list[list[str]]:
        # Placeholders by name only; the plan never shows a value.
        return [
            [
                "curl", "-sS", "-u", "$JENKINS_USER:$JENKINS_API_TOKEN", "-X", "POST",
                "$JENKINS_URL/job/$JENKINS_JOB/buildWithParameters",
                "--data-urlencode", f"RUN_ID={run_id_of(stage, answers)}",
                "--data-urlencode", f"TIER={tier_of(stage, answers)}",
                "--data-urlencode", f"STAGE={stage.get('name') or ''}",
            ]
        ]

    # --- Sprint 4 phase 9: provisioning and secret injection ---------------------------------

    injection_target = "Jenkins credentials store (secret text, id = NAME)"

    def provision_commands(self, records: list[dict], cwd: str, answers: dict) -> list[list[str]]:
        """Create the pipeline job from the generated deploy/jenkins-job.xml (REST createItem)."""
        job_xml = artifact_of_kind(records, "jenkins_job", cwd=cwd)
        if not job_xml:
            return []
        return [[
            "curl", "-sS", "-f", "-u", "$JENKINS_USER:$JENKINS_API_TOKEN", "-X", "POST",
            "-H", "Content-Type: application/xml", "--data-binary", f"@{job_xml}",
            "$JENKINS_URL/createItem?name=$JENKINS_JOB",
        ]]

    def inject_commands(self, names: list[str], answers: dict) -> list[list[str]]:
        """One secret-text credential per name; the value arrives on stdin as the JSON body."""
        return [[
            "curl", "-sS", "-f", "-u", "$JENKINS_USER:$JENKINS_API_TOKEN", "-X", "POST",
            "--data-urlencode", "json@-",
            "$JENKINS_URL/credentials/store/system/domain/_/createCredentials",
        ] for _name in names]

    def run_injection(self, name: str, argv: list[str], cwd: str) -> bool:
        """Wrap the value in the credentials JSON on stdin; the argv never carries it."""
        value = os.getenv(name, "")
        payload = json.dumps({
            "": "0",
            "credentials": {
                "scope": "GLOBAL", "id": name, "description": f"orchestrator: {name}", "secret": value,
                "$class": "org.jenkinsci.plugins.plaincredentials.impl.StringCredentialsImpl",
            },
        })
        try:
            completed = subprocess.run(
                expand_env(argv), cwd=cwd, input=payload, capture_output=True, text=True, timeout=60, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0

    def execute(self, stage: dict, records: list[dict], cwd: str, *, mode: str, answers: dict) -> StageRun:
        status = self.detect()
        argvs = self.would_run(stage, records, cwd, answers or {})
        if mode != "apply" or status.mode != "apply":
            return plan_result(self.runner, status.missing, argvs, records)
        return self._rest_build(stage, answers or {})

    # --- REST ---------------------------------------------------------------------------

    @staticmethod
    def _auth_header() -> str:
        user = os.getenv("JENKINS_USER", "")
        token = os.getenv("JENKINS_API_TOKEN", "")
        return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()

    def _request(self, method: str, url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 30) -> tuple[int, str]:
        request = urllib.request.Request(url, data=data, method=method, headers={"Authorization": self._auth_header(), **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return int(response.status), response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return 0, f"{type(exc).__name__}: {exc}"

    def _rest_build(self, stage: dict, answers: dict) -> StageRun:
        base = os.getenv("JENKINS_URL", "").rstrip("/")
        job = os.getenv("JENKINS_JOB", "")
        label = f"jenkins:{job}"
        headers: dict[str, str] = {}
        code, body = self._request("GET", f"{base}/crumbIssuer/api/json")
        if code == 200:
            try:
                crumb = json.loads(body)
                headers[crumb["crumbRequestField"]] = crumb["crumb"]
            except (ValueError, KeyError):
                pass
        params = urllib.parse.urlencode({"RUN_ID": run_id_of(stage, answers), "TIER": tier_of(stage, answers), "STAGE": str(stage.get("name") or "")}).encode()
        code, body = self._request("POST", f"{base}/job/{job}/buildWithParameters", data=params, headers=headers)
        if code not in (200, 201, 302):
            evidence = redact(f"buildWithParameters returned {code}: {body}", limit=2000)
            return StageRun(status="fail", exit_code=1, blocked=False,
                            commands=[{"label": label, "argv": ["POST", f"{base}/job/{job}/buildWithParameters"], "exit_code": 1, "evidence": evidence}],
                            checks=[{"name": label, "status": "fail", "job": ""}], evidence=evidence, plan={})
        deadline = time.time() + self.timeout
        result = ""
        number = ""
        while time.time() < deadline:
            time.sleep(POLL_SECONDS)
            code, body = self._request("GET", f"{base}/job/{job}/lastBuild/api/json")
            if code != 200:
                continue
            try:
                payload = json.loads(body)
            except ValueError:
                continue
            number = str(payload.get("number") or "")
            if not payload.get("building") and payload.get("result"):
                result = str(payload["result"])
                break
        code, console = self._request("GET", f"{base}/job/{job}/lastBuild/consoleText")
        evidence = redact(f"jenkins job {job} build {number} result {result or 'TIMEOUT'}\n{console if code == 200 else ''}", limit=8000)
        passed = result == "SUCCESS"
        return StageRun(
            status="pass" if passed else "fail",
            exit_code=0 if passed else 1,
            blocked=False,
            commands=[{"label": label, "argv": ["POST", f"{base}/job/{job}/buildWithParameters"], "exit_code": 0 if passed else 1, "evidence": evidence}],
            checks=[{"name": label, "status": "pass" if passed else "fail", "job": ""}],
            evidence=evidence,
            plan={},
        )
