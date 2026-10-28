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
from typing import TypeVar

from langchain_core.exceptions import LangChainException
from langchain_openai import ChatOpenAI
from langchain_typesafe import Choice, Noul, NoulCriteria, TypeSafeClassifier
from openai import OpenAIError
from pydantic import BaseModel, ValidationError

from app import config

from . import redaction
from .registry import RUNNERS
from .schemas import (
    ArtifactReview,
    ArtifactSet,
    CheckAdviceSet,
    ClarifyDecision,
    DeployDoc,
    Diagnosis,
    FollowUpDecision,
    FollowUpSet,
    GateDecision,
    PlanProposal,
    QuestionSet,
    RankDecision,
    RunnerDecision,
    StageBrief,
)
from .state import MAX_CLARIFY_ROUNDS

SchemaT = TypeVar("SchemaT", bound=BaseModel)

CLIENT_NAME = "langchain_typesafe.TypeSafeClassifier"
DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"

PLAN_SYSTEM = (
    "You are Astra. Propose two to four different stage DAGs for this repository. Stage names "
    "come from stage_menu, or are new snake_case names when no template fits; name the family "
    "for every stage. Propose at least one deterministic candidate that uses only the fixed "
    "stages build, test, scanners, dast, package, container, release, deploy, uat, and at least "
    "one stochastic candidate that adds generated stages (for example container_scan with trivy, "
    "sbom with syft, deploy_dev on aws_codebuild, post_deploy_smoke) suited to the answers, the "
    "platform, and the retrieved code. Every candidate includes build, test, and uat; uat is last "
    "and depends on the stage before it. No production stage and nothing after uat. Pick a "
    "runner for each stage from runners_available; when the readiness note says credentials are "
    "missing, still propose the stage and say it will run in plan mode. Prior episodes tell you "
    "what ran and failed before; prefer routes that fixed earlier failures. For a fixed stage do "
    "not write commands; for a generated stage list the artifacts you intend to create (kind, "
    "path, purpose) without their content."
)

FOLLOW_UP_SYSTEM = (
    "You are Astra. The person answered the first round. Using their answers and the retrieved "
    "excerpts, ask at most three follow-up questions that change which stages, runner, "
    "provider, or environment the pipeline should use. Skip anything already answered. Use "
    "short snake_case ids. Do not answer the questions. Do not write shell."
)

GENERATE_SYSTEM = (
    "You are Astra. Write the complete files one generated CI/CD stage needs for the given "
    "runner, provider, and environment. Start from the skeletons provided and keep them valid "
    "for their format (GitHub Actions YAML, Jenkinsfile, CodeBuild buildspec, Cloud Build "
    "config, Azure Pipelines YAML, Dockerfile, Compose, or a bash script with set -euo pipefail). "
    "Use the suggested_paths for each kind. Reference credentials only by the placeholder names "
    "provided, as ${{ secrets.NAME }} in GitHub Actions, $NAME or ${NAME} elsewhere; never write "
    "a value. Never reference production. Never use rm -rf on / or $HOME, curl piped to a shell, "
    "sudo, chmod 777, or --force on main. Pin every GitHub action to a tag or SHA. A GitHub "
    "workflow triggers only on workflow_dispatch. Print machine-readable summary lines so "
    "findings can be extracted. When policy_findings are present, fix every one of them and "
    "change nothing else. Return full file contents for every artifact."
)

DOCS_SYSTEM = (
    "You are Astra. Write a short deployment document for this pipeline and environment: what "
    "runs where, which placeholder variables must be set and where (GitHub secrets, .env, CI "
    "variables), how to run the generated artifacts, and how to roll back. Use only the "
    "artifact paths and placeholder names given. Do not include any secret value."
)

QUESTION_SYSTEM = (
    "You are Astra. Write the questions a human must answer before a CI/CD "
    "pipeline can be planned for this checkout. Use the retrieved code excerpts and "
    "prior episodes to avoid asking what the repository or an earlier run already "
    "answers. Ask only about gaps that are not already answered, at most six. When a "
    "topic is open, use these ids exactly: env_target (local, dev, staging, or uat; never "
    "production), env_platform (local, docker, github, jenkins, aws, gcp, azure, remote, "
    "kubernetes), runner_preference, test_depth, scan_policy, release_policy (this system "
    "stops at UAT and has no production edge). Do not answer the questions. Do not "
    "propose a pipeline. Do not invent shell commands. Use short snake_case ids."
)

BRIEF_SYSTEM = (
    "You are Astra. Write a short brief for one designated CI stage before it runs. "
    "summary is one or two sentences on what this stage is for in this repository. "
    "watch_for is one sentence on what a failure would mean. "
    "Do not include shell commands, scripts, or argv."
)

DIAGNOSE_SYSTEM = (
    "You are Astra. Diagnose the failed stage evidence in one short summary "
    "and one sentence of retry advice. Use the check findings when they are "
    "present. Do not invent a vulnerability, file, or package that is not in "
    "the evidence. Do not invent a production deploy and do not invent shell commands."
)

ADVISE_SYSTEM = (
    "You are Astra. Each check already has extracted findings from one test or scan. "
    "For each check, findings must restate only what the extract says. "
    "next_step is one sentence the engineer should take. "
    "If the extract has no finding, next_step says no change is required. "
    "Do not invent a CVE, path, package, or command that is not in the extract."
)

def scrub(text: str) -> str:
    """Remove credential material from text that may be printed or stored.

    Every ``kind: secret`` name in the credential registry and every credential-shaped
    token is covered; see ``orchestrator/redaction.py``.
    """
    return redaction.redact(text)


def _client(model_name: str, temperature: float | None = None) -> ChatOpenAI:
    api_key = config.get_openai_api_key()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Add it to langgraph_support_app/.env "
            "or export it before running the orchestrator demo."
        )
    kwargs: dict = {
        "model": model_name,
        "api_key": api_key,
        "use_responses_api": True,
        "timeout": 120,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    return ChatOpenAI(**kwargs)


def astra_temperature() -> float:
    """Sampling temperature for proposals and generation (ORCHESTRATOR_ASTRA_TEMPERATURE)."""
    raw = os.getenv("ORCHESTRATOR_ASTRA_TEMPERATURE", "0.7").strip()
    try:
        value = float(raw)
    except ValueError:
        return 0.7
    return min(2.0, max(0.0, value))


def invoke_structured(
    model_name: str,
    schema: type[SchemaT],
    system: str,
    payload: dict,
    *,
    temperature: float | None = None,
) -> SchemaT:
    """One Astra structured-output call. Retried once on a bad payload.

    Some reasoning models reject ``temperature``; when the error says so the call is retried
    once more without it, so a stochastic proposal degrades to the model default instead of
    failing the run.
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, default=str)},
    ]
    last_error: Exception | None = None
    call_errors = (OSError, RuntimeError, ValueError, TypeError, OpenAIError, LangChainException)
    attempts = 2
    use_temperature = temperature
    while attempts > 0:
        attempts -= 1
        model = _client(model_name, use_temperature).with_structured_output(schema)
        try:
            raw = model.invoke(messages)
        except call_errors as exc:
            last_error = exc
            if use_temperature is not None and "temperature" in str(exc).lower():
                use_temperature = None
                attempts += 1
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
            environment = item.get("environment") or {}
            criteria[ident] = (
                f"{summary} Mode: {item.get('mode') or 'deterministic'}. "
                f"Environment: {environment.get('tier') or 'local'} on {environment.get('platform') or 'local'}. "
                f"Stages: {names}. Readiness: {item.get('readiness_note') or 'runs now'}. "
                "Prefer a pipeline that builds, tests, and stops at uat. Prefer a runnable route "
                "over a plan-only route unless the person chose that platform. "
                "There is no production stage."
            )
        response = self._invoke(
            payload,
            {
                "pipeline": Choice(
                    instructions=(
                        "Choose exactly one candidate pipeline id. "
                        "Prefer build, test, and a stop at UAT. A stochastic candidate is "
                        "acceptable when its generated stages serve the answers. "
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
                            "Every required stage passed, was an explained skip, or is planned "
                            "because credentials for its provider are missing."
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

    def follow_up(self, payload: dict) -> FollowUpDecision:
        """Noul: is more context needed? Choice: proceed or ask one more round."""
        rounds = int(payload.get("rounds") or 0)
        max_rounds = int(payload.get("max_rounds") or MAX_CLARIFY_ROUNDS)
        response = self._invoke(
            payload,
            {
                "needs_more_context": Noul(
                    instructions=(
                        "Do the answers plus the retrieved excerpts still leave the runner, "
                        "platform, scan policy, or deploy target ambiguous for this checkout?"
                    ),
                    criteria=NoulCriteria(
                        true="A stage, runner, provider, or environment choice cannot be made without another answer.",
                        false="The answers and the retrieved excerpts are enough to plan the pipeline.",
                    ),
                ),
                "action": Choice(
                    instructions=(
                        "Choose ask_more only when a short follow-up round would change the "
                        "pipeline. Otherwise choose proceed."
                    ),
                    criteria={
                        "proceed": "Plan now with the answers and retrieved context.",
                        "ask_more": "Ask up to three follow-up questions before planning.",
                    },
                ),
            },
        )
        try:
            noul = response.nouls["needs_more_context"]
            choice = response.choices["action"]
        except (AttributeError, KeyError, TypeError) as exc:
            raise RuntimeError(
                f"TypeSafeClassifier follow_up response was missing Noul or Choice: {scrub(str(exc))}"
            ) from exc
        self._stamp(response, ["Noul", "Choice"], {"needs_more_context": noul.noul}, {"action": _choice_view(choice)})
        if choice.choice not in {"proceed", "ask_more"}:
            raise RuntimeError(f"TypeSafe follow_up Choice returned {choice.choice!r}, which is not proceed or ask_more")
        action = choice.choice
        rationale = (
            f"TypeSafe Choice action={action} confidence={choice.confidence:.3f}; "
            f"Noul needs_more_context={noul.noul:.3f}; model={response.model}; request_id={response.request_id}"
        )
        if action == "ask_more" and rounds >= max_rounds:
            action = "proceed"
            rationale += " | guardrail: clarify rounds exhausted"
        return FollowUpDecision(action=action, rationale=rationale)

    def runner(self, payload: dict) -> RunnerDecision:
        """One Choice per stage, keyed runner:<stage>, with readiness facts as the criteria."""
        available = payload.get("runners_available") or {}
        platform_runners = set(payload.get("platform_runners") or [])
        decided: dict[str, str] = {}
        questions: dict = {}
        for stage in payload.get("stages") or []:
            name = str(stage.get("name") or "")
            if not name:
                continue
            options = {}
            for runner in stage.get("runner_options") or ["local"]:
                if runner not in RUNNERS:
                    continue
                info = available.get(runner) or {}
                missing = list(info.get("missing") or [])
                options[runner] = (
                    f"{runner}: cli {'present' if info.get('cli', True) else 'absent'}; "
                    f"credentials {'complete' if not missing else 'missing ' + ', '.join(missing)}; "
                    f"would run in {info.get('mode') or 'plan'} mode. "
                    + ("Matches the platform the person chose. " if runner in platform_runners else "")
                )
            if name == "uat" or not options:
                decided[name] = "local"
            elif len(options) == 1:
                decided[name] = next(iter(options))
            else:
                questions[f"runner:{name}"] = Choice(
                    instructions=(
                        "Choose the runner for this stage. Prefer a runner that runs now over "
                        "plan-only, unless the person chose that platform explicitly."
                    ),
                    criteria=options,
                )
        views: dict = {}
        model = config.jev_model()
        request_id = None
        if questions:
            response = self._invoke(payload, questions)
            for key in questions:
                try:
                    choice = response.choices[key]
                except (AttributeError, KeyError, TypeError) as exc:
                    raise RuntimeError(
                        f"TypeSafeClassifier runner response was missing {key}: {scrub(str(exc))}"
                    ) from exc
                if choice.choice not in RUNNERS:
                    raise RuntimeError(f"TypeSafe runner Choice returned {choice.choice!r}, which is not a runner")
                decided[key.split(":", 1)[1]] = choice.choice
                views[key] = _choice_view(choice)
            self._stamp(response, ["Choice"], {}, views)
            model = response.model
            request_id = response.request_id
        else:
            self.last_call = {
                "model": model,
                "client": CLIENT_NAME,
                "endpoint": self._endpoint(),
                "primitives": [],
                "noul": {},
                "choice": {},
                "request_id": None,
            }
        decided["uat"] = "local" if "uat" in decided else decided.get("uat", "local")
        return RunnerDecision(
            runners=decided,
            rationale=(
                f"TypeSafe Choice runners={decided}; questions={len(questions)}; "
                f"model={model}; request_id={request_id}"
            ),
        )

    def artifact_review(self, payload: dict) -> ArtifactReview:
        """Choice: accept, revise, or reject one stage's generated artifacts from the policy result."""
        policy = payload.get("policy") or {}
        ok = bool(policy.get("ok"))
        revisions_left = int(payload.get("revisions_left") or 0)
        response = self._invoke(
            payload,
            {
                "verdict": Choice(
                    instructions="Review the policy result for the generated artifacts of one stage.",
                    criteria={
                        "accept": "Policy reported no errors. Warnings only, or none.",
                        "revise": (
                            "Policy reported fixable errors (secret literal, unpinned action, "
                            "missing permissions, missing set -euo pipefail) and a revision is still available."
                        ),
                        "reject": (
                            "Policy reported a production reference, a destructive command, a path "
                            "outside the allowed directories, or revisions are exhausted."
                        ),
                    },
                )
            },
        )
        try:
            choice = response.choices["verdict"]
        except (AttributeError, KeyError, TypeError) as exc:
            raise RuntimeError(
                f"TypeSafeClassifier artifact_review response was missing Choice: {scrub(str(exc))}"
            ) from exc
        self._stamp(response, ["Choice"], {}, {"verdict": _choice_view(choice)})
        if choice.choice not in {"accept", "revise", "reject"}:
            raise RuntimeError(f"TypeSafe artifact_review Choice returned {choice.choice!r}")
        verdict = choice.choice
        rationale = (
            f"TypeSafe Choice verdict={verdict} confidence={choice.confidence:.3f}; "
            f"model={response.model}; request_id={response.request_id}"
        )
        return guard_review(ArtifactReview(verdict=verdict, rationale=rationale), ok, revisions_left)


def guard_review(review: ArtifactReview, ok: bool, revisions_left: int) -> ArtifactReview:
    """Code wins over opinion: accept needs a clean policy, revise needs a revision left."""
    verdict = review.verdict
    rationale = review.rationale
    if verdict == "accept" and not ok:
        verdict = "revise" if revisions_left > 0 else "reject"
        rationale += " | guardrail: policy errors cannot be accepted"
    if verdict == "revise" and revisions_left <= 0:
        verdict = "reject"
        rationale += " | guardrail: revisions exhausted"
    return ArtifactReview(verdict=verdict, rationale=rationale)


class OpenAIAstra:
    """Questions, plans, briefs, failure diagnosis, and scan next steps. Not used for routing.

    Proposals are sampled at ``temperature`` (default ``ORCHESTRATOR_ASTRA_TEMPERATURE``,
    0.7) so two runs over the same checkout can explore different routes; questions, briefs,
    advice, and diagnosis stay at the model default.
    """

    def __init__(self, temperature: float | None = None) -> None:
        self.temperature = astra_temperature() if temperature is None else temperature

    def write_questions(self, payload: dict) -> QuestionSet:
        return invoke_structured(config.astra_model(), QuestionSet, QUESTION_SYSTEM, payload)

    def propose(self, payload: dict) -> PlanProposal:
        return invoke_structured(
            config.astra_model(), PlanProposal, PLAN_SYSTEM, payload, temperature=self.temperature
        )

    def follow_up(self, payload: dict) -> FollowUpSet:
        return invoke_structured(config.astra_model(), FollowUpSet, FOLLOW_UP_SYSTEM, payload)

    def generate_artifacts(self, payload: dict) -> ArtifactSet:
        return invoke_structured(
            config.astra_model(), ArtifactSet, GENERATE_SYSTEM, payload, temperature=self.temperature
        )

    def write_docs(self, payload: dict) -> DeployDoc:
        return invoke_structured(config.astra_model(), DeployDoc, DOCS_SYSTEM, payload)

    def brief(self, payload: dict) -> StageBrief:
        return invoke_structured(config.astra_model(), StageBrief, BRIEF_SYSTEM, payload)

    def diagnose(self, payload: dict) -> Diagnosis:
        return invoke_structured(config.astra_model(), Diagnosis, DIAGNOSE_SYSTEM, payload)

    def advise(self, payload: dict) -> CheckAdviceSet:
        return invoke_structured(config.astra_model(), CheckAdviceSet, ADVISE_SYSTEM, payload)
