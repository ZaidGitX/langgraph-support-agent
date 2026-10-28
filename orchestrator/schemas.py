"""Typed schemas for Jev routing and Astra planning.

Jev fills routing models from `langchain_typesafe.TypeSafeClassifier`
answers (`Noul` and `Choice`). Astra uses OpenAI structured output for
questions, candidate DAGs, stage briefs, failure diagnosis, and
next steps for test or scan findings. Astra
schemas have no command or answer fields.

Sprint 3 opens the stage vocabulary. A stage name is a registry name or a
validated custom slug (`orchestrator/registry.py`), and every stage carries a
runner and an environment. The environment tier has no production value.
Generated stages declare artifact specs; the generated file content is a
separate model (`GeneratedArtifact`) that the policy checks before approval.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, Field, ValidationInfo, model_validator

from . import registry
from .state import STAGE_NAMES

# Kept for old imports. A stage name is now any registry name or custom slug.
StageName = str

StageFamily = Literal[
    "build",
    "test",
    "scan",
    "package",
    "container",
    "release",
    "deploy",
    "verify",
    "uat",
    "custom",
]
Runner = Literal[
    "local",
    "docker",
    "github_actions",
    "jenkins",
    "aws_codebuild",
    "gcp_cloudbuild",
    "azure_pipelines",
    "ssh_remote",
    "kubernetes",
]
Platform = Literal[
    "local",
    "docker",
    "github",
    "jenkins",
    "aws",
    "gcp",
    "azure",
    "remote",
    "kubernetes",
]
# Production is not a choice.
Tier = Literal["local", "dev", "staging", "uat"]
ArtifactKind = Literal[
    "github_workflow",
    "jenkinsfile",
    "buildspec",
    "cloudbuild",
    "azure_pipeline",
    "shell",
    "dockerfile",
    "compose",
    "env_example",
    "doc",
    "kubernetes_manifest",
    "script",
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


class EnvironmentSpec(BaseModel):
    tier: Tier = "local"
    platform: Platform = "local"
    region: str = Field(default="", description="For example us-east-1. Empty for local.")
    notes: str = ""


class ArtifactSpec(BaseModel):
    kind: ArtifactKind
    path: str = Field(description="Relative path inside the checkout where the file would live.")
    purpose: str


class GeneratedArtifact(ArtifactSpec):
    content: str = Field(
        description="Full file contents. Secrets only as ${{ secrets.NAME }}, $NAME, or ${NAME}.",
    )
    secrets_referenced: list[str] = Field(default_factory=list)


class StageSpec(BaseModel):
    name: str = Field(
        description=(
            "A stage name from the stage menu, or a custom snake_case slug "
            "(3 to 41 characters). Never a production target."
        ),
    )
    family: StageFamily = "custom"
    kind: Literal["fixed", "generated"] = "fixed"
    runner: Runner = "local"
    provider: str = Field(default="", description="Scanner or provider key. Empty for fixed stages.")
    environment: EnvironmentSpec = Field(default_factory=EnvironmentSpec)
    depends_on: list[str] = Field(default_factory=list)
    purpose: str
    artifacts: list[ArtifactSpec] = Field(
        default_factory=list,
        description="Files this stage needs. Intent only; the content is generated later.",
    )
    stochastic: bool = False

    @model_validator(mode="before")
    @classmethod
    def _derive_from_registry(cls, data: object) -> object:
        """The registry owns the name, family, and kind. `uat` is always fixed and local."""
        if not isinstance(data, dict) or "name" not in data:
            return data
        derived = dict(data)
        slug = registry.validate_stage_name(derived["name"])
        derived["name"] = slug
        derived["family"] = registry.family_of(slug)
        derived["kind"] = "fixed" if registry.is_fixed(slug) else "generated"
        deps = derived.get("depends_on")
        if isinstance(deps, (list, tuple)):
            derived["depends_on"] = [
                item.strip().lower() if isinstance(item, str) else item for item in deps
            ]
        if slug == "uat":
            derived["runner"] = "local"
        return derived


class CandidatePipeline(BaseModel):
    id: str
    summary: str
    mode: Literal["deterministic", "stochastic"] = "deterministic"
    rationale: str = ""
    environment: EnvironmentSpec = Field(default_factory=EnvironmentSpec)
    stages: list[StageSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _uat_rules(self, info: ValidationInfo) -> Self:
        """`uat` appears at most once, comes last, and depends on an earlier stage.

        A candidate with no `uat` is accepted, because the Sprint 2 doubles build short
        pipelines. Validate with context={"require_uat": True} to demand one.
        """
        names = [stage.name for stage in self.stages]
        if "uat" not in names:
            context = info.context if isinstance(info.context, dict) else {}
            if context.get("require_uat"):
                raise ValueError("a candidate must end with the uat stage")
            return self
        if names.count("uat") > 1:
            raise ValueError("a candidate may contain only one uat stage")
        if names[-1] != "uat":
            raise ValueError("uat must be the last stage of a candidate")
        if any("uat" in stage.depends_on for stage in self.stages[:-1]):
            raise ValueError("no stage may depend on uat; nothing runs after UAT sign-off")
        if not set(self.stages[-1].depends_on) & set(names[:-1]):
            raise ValueError("uat must depend on at least one other stage of the candidate")
        return self


class PlanProposal(BaseModel):
    candidates: list[CandidatePipeline] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def _ids_and_modes(self) -> Self:
        """Ids are unique and at least one candidate is the deterministic route.

        A missing stochastic candidate is allowed here; the graph logs it instead of failing.
        """
        ids = [item.id for item in self.candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("candidate ids must be unique")
        if not any(item.mode == "deterministic" for item in self.candidates):
            raise ValueError("at least one candidate must be deterministic")
        return self


class FollowUpSet(BaseModel):
    questions: list[EcosystemQuestion] = Field(default_factory=list, max_length=3)


class FollowUpDecision(BaseModel):
    action: Literal["proceed", "ask_more"]
    rationale: str


class RunnerDecision(BaseModel):
    runners: dict[str, Runner] = Field(description="Stage name to runner.")
    rationale: str


class ArtifactSet(BaseModel):
    stage: str
    artifacts: list[GeneratedArtifact] = Field(min_length=1, max_length=6)
    notes: str = ""


class ArtifactReview(BaseModel):
    verdict: Literal["accept", "revise", "reject"]
    rationale: str


class DeployDoc(BaseModel):
    path: str
    content: str


class PolicyFinding(BaseModel):
    rule: str
    severity: Literal["error", "warn"]
    message: str
    line: int = 0


class PolicyResult(BaseModel):
    ok: bool
    findings: list[PolicyFinding] = Field(default_factory=list)


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
