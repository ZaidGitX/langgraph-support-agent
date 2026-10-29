"""Pull findings out of a GitHub Actions log for the results tiles.

The extract uses only lines the tool printed. Astra is asked to write the
next step when that extract contains a finding or the step failed.
"""

from __future__ import annotations

import re

from . import scanners

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.]+Z\s*")

_TOOL_KEYS = (
    ("semgrep", "semgrep"),
    ("pytest", "pytest"),
    ("trivy", "trivy"),
    ("snyk", "snyk"),
    ("codeql", "codeql"),
    ("sonar", "sonar"),
    ("zap", "zap"),
    ("stackhawk", "stackhawk"),
)


def tool_key(step_name: str) -> str:
    name = (step_name or "").lower()
    if "install snyk" in name or name.startswith("require sonar"):
        return ""
    for needle, key in _TOOL_KEYS:
        if needle in name:
            return key
    for alias, key in scanners.aliases().items():
        if alias in name:
            return key
    return ""


_GENERIC_LINE = re.compile(r"(?i)(error|fail|vuln|critical|high|finding|issue)")


def _extract_from_catalog(spec: dict, lines: list[str], failed: bool) -> tuple[str, str, bool]:
    """Findings from a catalog ``extract`` block: pick lines, count, decide, suggest."""
    line_regex = spec.get("line_regex") or ""
    try:
        picker = re.compile(line_regex) if line_regex else None
    except re.error:
        picker = None
    picked = [line for line in lines if picker.search(line)] if picker else list(lines[-8:])
    findings = _keep(picked)
    count = None
    if spec.get("count_regex"):
        try:
            count = _count(spec["count_regex"], "\n".join(lines))
        except re.error:
            count = None
    fail_when = str(spec.get("fail_when") or "exit!=0")
    hit = bool(count) if fail_when == "count>0" else bool(failed)
    if failed:
        hit = True
    step = str(spec.get("next_step_hit") if hit else spec.get("next_step_clean") or "")
    if not findings:
        findings = "The tool reported findings; open the stage log." if hit else "The tool finished without a summary line."
    return findings, step, hit


def extract_generic(lines: list[str], failed: bool) -> tuple[str, str, bool]:
    picked = [line for line in lines if _GENERIC_LINE.search(line)]
    findings = _keep(picked, limit=8)
    if failed:
        return findings or "The step failed; open the stage log.", "Read the findings and fix this step, then run the stage again.", True
    return findings or "The step finished.", "No change is required unless the log shows a finding.", False


def parse_workflow_log(text: str) -> dict[tuple[str, str], list[str]]:
    groups: dict[tuple[str, str], list[str]] = {}
    for line in (text or "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        job, step, message = parts
        message = _ANSI.sub("", message)
        message = _TIMESTAMP.sub("", message).strip()
        if not message or message.startswith(("##[", "shell:")):
            continue
        groups.setdefault((job, step), []).append(message)
    return groups


def _keep(lines: list[str], limit: int = 12) -> str:
    chosen = [line.strip() for line in lines if line.strip()]
    text = "\n".join(chosen[:limit])
    if len(text) > 900:
        return text[:900] + "\n…[truncated]"
    return text


def _count(pattern: str, text: str) -> int | None:
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def extract_result(tool: str, lines: list[str], *, failed: bool = False) -> tuple[str, str, bool]:
    """Return findings, a fallback next step, and whether Astra should reason.

    The eight Sprint 2 extractors stay as written; every other catalog tool uses its
    ``extract`` block, and an unknown tool gets the generic error-line extractor.
    """
    if tool == "semgrep":
        picked = [
            line
            for line in lines
            if "❯" in line or line.endswith(".py") or "Findings:" in line or line.startswith("Ran ")
        ]
        findings = _keep(picked)
        count = _count(r"Findings:\s*(\d+)", findings)
        if count == 0 or "0 finding" in findings:
            return findings or "Semgrep reported no findings.", "No code change is required for Semgrep.", False
        if count:
            return (
                findings,
                "Change the code Semgrep named, then run the scanners stage again.",
                True,
            )
        return findings or "Semgrep finished without a findings summary.", "Open the scanners log if this summary is empty.", False

    if tool == "pytest":
        picked = [line for line in lines if "passed" in line or "failed" in line or line.startswith("FAILED")]
        findings = _keep(picked, limit=8)
        failed = _count(r"(\d+)\s+failed", findings) or 0
        if failed or "FAILED" in findings:
            return findings or "Pytest reported failures.", "Fix the failing tests, then run the test stage again.", True
        return findings or "Pytest finished.", "No test change is required.", False

    if tool == "trivy":
        picked = [
            line
            for line in lines
            if "Report Summary" in line
            or "requirements.txt" in line
            or "package-lock.json" in line
            or line.strip().startswith("│")
            or line.strip().startswith("Total:")
        ]
        findings = _keep(picked, limit=8)
        counts = [int(item) for item in re.findall(r"│\s+(\d+)\s+│", findings)]
        counts.extend(int(item) for item in re.findall(r"Total:\s*(\d+)", findings))
        if any(count > 0 for count in counts):
            return findings, "Upgrade the vulnerable package Trivy listed, then run scanners again.", True
        return findings or "Trivy reported no high or critical vulnerabilities.", "No dependency change is required for Trivy.", False

    if tool == "snyk":
        picked = [
            line
            for line in lines
            if "Tested" in line
            or "vulnerable" in line.lower()
            or line.strip().startswith("✗")
            or "upgrade" in line.lower()
            or "patch" in line.lower()
            or "Severity" in line
        ]
        findings = _keep(picked)
        found = _count(r"found\s+(\d+)\s+issue", findings)
        if found:
            if "no upgrade or patch" in findings.lower() or "no direct upgrade" in findings.lower():
                step = "No upgrade is available. Track the Snyk issue and keep the current pin until a fixed release exists."
            else:
                step = "Upgrade the package Snyk named, then run the scanners stage again."
            return findings, step, True
        if "no vulnerable paths" in findings.lower():
            return findings, "No dependency change is required for this Snyk test.", False
        return findings or "Snyk finished without an issue summary.", "Open the scanners log if this summary is empty.", False

    if tool == "codeql":
        picked = [
            line
            for line in lines
            if "alert" in line.lower() or "uploaded" in line.lower() or "no results" in line.lower()
        ]
        findings = _keep(picked, limit=6)
        alerts = _count(r"(\d+)\s+alerts?", findings)
        if alerts:
            return findings, "Open the repository code scanning alerts and fix the CodeQL results.", True
        text = findings or "CodeQL analysis completed and uploaded results to GitHub code scanning."
        return text, "Open the repository code scanning page for this language. No code change is required when it shows no alerts.", False

    if tool == "sonar":
        picked = [
            line
            for line in lines
            if "ANALYSIS SUCCESSFUL" in line or "dashboard" in line.lower() or "QUALITY GATE" in line
        ]
        findings = _keep(picked, limit=4)
        if "failed" in findings.lower() or "error" in findings.lower():
            return findings or "SonarQube Cloud scan failed.", "Open the SonarQube Cloud dashboard and fix the failed quality gate.", True
        text = findings or "SonarQube Cloud analysis was uploaded."
        return text, "Open the SonarQube Cloud dashboard after processing finishes and fix any new issues.", False

    if tool == "zap":
        picked = [line for line in lines if "WARN-NEW" in line or "FAIL-NEW" in line or line.startswith("FAIL-NEW:")]
        findings = _keep(picked)
        failed = _count(r"FAIL-NEW:\s*(\d+)", findings) or 0
        warned = _count(r"WARN-NEW:\s*(\d+)", findings) or 0
        if failed or warned:
            return findings, "Add the missing browser security headers ZAP warned about, then run DAST again.", True
        return findings or "ZAP baseline reported no new warnings or failures.", "No header change is required for ZAP.", False

    if tool == "stackhawk":
        picked = [
            line
            for line in lines
            if "High:" in line or "View on StackHawk" in line or line.strip().startswith("Risk:")
        ]
        findings = _keep(picked, limit=8)
        high = _count(r"High:\s*(\d+)", findings) or 0
        medium = _count(r"Medium:\s*(\d+)", findings) or 0
        if high or medium:
            return findings, "Open the StackHawk scan link and fix the Medium or High findings, then run DAST again.", True
        return findings or "StackHawk finished.", "No application change is required when StackHawk shows no Medium or High findings.", False

    item = scanners.entry(tool)
    if item and isinstance(item.get("extract"), dict):
        return _extract_from_catalog(item["extract"], lines, failed)
    if tool:
        return extract_generic(lines, failed)
    return "", "", False


def enrich_checks(checks: list[dict], log_text: str) -> list[dict]:
    groups = parse_workflow_log(log_text)
    enriched: list[dict] = []
    for check in checks:
        tool = tool_key(str(check.get("name") or ""))
        if not tool:
            enriched.append(check)
            continue
        job = str(check.get("job") or "")
        lines = groups.get((job, str(check.get("name") or "")), [])
        if not lines:
            for (group_job, group_step), group_lines in groups.items():
                if group_step == check.get("name") and (not job or group_job == job):
                    lines = group_lines
                    break
        if check.get("status") == "skip":
            # GitHub skips every later step of a job once one step fails. An empty log must
            # not read as a clean result, so the tile says the step did not run.
            updated = dict(check)
            updated["tool"] = tool
            updated["findings"] = "This step did not run. GitHub skipped it after an earlier step in the job failed."
            updated["next_step"] = "Fix the failed step, then run the stage again."
            updated["needs_astra"] = False
            enriched.append(updated)
            continue
        findings, next_step, needs = extract_result(tool, lines, failed=check.get("status") == "fail")
        if check.get("status") == "fail":
            needs = True
            if not next_step:
                next_step = "Read the findings and fix this step, then run the stage again."
        updated = dict(check)
        updated["tool"] = tool
        updated["findings"] = findings
        updated["next_step"] = next_step
        updated["needs_astra"] = needs
        enriched.append(updated)
    return enriched


def apply_astra_advice(checks: list[dict], advise) -> list[dict]:
    """Ask Astra only for checks that have findings or failed."""
    pending = [check for check in checks if check.get("needs_astra") and check.get("findings")]
    if not pending or advise is None:
        return checks
    try:
        advice = advise(
            {
                "checks": [
                    {
                        "name": check.get("name"),
                        "status": check.get("status"),
                        "findings": check.get("findings"),
                    }
                    for check in pending
                ]
            }
        )
    except (OSError, RuntimeError, ValueError, TypeError):
        return checks
    by_name = {item.name: item for item in advice.checks}
    updated: list[dict] = []
    for check in checks:
        item = by_name.get(check.get("name"))
        if item is None:
            updated.append(check)
            continue
        revised = dict(check)
        if (item.findings or "").strip():
            revised["findings"] = item.findings.strip()
        if (item.next_step or "").strip():
            revised["next_step"] = item.next_step.strip()
        revised["astra"] = True
        updated.append(revised)
    return updated
