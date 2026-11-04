"""Code-level fixes to model-written platform files (live run 3 found both defects at the gate)."""

from __future__ import annotations

from orchestrator import generator, suite, templates
from orchestrator.schemas import ArtifactSet, GeneratedArtifact, PipelineSuite

BUILDSPEC = "version: 0.2\nenv:\n  variables:\n    RUN_ID: \"r1\"\n    AWS_REGION: \"\"\n    TIER: \"dev\"\nphases:\n  build:\n    commands:\n      - echo ok\n"


def test_an_empty_region_is_dropped_from_a_buildspec_but_a_real_one_stays():
    fixed = generator.harden(GeneratedArtifact(kind="buildspec", path="ci/buildspec-sca.yml", purpose="x", content=BUILDSPEC))
    assert "AWS_REGION" not in fixed.content and "TIER: \"dev\"" in fixed.content and "RUN_ID" in fixed.content
    kept = GeneratedArtifact(kind="buildspec", path="ci/buildspec-sca.yml", purpose="x", content=BUILDSPEC.replace('AWS_REGION: ""', 'AWS_REGION: "us-east-1"'))
    assert generator.harden(kept).content == kept.content
    shell = GeneratedArtifact(kind="shell", path="ci/x.sh", purpose="x", content="#!/usr/bin/env bash\nAWS_REGION: \"\"\n")
    assert generator.harden(shell).content == shell.content
    bare = generator.harden(GeneratedArtifact(kind="buildspec", path="ci/b.yml", purpose="x", content="env:\n  variables:\n    AWS_REGION:\n    X: y\n"))
    assert "AWS_REGION" not in bare.content and "X: y" in bare.content


def test_with_env_manifest_hardens_every_generated_buildspec():
    artifact_set = ArtifactSet(stage="sca", artifacts=[GeneratedArtifact(kind="buildspec", path="ci/buildspec-sca.yml", purpose="x", content=BUILDSPEC)])
    fixed = generator.with_env_manifest(artifact_set, {"name": "sca", "environment": {"tier": "dev", "platform": "aws"}}, [])
    assert "AWS_REGION" not in fixed.artifacts[0].content


def test_the_suite_keeps_privileged_mode_on_for_the_project_document():
    document = GeneratedArtifact(kind="codebuild_project", path="deploy/codebuild-project.json", purpose="x", content='{"name": "p", "environment": {"privilegedMode":false, "image": "x"}}')
    spec = GeneratedArtifact(kind="buildspec", path="ci/buildspec-build.yml", purpose="x", content=BUILDSPEC)
    hardened = suite.with_manifest(PipelineSuite(platform="aws", runner="aws_codebuild", artifacts=[document, spec]), {"tier": "dev", "platform": "aws"}, [])
    by_path = {item.path: item for item in hardened.artifacts}
    assert '"privilegedMode": true' in by_path["deploy/codebuild-project.json"].content
    assert "AWS_REGION" not in by_path["ci/buildspec-build.yml"].content
    assert any(item.kind == "env_example" for item in hardened.artifacts)


def test_the_generated_stage_skeleton_no_longer_sets_the_region():
    environment = {"tier": "dev", "platform": "aws"}
    skeleton = templates.render_template("buildspec", {"name": "sca", "family": "scan", "purpose": "x", "environment": environment}, environment, runner="aws_codebuild", run_id="r1", placeholders=[])
    assert not any(line.strip().startswith("AWS_REGION:") for line in skeleton.splitlines())
    project = templates.render_template("codebuild_project", {"name": "pipeline", "family": "custom", "purpose": "x", "environment": environment}, environment, runner="aws_codebuild", run_id="r1", placeholders=[])
    assert '"privilegedMode": true' in project and "privilegedMode stays true" in project


def test_the_project_description_is_cut_to_codebuild_limit():
    long = "x" * 300
    document = GeneratedArtifact(kind="codebuild_project", path="deploy/codebuild-project.json", purpose="x",
                                 content='{"name": "p", "description": "' + long + '", "environment": {"privilegedMode": false}}')
    hardened = suite.with_manifest(PipelineSuite(platform="aws", runner="aws_codebuild", artifacts=[document]), {"tier": "dev", "platform": "aws"}, [])
    import json

    parsed = json.loads(next(item for item in hardened.artifacts if item.path.endswith("codebuild-project.json")).content)
    assert len(parsed["description"]) <= 255 and parsed["description"].endswith("...")
    assert parsed["environment"]["privilegedMode"] is True
    short = GeneratedArtifact(kind="codebuild_project", path="deploy/codebuild-project.json", purpose="x", content='{"name": "p", "description": "short", "environment": {"privilegedMode": true}}')
    assert suite.with_manifest(PipelineSuite(platform="aws", runner="aws_codebuild", artifacts=[short]), {"tier": "dev", "platform": "aws"}, []).artifacts[0].content == short.content
    broken = GeneratedArtifact(kind="codebuild_project", path="deploy/codebuild-project.json", purpose="x", content='{not json, "privilegedMode": false')
    assert '"privilegedMode": true' in suite.with_manifest(PipelineSuite(platform="aws", runner="aws_codebuild", artifacts=[broken]), {"tier": "dev", "platform": "aws"}, []).artifacts[0].content


def test_the_rendered_project_template_description_fits_codebuild():
    environment = {"tier": "dev", "platform": "aws"}
    project = templates.render_template("codebuild_project", {"name": "pipeline", "family": "custom", "purpose": "x", "environment": environment}, environment, run_id="6" * 36, runner="aws_codebuild", placeholders=[])
    import json

    assert len(json.loads(project.replace("$CODEBUILD_PROJECT", "p").replace("$APP_NAME", "a").replace("$S3_BUCKET", "b").replace("$RUN_ID", "r").replace("$TIER", "dev").replace("$REGION", "us-east-1").replace("$CODEBUILD_ROLE_ARN", "arn"))["description"]) <= 200
