"""Fail the build only when pip-audit reports a vulnerability that has a fix."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def fixable_vulnerabilities(report: dict) -> list[str]:
    found: list[str] = []
    for dependency in report.get("dependencies", []):
        name = dependency.get("name", "unknown")
        for vulnerability in dependency.get("vulns") or []:
            fixes = vulnerability.get("fix_versions") or []
            if fixes:
                found.append(f"{name} {vulnerability.get('id')} fix: {', '.join(fixes)}")
    return found


def main(path: Path) -> int:
    if not path.is_file():
        print(f"pip-audit did not write a report at {path}.")
        return 1

    report = json.loads(path.read_text(encoding="utf-8"))
    fixable = fixable_vulnerabilities(report)
    if fixable:
        print("Fixable vulnerabilities:")
        print("\n".join(fixable))
        return 1

    print("No fixable vulnerabilities reported.")
    return 0


if __name__ == "__main__":
    report_path = Path(sys.argv[1] if len(sys.argv) > 1 else "audit.json")
    raise SystemExit(main(report_path))
