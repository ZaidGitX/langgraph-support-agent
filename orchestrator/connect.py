"""Platform connections (Sprint 4, phase 8): device-code logins and whoami checks.

A *login flow* runs the vendor CLI's device-code command without a TTY (``gh auth login
--web``, ``az login --use-device-code``, ``aws sso login --use-device-code``, ``gcloud auth
login --no-launch-browser``), parses the URL and the one-time code from its output, and
exposes them to the dashboard; the person authorizes in the vendor's browser page and the
CLI keeps the resulting credentials in its own store. Some flows (gcloud) need a
verification code pasted back; ``provide_input`` writes it to the process.

``verify`` runs the group's whoami command (or HTTP check) and caches ok / not ok with a
timestamp. ``credentials.group_status`` reads that cache, never the network. Output is
redacted before it is kept and only a short tail is ever returned.

``sync_secrets`` (phase 12) copies the values present here (vault, .env, environment) into a
platform's own secret store so a push-triggered run finds them: GitHub repository secrets
through ``gh secret set NAME`` (gh reads the value from stdin when ``--body`` is absent),
the value never on an argv.
``python -m orchestrator.connect sync-github`` does the same from a shell; both report names only.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from app import config

from . import credentials, redaction

LOGIN_TIMEOUT_SECONDS = 900
VERIFY_TIMEOUT_SECONDS = 20
MAX_FLOWS = 20
_TAIL_LINES = 12
_LOCK = threading.RLock()
_FLOWS: dict[str, LoginFlow] = {}
_VERIFIED: dict[str, dict] = {}
_PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}|\$([A-Z][A-Z0-9_]*)")


class ConnectError(ValueError):
    """A flow or check cannot start (unknown group, missing CLI or names, no such flow)."""


@dataclass
class LoginFlow:
    id: str
    group: str
    argv: list[str]
    status: str = "starting"  # starting | waiting | done | failed | expired
    url: str = ""
    code: str = ""
    input_label: str = ""
    input_given: bool = False
    exit_code: int | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    lines: list[str] = field(default_factory=list)
    process: subprocess.Popen | None = field(default=None, repr=False)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "group": self.group,
            "status": self.status,
            "url": self.url,
            "code": self.code,
            "needs_input": bool(self.input_label) and not self.input_given and self.status == "waiting",
            "input_label": self.input_label,
            "exit_code": self.exit_code,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at) if self.finished_at else "",
            "message": "\n".join(self.lines[-_TAIL_LINES:]),
        }


def _iso(moment: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))


def _expand(argv: list[str], env: dict[str, str] | None = None) -> list[str]:
    source = env if env is not None else os.environ

    def replace(match: re.Match) -> str:
        name = match.group(1) or match.group(2)
        return source.get(name, "")

    return [_PLACEHOLDER.sub(replace, item) for item in argv]


def _placeholders(argv: list[str]) -> list[str]:
    names: list[str] = []
    for item in argv:
        for match in _PLACEHOLDER.finditer(item):
            names.append(match.group(1) or match.group(2))
    return names


def login_spec(group: str) -> dict | None:
    item = credentials.groups().get(group) or {}
    spec = item.get("login")
    return dict(spec) if isinstance(spec, dict) and spec.get("argv") else None


def verify_spec(group: str) -> dict | None:
    item = credentials.groups().get(group) or {}
    spec = item.get("verify")
    return dict(spec) if isinstance(spec, dict) and (spec.get("argv") or spec.get("http")) else None


def can_login(group: str, env: dict[str, str] | None = None) -> tuple[bool, str]:
    """Whether a device-code flow can start now, and why not when it cannot."""
    spec = login_spec(group)
    if spec is None:
        return False, "this platform has no device-code login; use the vault form"
    cli = spec["argv"][0]
    if shutil.which(cli) is None:
        return False, f"the {cli} CLI is not installed on the orchestrator host"
    source = env if env is not None else os.environ
    missing = [name for name in spec.get("requires") or [] if not str(source.get(name, "")).strip()]
    if missing:
        return False, "set " + ", ".join(missing) + " first"
    return True, ""


def _subprocess_env(env: dict[str, str] | None = None) -> dict[str, str]:
    merged = dict(env if env is not None else os.environ)
    merged.update(
        {
            "NO_COLOR": "1",
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "AWS_PAGER": "",
            "AZURE_CORE_NO_COLOR": "true",
            "CLOUDSDK_CORE_DISABLE_PROMPTS": "0",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return merged


def start_login(group: str, env: dict[str, str] | None = None) -> dict:
    """Start the group's device-code flow in a subprocess and return its first status."""
    ok, reason = can_login(group, env)
    if not ok:
        raise ConnectError(reason)
    spec = login_spec(group) or {}
    argv = _expand(list(spec["argv"]), env)
    flow = LoginFlow(id=uuid.uuid4().hex[:12], group=group, argv=argv, input_label=str(spec.get("input") or ""))
    try:
        flow.process = subprocess.Popen(
            argv,
            cwd=str(config.PROJECT_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=_subprocess_env(env),
        )
    except OSError as exc:
        flow.status = "failed"
        flow.finished_at = time.time()
        flow.lines.append(redaction.redact(f"could not start {argv[0]}: {exc}"))
        _remember(flow)
        return flow.as_dict()
    _remember(flow)
    threading.Thread(target=_pump, args=(flow, spec), name=f"login-{group}", daemon=True).start()
    return flow.as_dict()


def _remember(flow: LoginFlow) -> None:
    with _LOCK:
        _FLOWS[flow.id] = flow
        while len(_FLOWS) > MAX_FLOWS:
            oldest = min(_FLOWS.values(), key=lambda item: item.started_at)
            if oldest.process and oldest.process.poll() is None:
                break
            del _FLOWS[oldest.id]


def _pump(flow: LoginFlow, spec: dict) -> None:
    url_pattern = re.compile(spec.get("url_pattern") or r"https://\S+")
    code_pattern = re.compile(spec["code_pattern"]) if spec.get("code_pattern") else None
    process = flow.process
    assert process is not None and process.stdout is not None
    deadline = flow.started_at + LOGIN_TIMEOUT_SECONDS
    try:
        for raw in process.stdout:
            line = redaction.redact(raw.rstrip("\n"))
            with _LOCK:
                flow.lines.append(line)
                if len(flow.lines) > 200:
                    del flow.lines[:-200]
                if not flow.url:
                    found = url_pattern.search(line)
                    if found:
                        flow.url = found.group(0).rstrip(".,")
                if code_pattern and not flow.code:
                    found = code_pattern.search(line)
                    if found and "http" not in found.group(0).lower():
                        flow.code = found.group(0)
                if flow.url and flow.status == "starting":
                    flow.status = "waiting"
            if time.time() > deadline:
                process.kill()
                with _LOCK:
                    flow.status = "expired"
                break
    finally:
        code = process.wait()
        with _LOCK:
            flow.exit_code = code
            flow.finished_at = time.time()
            if flow.status != "expired":
                flow.status = "done" if code == 0 else "failed"
    if flow.status == "done":
        try:
            verify(flow.group)
        except ConnectError:
            pass


def provide_input(flow_id: str, text: str) -> dict:
    """Write a verification code (or any prompt answer) to the login process."""
    flow = _flow(flow_id)
    process = flow.process
    if process is None or process.poll() is not None or process.stdin is None:
        raise ConnectError("this login is no longer waiting for input")
    try:
        process.stdin.write(text.strip() + "\n")
        process.stdin.flush()
    except (OSError, ValueError) as exc:
        raise ConnectError(f"the login process did not accept input: {type(exc).__name__}") from exc
    with _LOCK:
        flow.input_given = True
    return flow.as_dict()


def _flow(flow_id: str) -> LoginFlow:
    with _LOCK:
        flow = _FLOWS.get(flow_id)
    if flow is None:
        raise ConnectError("no such login flow")
    return flow


def flow_status(flow_id: str) -> dict:
    return _flow(flow_id).as_dict()


def cancel(flow_id: str) -> dict:
    flow = _flow(flow_id)
    if flow.process and flow.process.poll() is None:
        flow.process.kill()
        with _LOCK:
            flow.status = "failed"
            flow.lines.append("cancelled")
    return flow.as_dict()


def _http_check(spec: dict, env: dict[str, str] | None, timeout: float) -> tuple[bool, str]:
    url = _expand([str(spec.get("http") or "")], env)[0]
    if not url.startswith(("http://", "https://")):
        return False, "the URL name is not set"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    auth_names = list(spec.get("auth") or [])
    source = env if env is not None else os.environ
    if len(auth_names) == 2:
        user, password = (source.get(auth_names[0], ""), source.get(auth_names[1], ""))
        if user and password:
            token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
            request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{type(exc).__name__}"


def verify(group: str, env: dict[str, str] | None = None, timeout: float = VERIFY_TIMEOUT_SECONDS) -> dict:
    """Run the group's whoami check, cache ok / not ok, and return the cached record."""
    spec = verify_spec(group)
    if spec is None:
        raise ConnectError("this platform has no verify command")
    detail = ""
    ok = False
    if spec.get("http"):
        ok, detail = _http_check(spec, env, timeout)
    else:
        argv = _expand(list(spec["argv"]), env)
        source = env if env is not None else os.environ
        unset = [name for name in _placeholders(list(spec["argv"])) if not str(source.get(name, "")).strip()]
        if unset:
            ok, detail = False, "set " + ", ".join(dict.fromkeys(unset)) + " first"
        elif shutil.which(argv[0]) is None:
            ok, detail = False, f"the {argv[0]} CLI is not installed on the orchestrator host"
        else:
            try:
                completed = subprocess.run(
                    argv, cwd=str(config.PROJECT_DIR), capture_output=True, text=True, timeout=timeout, check=False, env=_subprocess_env(env)
                )
                ok = completed.returncode == 0
                output = (completed.stdout or "") + (completed.stderr or "")
                detail = redaction.redact(" ".join(output.split())[:300]) or f"exit {completed.returncode}"
            except subprocess.TimeoutExpired:
                ok, detail = False, f"timed out after {int(timeout)}s"
            except OSError as exc:
                ok, detail = False, f"could not run {argv[0]}: {type(exc).__name__}"
    record = {"ok": ok, "checked_at": _iso(time.time()), "detail": detail}
    with _LOCK:
        _VERIFIED[group] = record
    return dict(record)


def verified(group: str) -> dict | None:
    with _LOCK:
        record = _VERIFIED.get(group)
    return dict(record) if record else None


def verified_all() -> dict[str, dict]:
    with _LOCK:
        return {key: dict(value) for key, value in _VERIFIED.items()}


def forget(group: str) -> None:
    with _LOCK:
        _VERIFIED.pop(group, None)


def reset() -> None:
    """Tests only: drop every flow and cached check."""
    with _LOCK:
        for flow in _FLOWS.values():
            if flow.process and flow.process.poll() is None:
                flow.process.kill()
        _FLOWS.clear()
        _VERIFIED.clear()


# --- Sprint 4 phase 12: vault -> platform secret store --------------------------------------

SYNC_TARGETS = ("github",)
SYNC_TIMEOUT_SECONDS = 60
_GITHUB_REMOTE = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")

SyncRunner = Callable[[list[str], str], tuple[bool, str]]


def github_repository(env: Mapping[str, str] | None = None, cwd: str | None = None) -> str:
    """``owner/name`` from GITHUB_REPOSITORY, or (for the live environment only) from the origin
    remote, ssh or https; "" when unknown."""
    source = env if env is not None else os.environ
    configured = str(source.get("GITHUB_REPOSITORY", "") or "").strip()
    if configured or env is not None:
        return configured
    try:
        completed = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd or str(config.PROJECT_DIR),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    match = _GITHUB_REMOTE.search(completed.stdout.strip()) if completed.returncode == 0 else None
    return f"{match.group(1)}/{match.group(2)}" if match else ""


def sync_names(groups: list[str] | None = None, names: list[str] | None = None) -> list[str]:
    """Registry names marked ``github_secret``, limited to ``groups`` or ``names`` when given."""
    wanted = set(credentials.github_secret_names())
    if groups:
        allowed: set[str] = set()
        for key in groups:
            allowed.update(credentials.placeholders_for(key))
        wanted &= allowed
    if names:
        wanted &= {str(name).strip() for name in names}
    return sorted(wanted)


def _run_sync(argv: list[str], value: str) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            argv, input=value, capture_output=True, text=True, timeout=SYNC_TIMEOUT_SECONDS, check=False, env=_subprocess_env()
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, type(exc).__name__
    if completed.returncode == 0:
        return True, ""
    tail = (completed.stderr or completed.stdout or "").strip().splitlines()
    return False, redaction.redact(tail[-1] if tail else f"exit {completed.returncode}")[:160]


def sync_secrets(
    target: str = "github",
    *,
    groups: list[str] | None = None,
    names: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    runner: SyncRunner | None = None,
) -> dict:
    """Copy the values present here into the platform's secret store; the result names names only.

    GitHub: one ``gh secret set NAME --repo owner/name`` per present name, the value on stdin
    (gh reads it there when ``--body`` is absent; never on the argv, never in the result), with
    the gh CLI's own login or GITHUB_TOKEN.
    Names without a value here are listed as ``skipped``; a failed command names the name and a
    redacted one-line reason under ``failed``.
    """
    if target not in SYNC_TARGETS:
        raise ConnectError(f"secrets can be synced to {', '.join(SYNC_TARGETS)} only")
    if runner is None and shutil.which("gh") is None:
        raise ConnectError("the gh CLI is not installed on the orchestrator host")
    repository = github_repository(env)
    if not repository:
        raise ConnectError("no GitHub repository: set GITHUB_REPOSITORY or add an origin remote on github.com")
    if env is None:
        config.load_env_files()
        try:
            from . import vault

            vault.export_env()
        except Exception as exc:  # noqa: BLE001 - an unreadable vault only means fewer values
            print(f"orchestrator vault is unreadable: {type(exc).__name__}", flush=True)
    source = env if env is not None else os.environ
    chosen = sync_names(groups, names)
    present = [name for name in chosen if credentials.is_present(name, source if env is not None else None)]
    skipped = [name for name in chosen if name not in present]
    run = runner or _run_sync
    synced: list[str] = []
    failed: list[str] = []
    for name in present:
        argv = ["gh", "secret", "set", name, "--repo", repository]
        ok, detail = run(argv, str(source.get(name, "")))
        if ok:
            synced.append(name)
        else:
            failed.append(f"{name}: {detail}" if detail else name)
    return {
        "target": target,
        "repository": repository,
        "synced": synced,
        "skipped": skipped,
        "failed": failed,
        "at": _iso(time.time()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.connect",
        description="Platform connections from a shell. Values never appear in the output.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser(
        "sync-github",
        help="copy the present vault/.env values whose names are GitHub secrets into the repository's Actions secrets",
    )
    sync.add_argument("--group", action="append", dest="groups", help="limit to a registry group (repeatable), e.g. aws")
    sync.add_argument("--name", action="append", dest="names", help="limit to a registry name (repeatable)")
    args = parser.parse_args(argv)
    try:
        result = sync_secrets("github", groups=args.groups, names=args.names)
    except ConnectError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"GitHub repository {result['repository']}: synced {', '.join(result['synced']) or 'nothing'}")
    if result["skipped"]:
        print("no value here for " + ", ".join(result["skipped"]))
    if result["failed"]:
        print("failed: " + "; ".join(result["failed"]), file=sys.stderr)
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
