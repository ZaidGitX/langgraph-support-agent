"""Live run 16 (2026-11-06): six stages passed at the first attempt and the image was built and
pushed through the contract; the generated container_scan then looked for an image archive that no
stage produces. The image scan is a contract script now, and an image-scan stage on CodeBuild runs it."""

from __future__ import annotations

import subprocess

import yaml

from orchestrator import generator, llm, policy, suite, templates
from orchestrator.schemas import GeneratedArtifact

ENV = {"tier": "dev", "platform": "aws", "run_id": "r16"}
STAGES = [
    {"name": "build", "family": "build", "purpose": "compile", "depends_on": []},
    {"name": "scanners", "family": "scan", "purpose": "scan", "depends_on": ["build"]},
]


def test_image_scan_contract_renders_and_pulls_the_run_image(tmp_path):
    text = templates.contract_script("image_scan")
    assert "__" not in text.replace("__pycache__", "")
    for marker in ("docker pull", "--ignore-unfixed", 'imageTag="$RUN_ID"', "bash ci/container.sh", "image finding:", "ignore_unfixed"):
        assert marker in text, marker
    assert "image.tar" not in text and "docker save" not in text
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci" / "image_scan.sh").write_text(text)
    assert subprocess.run(["bash", "-n", "ci/image_scan.sh"], cwd=tmp_path, check=False, capture_output=True, text=True).returncode == 0
    assert policy.check_artifact(GeneratedArtifact(path="ci/image_scan.sh", kind="shell", purpose="contract", content=text)).ok
    assert "image_scan" in templates.CONTRACT_AWS and templates.contract_path("image_scan") == "ci/image_scan.sh"
    rendered = suite.render_skeletons("aws", STAGES, {"tier": "dev", "platform": "aws"}, run_id="r16", placeholders=["AWS_REGION"])
    assert "ci/image_scan.sh" in rendered
    assert "ci/image_scan.sh" not in suite.render_skeletons("jenkins", STAGES, {"tier": "dev", "platform": "jenkins"}, run_id="r16", placeholders=[])


def test_an_image_scan_stage_is_floored_to_the_contract():
    assert generator.floor_for({"name": "container_scan", "family": "scan", "provider": "trivy"}) == "image_scan"
    assert generator.floor_for({"name": "image_scan", "family": "scan"}) == "image_scan"
    assert generator.floor_for({"name": "sca", "family": "scan", "provider": "pip_audit"}) is None
    stage = {"name": "container_scan", "family": "scan", "provider": "trivy", "runner": "aws_codebuild", "environment": ENV}
    model = GeneratedArtifact(path="ci/buildspec-container_scan.yml", kind="buildspec", purpose="model", content=(
        "version: 0.2\nenv:\n  shell: bash\nphases:\n  build:\n    commands:\n"
        "      - set -euo pipefail\n"
        "      - aws s3 cp \"s3://$AWS_S3_BUCKET/ci-images/$RUN_ID/image.tar\" image.tar\n"
        "      - docker load --input image.tar\n"
        "      - trivy image --severity HIGH,CRITICAL --exit-code 1 app:latest\n"
        "      - bash ci/container_scan.sh\n"
        "      - echo scanned\n"
    ))
    commands = yaml.safe_load(generator.platform_floor([model], stage)[0].content)["phases"]["build"]["commands"]
    assert "bash ci/image_scan.sh" in commands and "echo scanned" in commands and "set -euo pipefail" in commands
    assert not any("image.tar" in c or "docker load" in c or "trivy image" in c or "ci/container_scan.sh" in c for c in commands)
    assert generator.platform_floor([model], {**stage, "runner": "local"})[0].content == model.content
    assert "bash ci/image_scan.sh" in llm.GENERATE_SYSTEM
