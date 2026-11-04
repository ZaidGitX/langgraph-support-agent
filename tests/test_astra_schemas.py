"""Every Astra output model must survive OpenAI strict structured output.

The first live AWS run (2026-11-03) lost its platform suite because ``PipelineSuite`` carried a
free-form mapping (``dict[str, str]``): strict mode needs every object to list its properties
and require them all. The check below walks the schema the OpenAI client sends and fails on
anything strict mode would refuse, before a key is ever spent on it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel

from orchestrator import llm
from orchestrator.schemas import (
    ArtifactSet,
    CheckAdviceSet,
    DeployDoc,
    Diagnosis,
    FollowUpSet,
    PipelineSuite,
    PlanProposal,
    QuestionSet,
    StageBrief,
    WorkerPlan,
)

ASTRA_MODELS = [QuestionSet, FollowUpSet, PlanProposal, ArtifactSet, PipelineSuite, DeployDoc, StageBrief, Diagnosis, CheckAdviceSet, WorkerPlan]


def strict_problems(node, path: str = "$") -> list[str]:
    """What OpenAI strict mode would refuse in this (already converted) JSON schema."""
    problems: list[str] = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            properties = node.get("properties")
            if not isinstance(properties, dict):
                problems.append(f"{path}: an object without properties (a free-form mapping)")
            else:
                if node.get("additionalProperties") is not False:
                    problems.append(f"{path}: additionalProperties must be false")
                if sorted(node.get("required") or []) != sorted(properties):
                    problems.append(f"{path}: required must list every property")
        for key, value in node.items():
            if key in {"properties", "$defs", "definitions"} and isinstance(value, dict):
                for name, child in value.items():
                    problems.extend(strict_problems(child, f"{path}.{name}"))
            elif key in {"items", "additionalProperties"} and isinstance(value, dict):
                problems.extend(strict_problems(value, f"{path}[{key}]"))
            elif key in {"anyOf", "oneOf", "allOf", "prefixItems"} and isinstance(value, list):
                for index, child in enumerate(value):
                    problems.extend(strict_problems(child, f"{path}[{key}][{index}]"))
    return problems


@pytest.mark.parametrize("model", ASTRA_MODELS, ids=lambda model: model.__name__)
def test_astra_output_models_are_strict_compatible(model):
    assert strict_problems(to_strict_json_schema(model)) == []


def test_the_check_catches_a_free_form_mapping():
    class Mapping(BaseModel):
        files: dict[str, str]

    assert any("free-form" in item for item in strict_problems(to_strict_json_schema(Mapping)))


def test_every_structured_astra_call_uses_a_checked_model():
    text = Path(llm.__file__).read_text(encoding="utf-8")
    used = set(re.findall(r"invoke_structured\(\s*config\.astra_model\(\),\s*(\w+)", text))
    assert used, "no structured Astra call found"
    assert used <= {model.__name__ for model in ASTRA_MODELS}


def test_astra_timeout_reads_the_toggle_and_clamps(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_ASTRA_TIMEOUT", raising=False)
    assert llm.astra_timeout() == 300.0
    for raw, expected in (("45", 45.0), ("5", 30.0), ("5000", 900.0), ("not-a-number", 300.0)):
        monkeypatch.setenv("ORCHESTRATOR_ASTRA_TIMEOUT", raw)
        assert llm.astra_timeout() == expected
