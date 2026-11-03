"""HTTP session the React UI and the tests both use."""

from __future__ import annotations

import asyncio
import hmac
import json
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from orchestrator import connect, credentials, vault
from orchestrator.session import SessionService


def require_token(request: Request) -> None:
    """Mutating routes need X-Orchestrator-Token when ORCHESTRATOR_UI_TOKEN is set.

    The push workflow always sets one, because the tunnel URL it prints is public.
    """
    expected = os.getenv("ORCHESTRATOR_UI_TOKEN", "").strip()
    if not expected:
        return
    given = request.headers.get("x-orchestrator-token", "")
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="a valid X-Orchestrator-Token header is required")


def token_required() -> bool:
    return bool(os.getenv("ORCHESTRATOR_UI_TOKEN", "").strip())


router = APIRouter(prefix="/api/orchestrator")
mutating = APIRouter(prefix="/api/orchestrator", dependencies=[Depends(require_token)])


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


@mutating.post("/session")
def start_session(request: Request) -> dict:
    return _service(request).start()


@mutating.post("/session/answer")
def answer_session(body: AnswerBody, request: Request) -> dict:
    try:
        return _service(request).submit(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/session/events")
def get_session_events(request: Request, since: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)) -> dict:
    """Timeline events with seq > since: node, stage, wave, jev, gate, autopilot, note."""
    return _service(request).events_since(since, limit)


@router.get("/session/stream")
async def stream_session(request: Request, max_changes: int = Query(0, ge=0, le=100000, alias="max")) -> StreamingResponse:
    """Server-sent events: one `change` frame per published view (seq, phase, running stages).

    The dashboard refetches the view on each frame; polling stays as the fallback. `max`
    ends the stream after that many frames (tests). A comment line every 15 s keeps proxies
    from closing an idle stream.
    """
    service = _service(request)

    async def generate():
        yield "retry: 2000\n\n"
        last = -1
        sent = 0
        idle = 0.0
        while True:
            if await request.is_disconnected():
                return
            seq = service.view_seq()
            if seq != last:
                last = seq
                snapshot = service.snapshot()
                payload = {
                    "seq": seq,
                    "run_id": snapshot.get("run_id"),
                    "phase": snapshot.get("phase"),
                    "status": snapshot.get("status"),
                    "gate": snapshot.get("gate"),
                    "running_stages": list(snapshot.get("running_stages") or []),
                    "trace_length": len(snapshot.get("trace") or []),
                }
                yield f"event: change\ndata: {json.dumps(payload)}\n\n"
                sent += 1
                idle = 0.0
                if max_changes and sent >= max_changes:
                    return
            else:
                idle += 0.25
                if idle >= 15:
                    idle = 0.0
                    yield ": ping\n\n"
            await asyncio.sleep(0.25)

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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


@mutating.post("/session/viewed")
def mark_session_viewed(request: Request) -> dict:
    try:
        return _service(request).mark_viewed()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@mutating.post("/public-url")
def set_public_url(body: PublicUrlBody, request: Request) -> dict:
    try:
        return _service(request).set_public_url(body.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# --- Connections (phase 8): names only in every response --------------------------------


class SecretsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: dict[str, str] = Field(min_length=1)


class InputBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=512)


def _connection_view(key: str, group: dict, status: dict, stored: set[str]) -> dict:
    env_rows = []
    for item in group.get("env") or []:
        name = item["name"]
        in_vault = name in stored
        present = credentials.is_present(name)
        env_rows.append(
            {
                "name": name,
                "kind": item.get("kind") or "config",
                "required": bool(item.get("required")),
                "purpose": item.get("purpose") or "",
                "set": bool(present or in_vault),
                "source": "vault" if in_vault else ("env" if present else "unset"),
            }
        )
    can_login, reason = connect.can_login(key)
    login = group.get("login") or {}
    return {
        "key": key,
        "label": status.get("label") or key,
        "cli_name": status.get("cli_name") or "",
        "cli": status.get("cli"),
        "mode": status.get("mode"),
        "runners": list(status.get("runners") or []),
        "present": list(status.get("present") or []),
        "missing": list(status.get("missing") or []),
        "alternatives": list(status.get("alternatives") or []),
        "authenticated": status.get("authenticated"),
        "checked_at": status.get("checked_at") or "",
        "env": env_rows,
        "vault": sorted(name for name in stored if any(row["name"] == name for row in env_rows)),
        "login": {"available": can_login, "reason": reason, "note": login.get("note") or "", "input": login.get("input") or ""},
        "verify": bool(group.get("verify")),
        "docs": group.get("docs") or "",
    }


def connections_view() -> dict:
    """Every provider group: CLI, names set (by source), verified state, login availability."""
    readiness = credentials.readiness()
    try:
        stored = set(vault.names())
    except vault.VaultError:
        stored = set()
    groups = []
    for key, group in credentials.groups().items():
        if key in credentials.APP_GROUPS:
            continue
        status = readiness["providers"].get(key) or credentials.group_status(key)
        groups.append(_connection_view(key, group, status, stored))
    return {"generated_at": readiness["generated_at"], "token_required": token_required(), "groups": groups}


def _group_or_404(key: str) -> dict:
    group = credentials.groups().get(key)
    if group is None or key in credentials.APP_GROUPS:
        raise HTTPException(status_code=404, detail=f"no provider group {key!r}")
    return group


@router.get("/connections")
def get_connections() -> dict:
    return connections_view()


@mutating.post("/connections/{key}/secrets")
def put_connection_secrets(key: str, body: SecretsBody) -> dict:
    """Write-only: store values for this group's names. The response never carries a value."""
    group = _group_or_404(key)
    allowed = {item["name"] for item in group.get("env") or []}
    unknown = sorted(name for name in body.values if name not in allowed)
    if unknown:
        raise HTTPException(status_code=400, detail=f"{', '.join(unknown)} do not belong to {key}")
    try:
        stored = vault.set_many(body.values)
    except vault.VaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    connect.forget(key)
    return {"stored": stored, "connection": next(item for item in connections_view()["groups"] if item["key"] == key)}


@mutating.delete("/connections/{key}/secrets/{name}")
def delete_connection_secret(key: str, name: str) -> dict:
    group = _group_or_404(key)
    if name not in {item["name"] for item in group.get("env") or []}:
        raise HTTPException(status_code=404, detail=f"{name} does not belong to {key}")
    removed = vault.delete(name)
    connect.forget(key)
    return {"removed": removed, "connection": next(item for item in connections_view()["groups"] if item["key"] == key)}


@mutating.post("/connections/{key}/login")
def start_connection_login(key: str) -> dict:
    _group_or_404(key)
    try:
        return {"flow": connect.start_login(key)}
    except connect.ConnectError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/connections/{key}/login/{flow_id}")
def get_connection_login(key: str, flow_id: str) -> dict:
    _group_or_404(key)
    try:
        flow = connect.flow_status(flow_id)
    except connect.ConnectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if flow["group"] != key:
        raise HTTPException(status_code=404, detail="no such login flow")
    return {"flow": flow}


@mutating.post("/connections/{key}/login/{flow_id}/input")
def input_connection_login(key: str, flow_id: str, body: InputBody) -> dict:
    _group_or_404(key)
    try:
        return {"flow": connect.provide_input(flow_id, body.text)}
    except connect.ConnectError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@mutating.post("/connections/{key}/login/{flow_id}/cancel")
def cancel_connection_login(key: str, flow_id: str) -> dict:
    _group_or_404(key)
    try:
        return {"flow": connect.cancel(flow_id)}
    except connect.ConnectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@mutating.post("/connections/{key}/verify")
def verify_connection(key: str) -> dict:
    _group_or_404(key)
    try:
        result = connect.verify(key)
    except connect.ConnectError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"verified": result, "connection": next(item for item in connections_view()["groups"] if item["key"] == key)}
