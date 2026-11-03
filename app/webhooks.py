"""Push webhooks (Sprint 4, phase 10): a signed push starts a run on a fresh workspace.

``POST /api/orchestrator/hooks/push`` accepts GitHub, GitLab, Gitea/Gogs, Bitbucket, and a
generic ``{clone_url, ref, sha}`` body. Every request must be signed with
``ORCHESTRATOR_WEBHOOK_SECRET`` (HMAC-SHA256 or the GitLab token header); an unsigned push
is refused, and a missing secret refuses everything. The dashboard token does not apply
here: the signature is the credential.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

from fastapi import APIRouter, HTTPException, Request

from orchestrator import profile, workspace

router = APIRouter(prefix="/api/orchestrator/hooks")


def _hex_hmac(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(headers, body: bytes, secret: str) -> str:
    """Which host signed the request. Raises 401 when nothing valid is present."""
    expected = _hex_hmac(secret, body)
    github = headers.get("x-hub-signature-256", "")
    if github:
        if hmac.compare_digest(github, "sha256=" + expected):
            return "github"
        raise HTTPException(status_code=401, detail="bad X-Hub-Signature-256")
    gitlab = headers.get("x-gitlab-token", "")
    if gitlab:
        if hmac.compare_digest(gitlab.encode(), secret.encode()):
            return "gitlab"
        raise HTTPException(status_code=401, detail="bad X-Gitlab-Token")
    for header, name in (("x-gitea-signature", "gitea"), ("x-gogs-signature", "gogs"), ("x-orchestrator-signature", "generic")):
        given = headers.get(header, "")
        if given:
            if hmac.compare_digest(given.lower(), expected):
                return name
            raise HTTPException(status_code=401, detail=f"bad {header}")
    bitbucket = headers.get("x-hub-signature", "")
    if bitbucket:
        if hmac.compare_digest(bitbucket, "sha256=" + expected):
            return "bitbucket"
        raise HTTPException(status_code=401, detail="bad X-Hub-Signature")
    raise HTTPException(status_code=401, detail="the push is not signed")


def parse_push(payload: dict, host: str) -> dict:
    """``{clone_url, ref, sha, repo}`` from the host's push payload. Raises 400 when unusable."""
    clone_url = ""
    ref = ""
    sha = ""
    repo = ""
    repository = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    project = payload.get("project") if isinstance(payload.get("project"), dict) else {}
    if host == "gitlab" or project:
        clone_url = str(project.get("git_http_url") or project.get("http_url") or project.get("git_ssh_url") or "")
        ref = str(payload.get("ref") or "")
        sha = str(payload.get("checkout_sha") or payload.get("after") or "")
        repo = str(project.get("path_with_namespace") or project.get("name") or "")
    elif host == "bitbucket" or "push" in payload:
        changes = ((payload.get("push") or {}).get("changes") or []) if isinstance(payload.get("push"), dict) else []
        new = (changes[0].get("new") or {}) if changes and isinstance(changes[0], dict) else {}
        ref = f"refs/heads/{new.get('name')}" if new.get("name") else ""
        sha = str(((new.get("target") or {}).get("hash")) or "")
        links = ((repository.get("links") or {}).get("clone") or []) if isinstance(repository.get("links"), dict) else []
        for link in links:
            if isinstance(link, dict) and link.get("name") == "https":
                clone_url = str(link.get("href") or "")
        repo = str(repository.get("full_name") or "")
    elif repository:
        clone_url = str(repository.get("clone_url") or repository.get("html_url") or repository.get("ssh_url") or "")
        ref = str(payload.get("ref") or "")
        sha = str(payload.get("after") or payload.get("head_commit", {}).get("id", "") if isinstance(payload.get("head_commit"), dict) else payload.get("after") or "")
        repo = str(repository.get("full_name") or repository.get("name") or "")
    else:
        clone_url = str(payload.get("clone_url") or payload.get("url") or "")
        ref = str(payload.get("ref") or "")
        sha = str(payload.get("sha") or payload.get("after") or "")
        repo = str(payload.get("repo") or "")
    if not clone_url:
        raise HTTPException(status_code=400, detail="the payload names no clone URL")
    if sha and set(sha) <= {"0"}:
        raise HTTPException(status_code=400, detail="the push deleted the ref; nothing to run")
    return {"clone_url": clone_url, "ref": ref, "sha": sha, "repo": repo}


def _watched_branches(service) -> list[str]:
    try:
        loaded = profile.load(service.repo_root)
    except profile.ProfileError:
        return []
    return list(loaded.remotes.branches) if loaded else []


@router.post("/push", status_code=202)
async def push(request: Request) -> dict:
    secret = os.getenv("ORCHESTRATOR_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="ORCHESTRATOR_WEBHOOK_SECRET is not set; unsigned pushes are refused")
    body = await request.body()
    host = verify(request.headers, body, secret)
    event = (request.headers.get("x-github-event") or request.headers.get("x-gitea-event") or request.headers.get("x-gogs-event") or "").lower()
    if event == "ping":
        return {"ok": True, "event": "ping", "host": host}
    if event and event != "push":
        return {"ok": True, "event": event, "skipped": "only push events start a run"}
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="the body is not JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="the body must be an object")
    push_info = parse_push(payload, host)
    service = getattr(request.app.state, "sessions", None)
    if service is None:
        raise HTTPException(status_code=503, detail="orchestrator session is not configured")
    branch = workspace.branch_of(push_info["ref"])
    watched = _watched_branches(service)
    if watched and branch and branch not in watched:
        return {"ok": True, "skipped": f"branch {branch} is not in remotes.branches", "host": host}
    try:
        prepared = workspace.prepare(push_info["clone_url"], push_info["ref"], push_info["sha"])
    except workspace.WorkspaceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    trigger = {
        "source": "webhook",
        "host": host,
        "repo": push_info["repo"] or push_info["clone_url"],
        "ref": push_info["ref"],
        "sha": prepared["sha"],
        "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    view = service.start(repo_root=prepared["path"], trigger=trigger)
    queued = [item for item in (view.get("queue") or []) if item.get("sha") == prepared["sha"]]
    return {
        "ok": True,
        "host": host,
        "run_id": None if queued else view.get("run_id"),
        "queued": len(view.get("queue") or []),
        "workspace": prepared["path"],
        "sha": prepared["sha"],
    }
