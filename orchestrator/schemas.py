"""Typed schemas for Jev routing and Astra planning.

Jev fills these models from `langchain_typesafe.TypeSafeClassifier`
answers (`Noul` and `Choice`). Astra still uses OpenAI structured output
for `PlanProposal` and `Diagnosis` only.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .state import STAGE_NAMES

StageName = Literal["build", "package", "container", "release", "deploy", "test", "uat"]


class ClarifyDecision(BaseModel):
    action: Literal["skip", "ask"] = Field(
        description="skip when open_questions is empty. ask when any question is still open.",
    )
    rationale: str


class StageSpec(BaseModel):
    name: StageName
    depends_on: list[str] = Field(default_factory=list)
    purpose: str


class CandidatePipeline(BaseModel):
    id: str
    summary: str
    stages: list[StageSpec] = Field(min_length=1)


class PlanProposal(BaseModel):
    candidates: list[CandidatePipeline] = Field(min_length=2, max_length=3)


class RankDecision(BaseModel):
    chosen_id: str = Field(description="Exactly one id from the candidate list.")
    ordered_ids: list[str]
    drop_stages: list[str] = Field(
        default_factory=list,
        description="Stage names to remove from the chosen candidate. Do not drop uat.",
    )
    rationale: str


class GateDecision(BaseModel):
    route: Literal["pass", "remediate", "escalate", "skip"]
    retry: bool = Field(
        description="True only when a failed stage should run again. False escalates.",
    )
    retry_stages: list[str] = Field(default_factory=list)
    replan: bool = False
    rationale: str


class Diagnosis(BaseModel):
    summary: str
    retry_advice: str


def stage_menu() -> list[str]:
    return list(STAGE_NAMES)
