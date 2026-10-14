"""Model bindings.

Jev routes. Each edge is one ``langchain_typesafe.TypeSafeClassifier`` call
to the TypeSafe Jev model (default ``jev-latest``) at
``https://api.typesafe.ai/v1/systemone``. The classifier reads
``TYPESAFE_API_KEY``. A failed call is raised. There is no ChatOpenAI
fallback for routing.

Astra (``OPENAI_MODEL``, gpt-6-astra in this app) writes ecosystem
questions, proposes candidate DAGs, briefs a stage before it runs, and
diagnoses failures. Astra does not choose the edge and does not invent
shell commands.
"""

from __future__ import annotations

import json
import os
import re
from typing import TypeVar

from langchain_core.exceptions import LangChainException
from langchain_openai import ChatOpenAI
from langchain_typesafe import Choice, Noul, NoulCriteria, TypeSafeClassifier
from openai import OpenAIError
from pydantic import BaseModel, ValidationError

from app import config

from .schemas import (
    ClarifyDecision,
    Diagnosis,
    GateDecision,
    PlanProposal,
    QuestionSet,
    RankDecision,
    StageBrief,
)

SchemaT = TypeVar("SchemaT", bound=BaseModel)

CLIENT_NAME = "langchain_typesafe.TypeSafeClassifier"
DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"

PLAN_SYSTEM = (
    "You are Astra. Propose two or three different stage DAGs for this "
    "repository. Stage names must be build, test, scanners, dast, package, "
    "container, release, deploy, or uat. Every candidate includes build, "
    "test, and uat. uat is the last stage and depends on the stage before it. "
    "No production stage and no stage after uat. Do not invent shell commands. "
    "The designated workflow or the fixed local command list will run; you only choose the DAG."
)

QUESTION_SYSTEM = (
    "You are Astra. Write the questions a human must answer before a CI/CD "
    "pipeline can be planned for this checkout. Ask only about gaps that are "
    "not already answered. When those topics are still open, cover the target "
    "environment (local, dev, staging, or uat — never production), the test "
    "depth, and the release policy (this system stops at UAT and has no "
    "production edge). Do not answer the questions. Do not propose a pipeline. "
    "Do not invent shell commands. Use short snake_case ids."
)

BRIEF_SYSTEM = (
    "You are Astra. Write a short brief for one designated CI stage before it runs. "
    "summary is one or two sentences on what this stage is for in this repository. "
    "watch_for is one sentence on what a failure would mean. "
    "Do not include shell commands, scripts, or argv."
)

DIAGNOSE_SYSTEM = (
    "You are Astra. Diagnose the failed stage evidence in one short summary "
    "and one sentence of retry advice. Do not invent a production deploy "
    "and do not invent shell commands."
)

_SECRET_ENVS = (
    "TYPESAFE_API_KEY",
    "OPENAI_API_KEY",
    "SONAR_TOKEN",
    "HAWK_API_KEY",
    "HAWK_APP_ID",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "SNYK_TOKEN",
)

_SECRET_MARK = "api" + "key_"


def scrub(text: str) -> str:
    """Remove credential material from text that may be printed or stored."""
    cleaned = text or ""
    for name in _SECRET_ENVS:
        secret = os.getenv(name, "")
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    cleaned = re.sub(_SECRET_MARK + r"[A-Za-z0-9_]+", "[redacted]", cleaned)
    cleaned = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "[redacted]", cleaned)
    cleaned = re.sub(r"hawk\.[A-Za-z0-9]{8,}\.[A-Za-z0-9_\-]{8,}", "[redacted]", cleaned)
    cleaned = re.sub(r"snyk_uat\.[A-Za-z0-9._\-]+", "[redacted]", cleaned)
    cleaned = re.sub(r"(?i)(api[_-]?key\s*[=:]\s*)\S+", r"\1[redacted]", cleaned)
    return cleaned


def _client(model_name: str) -> ChatOpenAI:
    api_key = config.get_openai_api_key()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Add it to langgraph_support_app/.env "
            "or export it before running the orchestrator demo."
        )
    return ChatOpenAI(
        model=model_name,
        api_key=api_key,
        use_responses_api=True,
        timeout=90,
    )


def invoke_structured(model_name: str, schema: type[SchemaT], system: str, payload: dict) -> SchemaT:
    """One Astra structured-output call. Retried once on a bad payload."""
    model = _client(model_name).with_structured_output(schema)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, default=str)},
    ]
    last_error: Exception | None = None
    call_errors = (OSError, RuntimeError, ValueError, TypeError, OpenAIError, LangChainException)
    for _ in range(2):
        try:
            raw = model.invoke(messages)
        except call_errors as exc:
            last_error = exc
            continue
        if isinstance(raw, schema):
            return raw
        try:
            return schema.model_validate(raw)
        except ValidationError as exc:
            last_error = exc
    raise RuntimeError(f"{model_name} structured output failed: {scrub(str(last_error))}")


def _choice_view(answer) -> dict:
    return {
        "choice": answer.choice,
        "confidence": answer.confidence,
        "probabilities": dict(answer.probabilities),
    }


class TypeSafeJev:
    """Edge governor. Each method is one TypeSafe Jev classification."""

    def __init__(self, classifier: TypeSafeClassifier | None = None) -> None:
        self._classifier = classifier
        self.last_call: dict = {}

    def _endpoint(self) -> str:
        base = getattr(self._classifier, "base_url", None) or "https://api.typesafe.ai"
        return f"{str(base).rstrip('/')}/v1/systemone"

    def _ensure(self) -> TypeSafeClassifier:
        if self._classifier is None:
            config.load_env_files()
            if not config.get_typesafe_api_key():
                raise RuntimeError(
                    "TYPESAFE_API_KEY is not set. Add it to this repo's .env "
                    "or export it before running the orchestrator."
                )
            self._classifier = TypeSafeClassifier(model=config.jev_model(), timeout=90.0)
        return self._classifier

    def _invoke(self, state: dict, questions: dict):
        classifier = self._ensure()
        try:
            return classifier.invoke({"state": state, "questions": questions})
        except Exception as exc:
            message = scrub(f"{type(exc).__name__}: {exc}")
            raise RuntimeError(f"TypeSafeClassifier call failed: {message}") from exc

    def _stamp(self, response, primitives: list[str], noul: dict, choice: dict) -> None:
        self.last_call = {
            "model": getattr(response, "model", None) or config.jev_model(),
            "client": CLIENT_NAME,
            "endpoint": self._endpoint(),
            "primitives": primitives,
            "noul": noul,
            "choice": choice,
            "request_id": getattr(response, "request_id", None),
        }

    def clarify(self, payload: dict) -> ClarifyDecision:
        response = self._invoke(
            payload,
            {
                "needs_human": Noul(
                    instructions=(
                        "Are there open questions a human must answer before "
                        "a CI/CD pipeline can be planned?"
                    ),
                    criteria=NoulCriteria(
                        true="open_questions contains at least one question.",
                        false="open_questions is empty.",
                    ),
                ),
                "action": Choice(
                    instructions=(
                        "Choose skip when open_questions is empty. "
                        "Choose ask when any question is still open. "
                        "Do not answer the questions."
                    ),
                    criteria={
                        "skip": "No open questions. Continue to planning.",
                        "ask": "At least one open question remains. Pause for a human.",
                    },
                ),
            },
        )
        try:
            noul = response.nouls["needs_human"]
            choice = response.choices["action"]
        except (AttributeError, KeyError, TypeError) as exc:
            raise RuntimeError(
                f"TypeSafeClassifier clarify response was missing Noul or Choice: {scrub(str(exc))}"
            ) from exc
        self._stamp(
            response,
            ["Noul", "Choice"],
            {"needs_human": noul.noul},
            {"action": _choice_view(choice)},
        )
        if choice.choice not in {"skip", "ask"}:
            raise RuntimeError(
                f"TypeSafe clarify Choice returned {choice.choice!r}, which is not skip or ask"
            )
        return ClarifyDecision(
            action=choice.choice,
            rationale=(
                f"TypeSafe Choice action={choice.choice} "
                f"confidence={choice.confidence:.3f}; "
                f"Noul needs_human={noul.noul:.3f}; "
                f"model={response.model}; request_id={response.request_id}"
            ),
        )

    def rank(self, payload: dict) -> RankDecision:
        candidates = payload.get("candidates") or []
        ids = [str(item.get("id")) for item in candidates if item.get("id")]
        if not ids:
            raise RuntimeError("TypeSafe rank requires at least one candidate pipeline id")
        criteria = {}
        for item in candidates:
            ident = str(item.get("id") or "")
            if not ident:
                continue
            names = ", ".join(
                str(stage.get("name") or "") for stage in (item.get("stages") or [])
            )
            summary = str(item.get("summary") or ident)
            criteria[ident] = (
                f"{summary} Stages: {names}. "
                "Prefer a pipeline that builds, tests, and stops at uat. "
                "There is no production stage."
            )
        response = self._invoke(
            payload,
            {
                "pipeline": Choice(
                    instructions=(
                        "Choose exactly one candidate pipeline id. "
                        "Prefer build, test, and a stop at UAT. "
                        "Do not choose a path past UAT."
                    ),
                    criteria=criteria,
                )
            },
        )
        try:
            choice = response.choices["pipeline"]
        except (AttributeError, KeyError, TypeError) as exc:
            raise RuntimeError(
                f"TypeSafeClassifier rank response was missing Choice: {scrub(str(exc))}"
            ) from exc
        self._stamp(
            response,
            ["Choice"],
            {},
            {"pipeline": _choice_view(choice)},
        )
        ordered = [choice.choice] + [ident for ident in ids if ident != choice.choice]
        return RankDecision(
            chosen_id=choice.choice,
            ordered_ids=ordered,
            drop_stages=[],
            rationale=(
                f"TypeSafe Choice pipeline={choice.choice} "
                f"confidence={choice.confidence:.3f}; "
                f"model={response.model}; request_id={response.request_id}"
            ),
        )

    def gate(self, payload: dict) -> GateDecision:
        response = self._invoke(
            payload,
            {
                "route": Choice(
                    instructions=(
                        "Choose the quality-gate route from the stage evidence. "
                        "Never invent a production or post-UAT route."
                    ),
                    criteria={
                        "pass": (
                            "Every required stage passed or was an explained skip."
                        ),
                        "skip": (
                            "The only gaps are scanner tools recorded as skipped "
                            "because the binary is not installed and the CI workflow is named."
                        ),
                        "remediate": "A failed stage should run again.",
                        "escalate": (
                            "The failure is a policy block, a production target, "
                            "or not something a retry can fix."
                        ),
                    },
                )
            },
        )
        try:
            choice = response.choices["route"]
        except (AttributeError, KeyError, TypeError) as exc:
            raise RuntimeError(
                f"TypeSafeClassifier gate response was missing Choice: {scrub(str(exc))}"
            ) from exc
        self._stamp(
            response,
            ["Choice"],
            {},
            {"route": _choice_view(choice)},
        )
        if choice.choice not in {"pass", "remediate", "escalate", "skip"}:
            raise RuntimeError(
                f"TypeSafe gate Choice returned {choice.choice!r}, "
                "which is not pass, remediate, escalate, or skip"
            )
        failures = [str(name) for name in (payload.get("failures") or [])]
        retry = choice.choice == "remediate"
        return GateDecision(
            route=choice.choice,
            retry=retry,
            retry_stages=failures if retry else [],
            replan=False,
            rationale=(
                f"TypeSafe Choice route={choice.choice} "
                f"confidence={choice.confidence:.3f}; "
                f"model={response.model}; request_id={response.request_id}"
            ),
        )


class OpenAIAstra:
    """Questions, plan synthesis, stage briefs, and failure diagnosis. Not used for routing."""

    def write_questions(self, payload: dict) -> QuestionSet:
        return invoke_structured(config.astra_model(), QuestionSet, QUESTION_SYSTEM, payload)

    def propose(self, payload: dict) -> PlanProposal:
        return invoke_structured(config.astra_model(), PlanProposal, PLAN_SYSTEM, payload)

    def brief(self, payload: dict) -> StageBrief:
        return invoke_structured(config.astra_model(), StageBrief, BRIEF_SYSTEM, payload)

    def diagnose(self, payload: dict) -> Diagnosis:
        return invoke_structured(config.astra_model(), Diagnosis, DIAGNOSE_SYSTEM, payload)
