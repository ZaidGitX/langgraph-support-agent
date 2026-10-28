"""HTTP session the React UI and the tests both use."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from orchestrator import credentials
from orchestrator.session import SessionService

router = APIRouter(prefix="/api/orchestrator")


@router.get("/readiness")
def get_readiness() -> dict:
    """CLIs and credential NAMES present on this host; never a value. Works before a session."""
    return credentials.readiness()


class AnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gate: str | None = None
    answers: dict[str, str] | None = None
    decision: str | None = None
    feedback: str | None = None


class PublicUrlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)


def _service(request: Request) -> SessionService:
    service = getattr(request.app.state, "sessions", None)
    if service is None:
        raise HTTPException(status_code=503, detail="orchestrator session is not configured")
    return service


@router.get("/session")
def get_session(request: Request) -> dict:
    return _service(request).snapshot()


@router.post("/session")
def start_session(request: Request) -> dict:
    return _service(request).start()


@router.post("/session/answer")
def answer_session(body: AnswerBody, request: Request) -> dict:
    try:
        return _service(request).submit(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/session/artifacts")
def get_session_artifacts(request: Request) -> dict:
    """Every generated artifact of the current run keyed by stage; content is redacted and bounded."""
    return _service(request).artifacts()


@router.get("/session/artifacts/{stage}/{index}", response_class=PlainTextResponse)
def get_session_artifact_text(stage: str, index: int, request: Request) -> str:
    """Full redacted text of one generated artifact."""
    text = _service(request).artifact_text(stage, index)
    if text is None:
        raise HTTPException(status_code=404, detail=f"no artifact {index} for stage {stage!r}")
    return text


@router.get("/episodes")
def get_episodes(request: Request, limit: int = Query(5, ge=1, le=50)) -> list[dict]:
    """Newest recorded runs of this checkout (episodic memory). Works before a session starts."""
    return _service(request).episodes(limit)


@router.post("/session/viewed")
def mark_session_viewed(request: Request) -> dict:
    try:
        return _service(request).mark_viewed()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/public-url")
def set_public_url(body: PublicUrlBody, request: Request) -> dict:
    try:
        return _service(request).set_public_url(body.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
