"""The committed run profile (Sprint 4, phase 10): what autopilot may answer and approve.

``orchestrator.yaml`` at the checkout root says which platform and tier a push should
target, how Astra's questions are answered without a person (by question id, then by topic
keyword), and when a pipeline may be approved on its own: the policy is clean, every
artifact review accepted, and Jev's rank confidence clears a threshold. UAT sign-off is never
answered here. A question the profile cannot answer, or a pipeline that misses a bar, falls
back to the human gate exactly as before.
"""

from __future__ import annotations

import time
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import registry
from .state import ALLOWED_TARGETS, FORBIDDEN_TARGETS

PROFILE_FILE = "orchestrator.yaml"


class ProfileError(ValueError):
    """The profile file exists but cannot be used."""


class AnswerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by_id: dict[str, str] = Field(default_factory=dict, description="question id -> answer")
    by_topic: dict[str, str] = Field(default_factory=dict, description="keyword found in the id or prompt -> answer")


class AutopilotSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer_questions: bool = True
    approve: bool = True
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    require_policy_clean: bool = True
    require_review_accept: bool = True


class RemoteSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branches: list[str] = Field(default_factory=lambda: ["main"], description="branches a push or the poller may start; empty = any")
    webhook_secret_name: str = "ORCHESTRATOR_WEBHOOK_SECRET"
    poll_interval_seconds: int = Field(default=60, ge=10, le=3600)


class RunProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    platform: str = "local"
    tier: str = "dev"
    answers: AnswerSpec = Field(default_factory=AnswerSpec)
    autopilot: AutopilotSpec = Field(default_factory=AutopilotSpec)
    remotes: RemoteSpec = Field(default_factory=RemoteSpec)
    path: str = Field(default="", exclude=True)

    @field_validator("platform")
    @classmethod
    def _platform(cls, value: str) -> str:
        cleaned = (value or "local").strip().lower()
        if cleaned not in registry.PLATFORMS:
            raise ValueError(f"platform must be one of {', '.join(registry.PLATFORMS)}")
        return cleaned

    @field_validator("tier")
    @classmethod
    def _tier(cls, value: str) -> str:
        cleaned = (value or "dev").strip().lower()
        if cleaned in FORBIDDEN_TARGETS or cleaned not in ALLOWED_TARGETS:
            raise ValueError(f"tier must be one of {', '.join(ALLOWED_TARGETS)}")
        return cleaned

    @property
    def enabled(self) -> bool:
        return self.autopilot.answer_questions or self.autopilot.approve


def path_for(root: str | Path) -> Path:
    return Path(root) / PROFILE_FILE


def load(root: str | Path) -> RunProfile | None:
    """The profile of a checkout, or None when there is none. Invalid content raises."""
    path = path_for(root)
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileError(f"{PROFILE_FILE} cannot be read: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"{PROFILE_FILE} must be a mapping")
    try:
        profile = RunProfile.model_validate(data)
    except ValueError as exc:
        raise ProfileError(f"{PROFILE_FILE} is invalid: {exc}") from exc
    profile.path = str(path)
    return profile


def answer_for(profile: RunProfile, question: dict) -> str | None:
    """By id, then the platform and tier defaults, then by topic keyword. None when unknown."""
    ident = str(question.get("id") or "").strip()
    prompt = str(question.get("prompt") or "")
    if ident in profile.answers.by_id:
        return profile.answers.by_id[ident]
    lowered = f"{ident} {prompt}".lower()
    if ident == "env_platform":
        return profile.platform
    if ident in {"env_target", "env_tier"}:
        return profile.tier
    for topic, answer in profile.answers.by_topic.items():
        if topic.strip().lower() in lowered:
            return answer
    return None


def answers_for(profile: RunProfile, questions: list[dict]) -> tuple[dict[str, str], list[str]]:
    """Answers for every question the profile knows, and the ids it does not."""
    answers: dict[str, str] = {}
    unmatched: list[str] = []
    for question in questions or []:
        answer = answer_for(profile, question)
        ident = str(question.get("id") or "")
        if answer is None or not str(answer).strip():
            unmatched.append(ident)
        else:
            answers[ident] = str(answer).strip()
    return answers, unmatched


def rank_confidence(decisions: list[dict] | None) -> float | None:
    """Jev's confidence for the ranked pipeline, from the newest rank event; None if unreported."""
    for item in reversed(decisions or []):
        if item.get("kind") != "rank":
            continue
        choice = (item.get("choice") or {}).get("pipeline") if isinstance(item.get("choice"), dict) else None
        if isinstance(choice, dict) and isinstance(choice.get("confidence"), (int, float)):
            return float(choice["confidence"])
        return None
    return None


def _policy_clean(artifacts: dict | None) -> bool:
    for records in (artifacts or {}).values():
        for record in records or []:
            summary = record.get("policy") or {}
            if summary and not summary.get("ok", True):
                return False
    return True


def approval(profile: RunProfile, view: dict) -> tuple[bool, str]:
    """Whether autopilot may approve the pipeline the view shows, and why not when it may not."""
    spec = profile.autopilot
    if not spec.approve:
        return False, "autopilot.approve is off"
    if spec.require_policy_clean and not _policy_clean(view.get("artifacts")):
        return False, "an artifact has a policy error"
    if spec.require_review_accept:
        verdicts = {name: (item or {}).get("verdict") for name, item in (view.get("artifact_review") or {}).items()}
        rejected = [name for name, verdict in verdicts.items() if verdict and verdict != "accept"]
        if rejected:
            return False, "artifact review did not accept " + ", ".join(sorted(rejected))
    confidence = rank_confidence(view.get("jev_decisions"))
    if confidence is not None and confidence < spec.min_confidence:
        return False, f"Jev's confidence {confidence:.2f} is below {spec.min_confidence:.2f}"
    stages = [stage.get("name") for stage in (view.get("pipeline") or {}).get("stages") or []]
    if any(name in FORBIDDEN_TARGETS for name in stages):
        return False, "a stage names a forbidden target"
    return True, "policy clean, reviews accepted, confidence " + ("not reported" if confidence is None else f"{confidence:.2f}")


def event(gate: str, action: str, detail: str) -> dict:
    return {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "gate": gate, "action": action, "detail": detail}
