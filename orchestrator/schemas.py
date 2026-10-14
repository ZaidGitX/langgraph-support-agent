"""Typed schemas for Jev routing and Astra planning.

Jev fills routing models from `langchain_typesafe.TypeSafeClassifier`
answers (`Noul` and `Choice`). Astra uses OpenAI structured output for
questions, candidate DAGs, stage briefs, failure diagnosis, and
next steps for test or scan findings. Astra
schemas have no command or answer fields.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .state import STAGE_NAMES

StageName = Literal[
    "build",
    "test",
    "scanners",
    "dast",
    "package",
    "container",
    "release",
    "deploy",
    "uat",
]


class EcosystemQuestion(BaseModel):
    id: str = Field(description="Short snake_case id. Not an answer.")
    prompt: str
    reason: str


class QuestionSet(BaseModel):
    questions: list[EcosystemQuestion] = Field(default_factory=list, max_length=6)


class StageBrief(BaseModel):
    summary: str
    watch_for: str


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
        description="Stage names to remove from the chosen candidate. Do not add stages.",
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


class CheckAdvice(BaseModel):
    name: str
    findings: str = Field(description="Only findings present in the extract. Do not add new ones.")
    next_step: str


class CheckAdviceSet(BaseModel):
    checks: list[CheckAdvice] = Field(default_factory=list)


def stage_menu() -> list[str]:
    return list(STAGE_NAMES)
