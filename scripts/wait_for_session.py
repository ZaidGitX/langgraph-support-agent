"""Wait until the human finishes the orchestrator session in the UI.

This process never submits ecosystem answers, pipeline approval, or UAT
sign-off. A timeout fails the job.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request


def wait_for(
    base_url: str,
    timeout_seconds: float,
    *,
    clock=time.time,
    sleep=time.sleep,
    opener=urllib.request.urlopen,
    grace_seconds: float = 600,
) -> int:
    deadline = clock() + timeout_seconds
    url = base_url.rstrip("/") + "/api/orchestrator/session"
    while clock() < deadline:
        try:
            with opener(url, timeout=10) as response:
                payload = json.loads(response.read().decode())
        except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            print(f"session poll failed: {exc.__class__.__name__}", flush=True)
            sleep(5)
            continue
        if not isinstance(payload, dict):
            print("session poll returned an unexpected payload", flush=True)
            sleep(5)
            continue
        phase = payload.get("phase")
        viewed = bool(payload.get("results_viewed"))
        remaining = max(0, int(deadline - clock()))
        print(
            f"waiting phase={phase} results_viewed={viewed} remaining_s={remaining}",
            flush=True,
        )
        if phase == "error":
            message = payload.get("error") or "The orchestrator session failed."
            print(message, flush=True)
            return 1
        if phase == "results" and viewed:
            print(
                f"Session finished. status={payload.get('status')} run_id={payload.get('run_id')}",
                flush=True,
            )
            if grace_seconds:
                sleep(grace_seconds)
            return 0
        sleep(10)
    print(
        "Session timed out after 60 minutes waiting for ecosystem answers and the results screen. "
        "No canned answers were submitted.",
        flush=True,
    )
    return 1


def main() -> int:
    base = os.environ.get("APP_LOCAL_URL", "http://127.0.0.1:8000")
    timeout = float(os.environ.get("SESSION_TIMEOUT_SECONDS", "3600"))
    return wait_for(base, timeout)


if __name__ == "__main__":
    raise SystemExit(main())
