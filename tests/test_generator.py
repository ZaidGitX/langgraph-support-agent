from __future__ import annotations

import json
from pathlib import Path

from orchestrator import generator, policy
from orchestrator.providers.aws import AwsProvider
from orchestrator.providers.local import LocalProvider
from orchestrator.schemas import ArtifactSet, GeneratedArtifact
from orchestrator.state import initial_state

FAKE_VALUE = "fakevalue-" + "q" * 20
STAGE = {
    "name": "container_scan",
    "family": "scan",
    "kind": "generated",
    "runner": "local",
    "provider": "trivy",
    "environment": {"tier": "dev", "platform": "docker", "region": ""},
    "purpose": "scan the built image",
    "artifacts": [{"kind": "shell", "path": "ci/container_scan.sh", "purpose": "scan"}],
}
OK_SCRIPT = "#!/usr/bin/env bash\nset -euo pipefail\necho ok\n"


def _state(root: str) -> dict:
    state = initial_state(root, run_id="run-7")
    state["ecosystem"] = {"retrieved": {"docker": [{"path": "Dockerfile", "lines": "1-3", "score": 1.0, "text": "FROM python", "topic": "docker"}]}}
    state["readiness"] = {"runners": {"local": {"mode": "apply", "missing": []}, "aws_codebuild": {"mode": "plan", "missing": ["AWS_REGION"]}}}
    state["answers"] = {"env_target": "dev", "env_platform": "docker"}
    return state


def test_context_for_carries_skeletons_paths_placeholders_and_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIVY_TOKEN", FAKE_VALUE)
    context = generator.context_for(_state(str(tmp_path)), STAGE, LocalProvider())
    assert context["stage"]["name"] == "container_scan" and context["run_id"] == "run-7"
    assert "shell" in context["skeletons"] and "container_scan" in context["skeletons"]["shell"]
    assert context["suggested_paths"]["shell"] == "ci/container_scan.sh"
    assert "TRIVY_TOKEN" in context["placeholders"]
    assert "docker" in context["retrieved"]
    assert context["readiness"] == {"runner": "local", "mode": "apply", "missing": []}
    assert any(line.startswith("prod.reference") for line in context["policy_rules"])
    assert context["policy_findings"] == []
    assert FAKE_VALUE not in json.dumps(context)
    aws = generator.context_for(_state(str(tmp_path)), {**STAGE, "runner": "aws_codebuild", "family": "deploy"}, AwsProvider())
    assert set(aws["skeletons"]) == {"buildspec", "shell"} and "AWS_REGION" in aws["placeholders"]
    assert aws["readiness"]["missing"] == ["AWS_REGION"]


def test_env_manifest_is_names_only():
    artifact = generator.env_manifest(STAGE, ["AWS_REGION", "AWS_SECRET_ACCESS_KEY"])
    assert artifact.kind == "env_example" and artifact.path == ".env.dev.example"
    assert "AWS_SECRET_ACCESS_KEY=\n" in artifact.content and "AWS_REGION=\n" in artifact.content
    assert artifact.secrets_referenced == ["AWS_SECRET_ACCESS_KEY"]
    merged = generator.with_env_manifest(ArtifactSet(stage="container_scan", artifacts=[GeneratedArtifact(kind="env_example", path=".env.dev.example", purpose="model", content="X=1\n")]), STAGE, ["AWS_REGION"])
    assert len(merged.artifacts) == 1 and "AWS_REGION=" in merged.artifacts[0].content and "X=1" not in merged.artifacts[0].content


def test_write_records_files_sha_and_policy(tmp_path):
    artifact_set = generator.with_env_manifest(
        ArtifactSet(stage="container_scan", artifacts=[GeneratedArtifact(kind="shell", path="ci/container_scan.sh", purpose="scan", content=OK_SCRIPT)]),
        STAGE,
        ["TRIVY_TOKEN"],
    )
    records = generator.write("run-7", STAGE, artifact_set, str(tmp_path))
    assert [record["kind"] for record in records] == ["shell", "env_example"]
    script = records[0]
    assert script["path"] == "ci/container_scan.sh" and len(script["sha256"]) == 64 and script["bytes"] == len(OK_SCRIPT)
    assert script["stored_at"] == str(tmp_path / "var" / "generated" / "run-7" / "container_scan" / "ci" / "container_scan.sh")
    assert Path(script["stored_at"]).read_text(encoding="utf-8") == OK_SCRIPT
    assert script["policy"] == {"ok": True, "errors": 0, "warns": 0} and script["findings"] == []
    assert generator.policy_rows(records, "container_scan") == []
    view = generator.records_for_view(records)
    assert view[0]["content"] == OK_SCRIPT and "TRIVY_TOKEN=" in view[1]["content"]
    assert generator.content_of({"stored_at": ""}) == ""


def test_write_keeps_a_bad_artifact_out_of_the_checkout(tmp_path):
    bad = ArtifactSet(
        stage="container_scan",
        artifacts=[
            GeneratedArtifact(kind="shell", path="../evil.sh", purpose="escape", content=OK_SCRIPT),
            GeneratedArtifact(kind="shell", path="ci/rm.sh", purpose="destroy", content="#!/usr/bin/env bash\nset -euo pipefail\nrm -rf /\n"),
        ],
    )
    records = generator.write("run-7", STAGE, bad, str(tmp_path))
    assert records[0]["stored_at"] == "" and records[0]["policy"]["ok"] is False
    assert records[1]["stored_at"].endswith("ci/rm.sh") and records[1]["policy"]["errors"] == 1
    rules = {row["rule"] for row in generator.policy_rows(records, "container_scan")}
    assert {"path.outside", "destructive.rm"} <= rules
    assert not (tmp_path / "evil.sh").exists()


def test_materialize_and_publish_are_gated(tmp_path, monkeypatch):
    artifact_set = generator.with_env_manifest(
        ArtifactSet(stage="container_scan", artifacts=[GeneratedArtifact(kind="shell", path="ci/container_scan.sh", purpose="scan", content=OK_SCRIPT)]),
        STAGE,
        [],
    )
    records = generator.write("run-7", STAGE, artifact_set, str(tmp_path))
    monkeypatch.delenv("ORCHESTRATOR_WRITE_ARTIFACTS", raising=False)
    assert generator.materialize("run-7", STAGE, str(tmp_path), records) == []
    assert not (tmp_path / "ci" / "container_scan.sh").exists()
    monkeypatch.setenv("ORCHESTRATOR_WRITE_ARTIFACTS", "1")
    written = generator.materialize("run-7", STAGE, str(tmp_path), records)
    assert written == ["ci/container_scan.sh", ".env.dev.example"]
    assert (tmp_path / "ci" / "container_scan.sh").read_text(encoding="utf-8") == OK_SCRIPT
    monkeypatch.delenv("ORCHESTRATOR_DISPATCH", raising=False)
    assert generator.publish_branch("run-7", str(tmp_path), written) == ""


def test_docs_path_and_generated_dir():
    assert str(generator.docs_path("/r", "run-1", {"platform": "aws", "tier": "dev"})).endswith("var/generated/run-1/pipeline/docs/deploy/aws-dev.md")
    assert generator.doc_relative_path({"platform": "aws", "tier": "dev"}) == "docs/deploy/aws-dev.md"
    assert policy.check_path(generator.doc_relative_path(None)).ok
    assert str(generator.generated_dir("/r", "run-1", "lint")).endswith("var/generated/run-1/lint")
