"""Live run 14 (2026-11-06): seven stages passed on CodeBuild up to the image wave; the generated
container_scan failed the built image on 66 unfixable Debian findings and one fixable setuptools.
The image scan now ignores vulnerabilities without a fix and the Dockerfile upgrades setuptools."""

from __future__ import annotations

from pathlib import Path

from orchestrator import llm, scanners

ROOT = Path(__file__).resolve().parent.parent


def test_image_scans_ignore_unfixable_findings_and_fail_on_fixable_ones():
    trivy = scanners.entry("trivy")["argv"]["container_scan"]
    assert "--ignore-unfixed" in trivy and "--severity" in trivy and trivy[trivy.index("--exit-code") + 1] == "1" and trivy[-1] == "$IMAGE"
    grype = scanners.entry("grype")["argv"]["container_scan"]
    assert "--only-fixed" in grype and "--fail-on" in grype
    assert "image_policy" in scanners.entry("trivy")
    for word in ("--ignore-unfixed", "fails on fixable HIGH or CRITICAL"):
        assert word in llm.GENERATE_SYSTEM, word


def test_the_image_upgrades_setuptools_past_the_known_vulnerability():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert '"setuptools>=78.1.1"' in dockerfile  # CVE-2025-47273: the one fixable finding of live run 14
    assert dockerfile.index('"setuptools>=78.1.1"') < dockerfile.index("-r requirements-dev.txt")
