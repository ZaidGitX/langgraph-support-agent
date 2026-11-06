"""Live run 22 (2026-11-06): nine stages passed, the stack was created with the ECS service-linked
role in place, and the first task never started: ECS read the task definition's bare
``ValueFrom: "app/NAME"`` as an SSM parameter name ("not authorized to perform: ssm:GetParameters").
A Secrets Manager secret is bound by its ARN."""

from __future__ import annotations

import yaml

from orchestrator import policy, suite, templates

ENV = {"tier": "dev", "platform": "aws", "run_id": "r22", "region": "us-east-1"}
STAGES = [
    {"name": "build", "family": "build", "purpose": "compile", "depends_on": []},
    {"name": "deploy", "family": "deploy", "purpose": "deploy", "depends_on": ["build"]},
]


def test_task_secrets_are_bound_by_secrets_manager_arn(monkeypatch):
    monkeypatch.setattr(suite.credentials, "is_present", lambda name, env=None: name == "OPENAI_API_KEY")
    lines = suite._container_secret_lines("aws")
    assert lines.splitlines()[0].strip() == "Secrets:"
    binding = lines.splitlines()[1]
    assert binding.strip() == f'- {{Name: OPENAI_API_KEY, ValueFrom: !Sub "arn:aws:secretsmanager:${{AWS::Region}}:${{AWS::AccountId}}:secret:{templates.APP_NAME}/OPENAI_API_KEY"}}'
    assert f'ValueFrom: "{templates.APP_NAME}/' not in lines  # never a bare name: ECS reads that as an SSM parameter
    rendered = suite.render_skeletons("aws", STAGES, ENV, run_id="r22", placeholders=["AWS_REGION"])["deploy/ecs-stack.yaml"]
    assert "secret:langgraph-support-app/OPENAI_API_KEY" in rendered
    assert not policy.yaml_findings(rendered, "cloudformation")  # the tag inside the flow mapping parses
    document = yaml.load(rendered, Loader=policy._AnyTagLoader)
    container = document["Resources"]["TaskDefinition"]["Properties"]["ContainerDefinitions"][0]
    assert container["Secrets"][0]["Name"] == "OPENAI_API_KEY"
    assert suite.referenced_secrets([{"content": rendered}]) == ["OPENAI_API_KEY"]  # injection still copies exactly this name
    execution = document["Resources"]["ExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    assert any("secretsmanager:GetSecretValue" in str(statement.get("Action")) for statement in execution)
    from orchestrator import redaction
    from orchestrator.schemas import GeneratedArtifact

    assert redaction.redact(rendered) == rendered  # the ARN's ":secret:" segment is a reference, not an assignment
    assert redaction.redact("SNYK_TOKEN=not-a-real-value-123456") == f"SNYK_TOKEN={redaction.MARK}"  # a real assignment still is
    assert policy.check_artifact(GeneratedArtifact(path="deploy/ecs-stack.yaml", kind="cloudformation", purpose="stack", content=rendered)).ok
