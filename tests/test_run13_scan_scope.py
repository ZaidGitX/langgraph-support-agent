"""Live run 13 (2026-11-06): build, frontend_build, test, and scanners passed on CodeBuild; the
generated sca stage re-ran the whole scanners contract with no selection and died when the
contract's semgrep shim resolved a broken system-wide semgrep the stage had installed, and the
scanners stage's gitleaks could not run (docker exit 125, Docker Hub pull limit) without a trace.
The contract runs its own semgrep by module, survives a missing report, logs why a docker tool
failed, pulls trivy and gitleaks from GHCR, and a generated scan stage runs only its own provider."""

from __future__ import annotations

import subprocess

from orchestrator import generator, llm, policy, templates
from orchestrator.schemas import ArtifactSet, GeneratedArtifact

ENV = {"tier": "dev", "platform": "aws", "run_id": "r13"}


def test_scanners_contract_is_immune_to_a_system_semgrep_and_a_missing_report(tmp_path):
    text = templates.contract_script("scanners")
    for marker in (
        '"$OUT/semgrep-venv/bin/python" -m semgrep scan', "semgrep setuptools", "wrote no report", "could not be read",
        '> "$OUT/trivy.log" 2>&1', '> "$OUT/gitleaks.log" 2>&1', "see the lines above",
    ):
        assert marker in text, marker
    assert '"$OUT/semgrep-venv/bin/semgrep" scan' not in text  # never the PATH-resolving shim
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci" / "scanners.sh").write_text(text)
    assert subprocess.run(["bash", "-n", "ci/scanners.sh"], cwd=tmp_path, check=False, capture_output=True, text=True).returncode == 0
    assert policy.check_artifact(GeneratedArtifact(path="ci/scanners.sh", kind="shell", purpose="contract", content=text)).ok


def test_scanner_images_come_from_ghcr():
    images = templates.contract_images()
    assert images["TRIVY_IMAGE"].startswith("ghcr.io/aquasecurity/trivy:") and images["GITLEAKS_IMAGE"].startswith("ghcr.io/gitleaks/gitleaks:")
    assert "docker.io" not in images["TRIVY_IMAGE"] and "zricethezav" not in images["GITLEAKS_IMAGE"]


def test_a_generated_scan_stage_runs_only_its_own_provider():
    stage = {"name": "sca", "family": "scan", "provider": "pip_audit", "runner": "aws_codebuild", "environment": ENV}
    script = GeneratedArtifact(path="ci/sca.sh", kind="shell", purpose="sca", content="#!/usr/bin/env bash\nset -euo pipefail\nbash ci/scanners.sh\necho done\n")
    spec = GeneratedArtifact(path="ci/buildspec-sca.yml", kind="buildspec", purpose="sca", content="version: 0.2\nenv:\n  shell: bash\nphases:\n  build:\n    commands:\n      - bash ci/scanners.sh\n      - SCAN_TOOLS=\"snyk\" bash ci/scanners.sh\n")
    scoped = {item.path: item.content for item in generator.scope_scan_contract([script, spec], stage)}
    assert 'SCAN_TOOLS="pip-audit" bash ci/scanners.sh\necho done' in scoped["ci/sca.sh"]
    assert scoped["ci/buildspec-sca.yml"].count('SCAN_TOOLS="pip-audit" bash ci/scanners.sh') == 1
    assert 'SCAN_TOOLS="snyk" bash ci/scanners.sh' in scoped["ci/buildspec-sca.yml"]  # an explicit selection stays
    for other in ({**stage, "family": "test"}, {**stage, "provider": "grype"}, {**stage, "provider": ""}):
        assert generator.scope_scan_contract([script], other)[0].content == script.content
    with_manifest = generator.with_env_manifest(ArtifactSet(stage="sca", artifacts=[script, spec]), stage, [])
    assert any('SCAN_TOOLS="pip-audit" bash ci/scanners.sh' in item.content for item in with_manifest.artifacts)


def test_prompt_scopes_generated_scan_stages():
    for word in ("SCAN_TOOLS=<its catalog key> bash ci/scanners.sh", "never installs another semgrep"):
        assert word in llm.GENERATE_SYSTEM, word
