"""Live run 5 (2026-11-05) fixes: a bare secret id under CodeBuild's secrets-manager block is a
reference, not a literal, and the suite qualifies it with the app prefix; a file a worker wrote
outside the allowed prefixes is moved, not refused; a dropped stage's files no longer block the
approval gate; a policy finding gets its revision before a reject stands."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from orchestrator import generator, policy, suite
from orchestrator.graph import approval_payload
from orchestrator.llm import guard_review
from orchestrator.schemas import ArtifactReview, GeneratedArtifact
from orchestrator.templates import APP_NAME

ROOT = Path(__file__).resolve().parent.parent
MIRROR = ROOT / ".claude" / "skills" / "sprint3-agentic-cicd" / "scripts" / "policy_rules.py"

BUILDSPEC = (
    "version: 0.2\n"
    "env:\n"
    "  variables:\n"
    "    STAGE: \"scanners\"\n"
    "  secrets-manager:\n"
    "    SNYK_TOKEN: \"SNYK_TOKEN\"\n"
    f"    SONAR_TOKEN: \"{APP_NAME}/SONAR_TOKEN\"\n"
    "phases:\n"
    "  build:\n"
    "    commands:\n"
    "      - bash ci/scanners.sh\n"
)


def _mirror():
    spec = importlib.util.spec_from_file_location("policy_rules_mirror", MIRROR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_secret_id_that_is_a_credential_name_is_a_reference_in_both_policy_copies():
    assert policy.check_text(BUILDSPEC, kind="buildspec").ok
    literal = BUILDSPEC.replace('"SNYK_TOKEN"', '"abcdefgh12345678"')
    assert {f.rule for f in policy.check_text(literal, kind="buildspec").findings} == {"secret.assignment"}
    mirror = _mirror()
    assert mirror.check_text(BUILDSPEC, kind="buildspec") == []
    assert {f["rule"] for f in mirror.check_text(literal, kind="buildspec")} == {"secret.assignment"}
    assert not policy.check_text("API_KEY: NOT_A_REGISTRY_NAME\n", kind="buildspec").ok


def test_the_suite_qualifies_bare_secret_ids_only_inside_the_secrets_manager_block():
    item = GeneratedArtifact(path="ci/buildspec-scanners.yml", kind="buildspec", purpose="scan", content=BUILDSPEC)
    hardened = suite._harden(item)
    assert f'    SNYK_TOKEN: "{APP_NAME}/SNYK_TOKEN"' in hardened.content
    assert f'    SONAR_TOKEN: "{APP_NAME}/SONAR_TOKEN"' in hardened.content
    assert '    STAGE: "scanners"' in hardened.content
    assert suite.referenced_secrets([hardened]) == ["SNYK_TOKEN", "SONAR_TOKEN"]
    untouched = GeneratedArtifact(path="ci/buildspec-build.yml", kind="buildspec", purpose="build", content="env:\n  variables:\n    SNYK_TOKEN: \"SNYK_TOKEN\"\n")
    assert suite._harden(untouched).content == untouched.content


def test_files_outside_the_allowed_prefixes_are_moved_and_say_so():
    moved = generator.relocate(GeneratedArtifact(path="scripts/ci/integration.generated.py", kind="shell", purpose="run tests", content="#!/usr/bin/env python3\n"), "integration_test")
    assert moved.path == "ci/integration.generated.py" and "moved from scripts/ci/integration.generated.py" in moved.purpose
    workflow = generator.relocate(GeneratedArtifact(path=".github/workflows/integration.generated.yml", kind="github_workflow", purpose="w", content="on: workflow_dispatch\n"), "integration_test")
    assert workflow.path == ".github/workflows/gen-integration_test.yml"
    dockerfile = generator.relocate(GeneratedArtifact(path="Dockerfile.ci", kind="dockerfile", purpose="d", content="FROM scratch\n"), "build_image")
    assert dockerfile.path == "docker/Dockerfile.ci"
    kept = GeneratedArtifact(path="ci/x.sh", kind="shell", purpose="k", content="set -euo pipefail\n")
    assert generator.relocate(kept, "x") is kept
    escaping = GeneratedArtifact(path="../x.sh", kind="shell", purpose="e", content="x\n")
    assert generator.relocate(escaping, "x").path == "../x.sh"
    assert policy.check_path(moved.path).ok and policy.check_path(workflow.path).ok and policy.check_path(dockerfile.path).ok


def test_the_approval_payload_shows_only_the_stages_still_in_the_plan():
    bad = {"path": "scripts/x.py", "kind": "shell", "policy": {"ok": False, "errors": 1, "warns": 0}, "findings": [], "sha256": "", "bytes": 1}
    good = {"path": "ci/x.sh", "kind": "shell", "policy": {"ok": True, "errors": 0, "warns": 0}, "findings": [], "sha256": "", "bytes": 1}
    state = {
        "pipeline": {"stages": [{"name": "build"}, {"name": "container_scan"}, {"name": "uat"}], "suite": {"status": "suite rejected (reject); on the host instead: build"}, "dropped_stages": ["integration_test: artifacts rejected (reject)"]},
        "generated_artifacts": {"container_scan": [good], "integration_test": [bad], "pipeline": [bad]},
        "artifact_review": {"container_scan": {"verdict": "accept"}, "integration_test": {"verdict": "reject"}, "pipeline": {"verdict": "reject"}},
    }
    payload = approval_payload(state)
    assert set(payload["artifacts"]) == {"container_scan"} and set(payload["dropped_artifacts"]) == {"integration_test", "pipeline"}
    assert payload["artifact_review"] == {"container_scan": {"verdict": "accept"}}
    state["pipeline"]["suite"] = {"status": "ok"}
    state["generated_artifacts"]["pipeline"] = [good]
    state["artifact_review"]["pipeline"] = {"verdict": "accept"}
    assert set(approval_payload(state)["artifacts"]) == {"container_scan", "pipeline"}


def test_a_policy_finding_gets_its_revision_before_a_reject_stands():
    rejected = ArtifactReview(verdict="reject", rationale="Jev: reject")
    assert guard_review(rejected, False, 1).verdict == "revise"
    assert guard_review(rejected, False, 0).verdict == "reject"
    assert guard_review(rejected, True, 1).verdict == "reject"
    assert guard_review(ArtifactReview(verdict="accept", rationale="ok"), True, 0).verdict == "accept"
