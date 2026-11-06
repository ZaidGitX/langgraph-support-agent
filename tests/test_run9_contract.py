"""The platform contract (Sprint 4, after live run 8): code-written stage scripts travel with
every suite, the orchestrator re-asserts them after generation, a generated deploy, verify, or
image stage on CodeBuild gets a working floor, a skeleton returned unchanged is a policy error,
the network and credential files are handled before the first build, and the task gets the
application's secrets by name."""

from __future__ import annotations

import json
import os
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver

from orchestrator import generator, policy, suite, templates
from orchestrator.graph import _bound_secret_names, build_graph, machine_report
from orchestrator.providers import aws as aws_module
from orchestrator.providers.aws import AwsProvider, _holds_credentials, role_policy
from orchestrator.schemas import ArtifactSet, GeneratedArtifact, PipelineSuite
from orchestrator.state import initial_state
from tests.test_orchestrator import FakeJev, PlannedCloudAstra, _echo, drive
from tests.test_suite import _answers_for

ROOT = Path(__file__).resolve().parent.parent
STAGES = [
    {"name": "build", "family": "build", "purpose": "compile", "depends_on": []},
    {"name": "scanners", "family": "scan", "purpose": "scan", "depends_on": ["build"]},
    {"name": "deploy", "family": "deploy", "purpose": "deploy", "depends_on": ["build"]},
]
ENV = {"tier": "dev", "platform": "aws", "run_id": "r9"}


def test_contract_scripts_render_pass_policy_and_cover_every_workflow_scan():
    for key in templates.CONTRACT_SCRIPTS:
        text = templates.contract_script(key)
        assert "__" not in text.replace("__pycache__", ""), key  # every token substituted
        assert "set -euo pipefail" in text.splitlines()[1], key
        result = policy.check_artifact(GeneratedArtifact(path=templates.contract_path(key), kind="shell", purpose="contract", content=text))
        assert result.ok, (key, [f.message for f in result.findings if f.severity == "error"])
    scanners = templates.contract_script("scanners")
    for tool in ("semgrep", "pip-audit", "snyk", "trivy", "gitleaks", "sonar", "codeql"):
        assert tool in scanners
    assert "skipped" in scanners and "SNYK_TOKEN is not provided" in scanners and "--severity-threshold=high" in scanners
    assert templates.contract_images()["TRIVY_IMAGE"] in scanners
    deploy = templates.contract_script("deploy")
    assert "aws cloudformation deploy" in deploy and "CAPABILITY_NAMED_IAM" in deploy and "ecs wait services-stable" in deploy
    assert "describe-vpcs" in deploy and "bash ci/container.sh" in deploy and "state/app_url.txt" in deploy
    assert "WITH_CLOUD_CLIS=0" in templates.contract_script("container")
    assert "zap-baseline.py" in templates.contract_script("dast")


def test_every_suite_carries_the_contract_and_the_fixed_stages_call_it():
    for platform in ("jenkins", "gcp", "azure", "aws"):
        rendered = suite.render_skeletons(platform, STAGES, {"tier": "dev", "platform": platform}, run_id="r9", placeholders=["AWS_REGION"])
        for key in templates.CONTRACT_PLATFORM_NEUTRAL:
            assert templates.contract_path(key) in rendered, (platform, key)
        for key in templates.CONTRACT_AWS:
            assert (templates.contract_path(key) in rendered) == (platform == "aws"), (platform, key)
    aws = suite.render_skeletons("aws", STAGES, {"tier": "dev", "platform": "aws"}, run_id="r9", placeholders=["AWS_REGION"])
    assert "- bash ci/build.sh" in aws["ci/buildspec-build.yml"] and "- bash ci/deploy.sh" in aws["ci/buildspec-deploy.yml"]
    assert "deploy/ecs-stack.yaml" in aws and "ci/deploy.sh" in aws
    assert suite.command_hints("deploy", "gcp")[0].startswith("case") and suite.command_hints("deploy", "aws") == ["bash ci/deploy.sh"]
    assert suite.command_hints("scanners", "jenkins") == ["bash ci/scanners.sh"]


def test_the_orchestrator_puts_back_contract_files_the_model_changed_or_dropped():
    skeletons = suite.render_skeletons("aws", STAGES, ENV, run_id="r9", placeholders=["AWS_REGION"])
    kept = {
        "ci/buildspec-build.yml": skeletons["ci/buildspec-build.yml"].replace("      - bash ci/build.sh\n", "      - echo compiled\n"),
        "ci/buildspec-scanners.yml": skeletons["ci/buildspec-scanners.yml"],
        "ci/buildspec-deploy.yml": skeletons["ci/buildspec-deploy.yml"],
        "deploy/codebuild-project.json": skeletons["deploy/codebuild-project.json"],
        "ci/scanners.sh": "#!/usr/bin/env bash\nset -euo pipefail\necho 'the model rewrote the scanners'\n",
    }
    model = PipelineSuite(
        platform="aws", runner="aws_codebuild", notes="model output",
        artifacts=[GeneratedArtifact(path=path, kind=policy.kind_for_path(path), purpose="model", content=text) for path, text in kept.items()],
    )
    fixed = suite.with_manifest(model, ENV, ["AWS_REGION"], platform="aws", stages=STAGES)
    by_path = {item.path: item for item in fixed.artifacts}
    assert by_path["ci/scanners.sh"].content == templates.contract_script("scanners")
    assert by_path["ci/deploy.sh"].content == templates.contract_script("deploy")
    assert "deploy/ecs-stack.yaml" in by_path and "ci/smoke.sh" in by_path
    assert "bash ci/build.sh" in by_path["ci/buildspec-build.yml"].content and "echo compiled" not in by_path["ci/buildspec-build.yml"].content  # live run 23: the skeleton, nothing beside the call
    assert suite.check(fixed).ok


def test_generated_deploy_verify_and_image_stages_get_a_floor_on_codebuild():
    placeholder = templates.render_template("buildspec", {"name": "deploy_dev", "family": "deploy", "purpose": "deploy"}, ENV, runner="aws_codebuild", run_id="r9", placeholders=[])
    assert policy.placeholder_findings(GeneratedArtifact(path="ci/buildspec-deploy_dev.yml", kind="buildspec", purpose="p", content=placeholder))
    deploy_stage = {"name": "deploy_dev", "family": "deploy", "runner": "aws_codebuild", "environment": ENV}
    floored = generator.platform_floor([GeneratedArtifact(path="ci/buildspec-deploy_dev.yml", kind="buildspec", purpose="p", content=placeholder)], deploy_stage)
    spec = next(item for item in floored if item.kind == "buildspec")
    assert "bash ci/deploy.sh" in spec.content and not policy.placeholder_findings(spec) and "orchestrator floor" in spec.purpose
    # No buildspec at all: one is created around the contract call.
    verify_stage = {"name": "post_deploy_smoke", "family": "verify", "runner": "aws_codebuild", "environment": ENV}
    created = generator.platform_floor([GeneratedArtifact(path="ci/smoke.generated.py", kind="script", purpose="p", content="print('x')\n")], verify_stage)
    spec = next(item for item in created if item.kind == "buildspec")
    assert spec.path == "ci/buildspec-post_deploy_smoke.yml" and "bash ci/smoke.sh" in spec.content and not policy.placeholder_findings(spec)
    # A deploy buildspec that mutates the stack itself loses that command and gets the contract call
    # (live run 12: the model's change set lacked every parameter); one that does nothing gets the call appended.
    real = "version: 0.2\nphases:\n  build:\n    commands:\n      - aws cloudformation deploy --stack-name x --template-file deploy/ecs-stack.yaml\n"
    taken_over = generator.platform_floor([GeneratedArtifact(path="ci/buildspec-deploy_dev.yml", kind="buildspec", purpose="p", content=real)], deploy_stage)[0].content
    assert "bash ci/deploy.sh" in taken_over and "cloudformation deploy" not in taken_over
    own = "version: 0.2\nphases:\n  build:\n    commands:\n      - bash ci/deploy.sh\n"
    assert "bash ci/deploy.sh" in generator.platform_floor([GeneratedArtifact(path="ci/buildspec-deploy_dev.yml", kind="buildspec", purpose="p", content=own)], deploy_stage)[0].content
    lazy = "version: 0.2\nphases:\n  build:\n    commands:\n      - echo deploying\n"
    appended = generator.platform_floor([GeneratedArtifact(path="ci/buildspec-build_image.yml", kind="buildspec", purpose="p", content=lazy)], {"name": "build_image", "family": "container", "runner": "aws_codebuild", "environment": ENV})
    # Since live run 17 the floored buildspec is the skeleton calling the contract and nothing else.
    assert "bash ci/container.sh" in appended[0].content and "echo deploying" not in appended[0].content
    # Other runners and other families are untouched.
    local_stage = {"name": "deploy_dev", "family": "deploy", "runner": "local", "environment": ENV}
    assert generator.platform_floor([GeneratedArtifact(path="ci/x.sh", kind="shell", purpose="p", content="echo\n")], local_stage)[0].content == "echo\n"
    assert generator.floor_for({"name": "sbom", "family": "scan"}) is None and generator.floor_for({"name": "push_image", "family": "custom"}) == "container"


def test_a_placeholder_is_a_policy_error_on_model_output_but_comments_are_not():
    flagged = ArtifactSet(stage="x", artifacts=[GeneratedArtifact(path="ci/x.sh", kind="shell", purpose="p", content='#!/usr/bin/env bash\nset -euo pipefail\necho "stage x placeholder"\n')])
    result = policy.check_artifact_set(flagged, {"name": "x"})
    assert not result.ok and any(f.rule == "artifact.placeholder" for f in result.findings)
    commented = GeneratedArtifact(path="ci/x.sh", kind="shell", purpose="p", content="#!/usr/bin/env bash\nset -euo pipefail\n# --- ASTRA: steps ---\npytest -q\n")
    assert policy.placeholder_findings(commented) == []
    suite_obj = PipelineSuite(platform="aws", runner="aws_codebuild", notes="n", artifacts=[GeneratedArtifact(path="ci/buildspec-build.yml", kind="buildspec", purpose="p", content='version: 0.2\nphases:\n  build:\n    commands:\n      - echo "stage build placeholder"\n')])
    assert not suite.check(suite_obj).ok


def test_bound_secret_names_come_from_content_and_records():
    records = [
        {"path": "ci/buildspec-scanners.yml", "content": f'    SNYK_TOKEN: "{templates.APP_NAME}/SNYK_TOKEN"\n', "secrets_referenced": []},
        {"path": "deploy/ecs-stack.yaml", "content": f'            - {{Name: OPENAI_API_KEY, ValueFrom: "{templates.APP_NAME}/OPENAI_API_KEY"}}\n', "secrets_referenced": []},
        {"path": "ci/x.sh", "content": "", "secrets_referenced": ["HAWK_API_KEY", "AWS_REGION"]},
    ]
    assert _bound_secret_names(records) == ["HAWK_API_KEY", "OPENAI_API_KEY", "SNYK_TOKEN"]


def test_the_task_definition_gets_the_app_secrets_it_can_have(monkeypatch):
    # Presence comes from the host (.env is reloaded by is_present): pin it for the test.
    monkeypatch.setattr(suite.credentials, "is_present", lambda name, env=None: name == "OPENAI_API_KEY")
    lines = suite._container_secret_lines("aws")
    assert "Secrets:" in lines and f'ValueFrom: !Sub "arn:aws:secretsmanager:${{AWS::Region}}:${{AWS::AccountId}}:secret:{templates.APP_NAME}/OPENAI_API_KEY"' in lines and "TYPESAFE" not in lines
    rendered = suite.render_skeletons("aws", STAGES, ENV, run_id="r9", placeholders=["AWS_REGION"])["deploy/ecs-stack.yaml"]
    assert "RoleName: !Sub \"${AppName}-${Tier}-task-execution\"" in rendered and "secretsmanager:GetSecretValue" in rendered
    assert f"{templates.APP_NAME}/OPENAI_API_KEY" in rendered
    assert suite.referenced_secrets([{"content": rendered}]) == ["OPENAI_API_KEY"]
    monkeypatch.setattr(suite.credentials, "is_present", lambda name, env=None: False)
    assert suite._container_secret_lines("aws") == ""
    assert "Secrets:" not in suite.render_skeletons("aws", STAGES, ENV, run_id="r9", placeholders=[])["deploy/ecs-stack.yaml"]


def test_project_document_and_role_policy_carry_what_the_builds_need():
    project = json.loads(templates.render_template("codebuild_project", {"name": "pipeline"}, ENV, runner="aws_codebuild", run_id="r9"))
    env_names = {item["name"] for item in project["environment"]["environmentVariables"]}
    assert {"AWS_VPC_ID", "AWS_SUBNET_IDS", "AWS_CFN_STACK", "AWS_ECS_CLUSTER", "AWS_ECS_SERVICE", "AWS_ACCOUNT_ID"} <= env_names
    assert project["environment"]["computeType"] == "BUILD_GENERAL1_MEDIUM" and project["timeoutInMinutes"] == 30
    assert AwsProvider.timeout == 1800
    actions = {action for statement in role_policy("1" * 12, "us-east-1", bucket="b", repository="r", project="p")["Statement"] for action in statement["Action"]}
    assert {"codebuild:CreateReportGroup", "codebuild:BatchPutTestCases", "ecr:DescribeImages", "ecr:BatchDeleteImage"} <= actions


def test_credential_files_never_travel_in_the_source_upload(tmp_path):
    for name in (".env", ".env.local", ".env.dev", "id_rsa", "server.pem", "secrets.enc"):
        assert _holds_credentials(name), name
    for name in (".env.example", ".env.dev.example", "main.py", "requirements.txt", "stackhawk.yml"):
        assert not _holds_credentials(name), name
    checkout = tmp_path / "checkout"
    (checkout / "app").mkdir(parents=True)
    (checkout / "app" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (checkout / ".env").write_text("OPENAI_API_KEY=unit-placeholder\n", encoding="utf-8")
    (checkout / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    (checkout / "deploy.pem").write_text("not a key\n", encoding="utf-8")
    import zipfile

    AwsProvider().prepare_provision([], str(checkout), {"run_id": "r9"}, bundle=[])
    with zipfile.ZipFile(checkout / "var" / "generated" / "r9" / "pipeline" / "source.zip") as archive:
        names = set(archive.namelist())
    assert "app/main.py" in names and ".env.example" in names
    assert ".env" not in names and "deploy.pem" not in names


class NetworkFakeAws:
    def __init__(self, *, vpc="vpc-123", public=("subnet-a", "subnet-b")):
        self.vpc, self.public, self.calls = vpc, public, []

    def __call__(self, argv, cwd, timeout=120):
        self.calls.append(list(argv))
        if argv[1:3] == ["ec2", "describe-vpcs"]:
            return 0, {"Vpcs": [{"VpcId": self.vpc}] if self.vpc else []}, ""
        if argv[1:3] == ["ec2", "describe-subnets"]:
            return 0, {"Subnets": [{"SubnetId": item} for item in self.public]}, ""
        return 0, {}, ""


def test_the_default_vpc_and_its_public_subnets_are_discovered_once(monkeypatch, tmp_path):
    for name in ("AWS_VPC_ID", "AWS_SUBNET_IDS"):
        monkeypatch.delenv(name, raising=False)
    provider = AwsProvider()
    fake = NetworkFakeAws()
    monkeypatch.setattr(provider, "_aws_json", fake)
    note = provider.discover_network(str(tmp_path), ["--region", "us-east-1"])
    assert note == "network: vpc-123 with 2 subnet(s)"
    assert os.environ["AWS_VPC_ID"] == "vpc-123" and os.environ["AWS_SUBNET_IDS"] == "subnet-a,subnet-b"
    fake.calls.clear()
    assert provider.discover_network(str(tmp_path), ["--region", "us-east-1"]) == "network: vpc-123 with 2 subnet(s)"
    assert fake.calls == []  # configured names are left alone
    monkeypatch.setenv("AWS_VPC_ID", "")
    monkeypatch.setenv("AWS_SUBNET_IDS", "")
    monkeypatch.setattr(provider, "_aws_json", NetworkFakeAws(vpc=""))
    assert provider.discover_network(str(tmp_path), ["--region", "us-east-1"]).startswith("network: no default VPC")


def test_a_generated_deploy_stage_on_codebuild_plans_its_build_with_the_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_DISPATCH", "platform")
    graph = build_graph(MemorySaver(), jev=FakeJev(), astra=PlannedCloudAstra(), commands=_echo())
    values = drive(graph, initial_state(str(tmp_path)), _answers_for("aws"), echo=False)
    records = values["generated_artifacts"]["deploy_dev"]
    spec = next(record for record in records if record["kind"] == "buildspec")
    assert spec["path"] == "ci/buildspec-deploy_dev.yml" and "orchestrator floor" in spec["purpose"]
    assert "bash ci/deploy.sh" in Path(spec["stored_at"]).read_text(encoding="utf-8")
    stage = next(row for row in values["pipeline"]["stages"] if row["name"] == "deploy_dev")
    assert stage["runner"] == "aws_codebuild"
    evidence = next(row for row in machine_report(values)["stage_evidence"] if row["stage"] == "deploy_dev")
    assert evidence["status"] == "planned"
    would = " ".join(" ".join(argv) for argv in (evidence.get("plan") or {}).get("would_run") or [])
    assert "codebuild start-build" in would and "buildspec-deploy_dev.yml" in would and "cloudformation" not in would
    assert "ci/deploy.sh" in {record["path"] for record in values["generated_artifacts"]["pipeline"]}


def test_teardown_removes_report_groups_and_can_remove_the_app_secrets():
    text = (ROOT / "scripts" / "aws_teardown.sh").read_text(encoding="utf-8")
    assert "delete-report-group" in text and "secretsmanager delete-secret" in text and "--force-delete-without-recovery" in text
    assert aws_module.LOG_TAIL_LINES == 60
