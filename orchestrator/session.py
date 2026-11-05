"""One human session over the orchestrator graph.

The web UI and the tests use this service. It does not invent ecosystem
answers, pipeline approval, or UAT sign-off.
"""

from __future__ import annotations

import copy
import os
import threading
import time
from collections import deque
from urllib.parse import urlparse

from app import config

from . import generator, registry
from . import profile as run_profile
from .llm import scrub
from .runner import reset_printer, reset_progress, set_printer, set_progress
from .state import ALLOWED_TARGETS, STAGE_NAMES, initial_state

MAX_EVENTS = 500


def _empty_view() -> dict:
    return {
        "run_id": None,
        "phase": "idle",
        "status": "idle",
        "questions": [],
        "pipeline": None,
        "stages": [],
        "report": None,
        "error": "",
        "gate": "",
        "has_answers": False,
        "jev_decisions": [],
        "quality_gate": {},
        "results_viewed": False,
        "public_url": "",
        "retrieval": {},
        "episodes": [],
        "readiness": {},
        "candidates": [],
        "artifacts": {},
        "artifact_review": {},
        "policy": [],
        "runner_plan": {},
        "worker_plan": {},
        "clarify_round": 0,
        "follow_up": {},
        "retrieval_evidence": [],
        "provision": {},
        "injection": {},
        "suite": {},
        "autopilot": {"enabled": False, "profile": "", "events": []},
        "trigger": {},
        "queue": [],
        # Sprint 4 phase 11: the live feed. ``seq`` changes on every publish; ``events`` is
        # the timeline (node, stage, wave, jev, gate, autopilot, note); ``running_stages``
        # are dispatched and not yet finished; ``timeline`` is the node trace with seconds.
        "seq": 0,
        "events": [],
        "running_stages": [],
        "timeline": [],
        "started_at": "",
    }


def _stage_board(values: dict, running: set[str] | None = None) -> list[dict]:
    from .state import latest_results

    latest = latest_results(values)
    board = []
    for stage in (values.get("pipeline") or {}).get("stages") or []:
        row = latest.get(stage["name"]) or {}
        status = row.get("status") or "pending"
        if running and stage["name"] in running and status == "pending":
            status = "running"
        board.append(
            {
                "name": stage["name"],
                "depends_on": list(stage.get("depends_on") or []),
                "purpose": stage.get("purpose") or "",
                "status": status,
                "evidence": row.get("evidence") or "",
                "brief": row.get("brief") or "",
                "watch_for": row.get("watch_for") or "",
                "attempt": row.get("attempt"),
                "exit_code": row.get("exit_code"),
                "runner": stage.get("runner") or "local",
                "provider": stage.get("provider") or "",
                "environment": dict(stage.get("environment") or {}),
                "kind": stage.get("kind") or "fixed",
                "family": stage.get("family") or "",
                "plan": dict(row.get("plan") or {}),
            }
        )
    return board


def _has_policy_errors(artifacts: dict) -> bool:
    for records in (artifacts or {}).values():
        for record in records or []:
            summary = record.get("policy") or {}
            if summary and not summary.get("ok", True):
                return True
    return False


class SessionService:
    def __init__(
        self,
        db_path: str | None = None,
        *,
        jev=None,
        astra=None,
        commands=None,
        repo_root: str | None = None,
    ) -> None:
        self.db_path = db_path or os.getenv("ORCHESTRATOR_DB") or str(
            config.PROJECT_DIR / "var" / "orchestrator.sqlite"
        )
        self.repo_root = repo_root or str(config.PROJECT_DIR)
        self.jev = jev
        self.astra = astra
        self.commands = commands
        self._cv = threading.Condition()
        self._thread: threading.Thread | None = None
        self._state: dict | None = None
        self._resume_payload: dict | None = None
        self._resume_ready = False
        self._closed = False
        self.results_viewed = False
        self.public_url = ""
        self.view = _empty_view()
        # Sprint 4 phase 10: pushes queue behind the active run; the profile drives autopilot.
        self._queue: deque[dict] = deque()
        self._active = False
        self._run_root = self.repo_root
        self.profile = None
        self.trigger: dict = {}
        self._seq = 0
        self._running: set[str] = set()
        self._run_started = 0.0

    def snapshot(self) -> dict:
        with self._cv:
            return copy.deepcopy(self.view)

    def start(self, repo_root: str | None = None, trigger: dict | None = None) -> dict:
        """Start a run on ``repo_root`` (default: this checkout). A run that arrives while
        another is active waits in the queue and starts when that run ends."""
        with self._cv:
            if self._active and self._thread and self._thread.is_alive():
                if repo_root or trigger:
                    self._queue.append({"repo_root": repo_root or self.repo_root, "trigger": dict(trigger or {"source": "manual"})})
                    self.view["queue"] = [dict(item["trigger"]) for item in self._queue]
                return copy.deepcopy(self.view)
            self._closed = False
            self._resume_ready = False
            self._resume_payload = None
            self.results_viewed = False
            self._run_root = repo_root or self.repo_root
            self.trigger = dict(trigger or {"source": "manual"})
            self._state = initial_state(self._run_root)
            self.view = _empty_view()
            self.view["run_id"] = self._state["run_id"]
            self.view["phase"] = "starting"
            self.view["status"] = "running"
            self.view["public_url"] = self.public_url
            self.view["trigger"] = dict(self.trigger)
            self.view["queue"] = [dict(item["trigger"]) for item in self._queue]
            self.view["autopilot"] = self._load_profile()
            self._auto_rejected = False
            self._running = set()
            self._run_started = time.time()
            self.view["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._run_started))
            self._active = True
            self._thread = threading.Thread(
                target=self._run,
                name="orchestrator-session",
                daemon=True,
            )
            self._thread.start()
            return copy.deepcopy(self.view)

    def submit(self, body: dict) -> dict:
        with self._cv:
            phase = self.view["phase"]
            if phase not in {"questions", "follow_up", "approval", "uat"}:
                raise RuntimeError(f"no human gate is open (phase={phase})")
            resume = self._resume_for(phase, body)
            if phase == "approval" and resume.get("decision") == "approved" and _has_policy_errors(self.view.get("artifacts") or {}):
                raise ValueError("pipeline has policy errors; reject it or let Astra revise the artifacts")
            self._resume_payload = resume
            self._resume_ready = True
            self.view["phase"] = "running"
            self.view["status"] = "running"
            if phase in {"questions", "follow_up"}:
                self.view["has_answers"] = True
            self._cv.notify_all()
            return copy.deepcopy(self.view)

    def mark_viewed(self) -> dict:
        with self._cv:
            if self.view["phase"] != "results":
                raise RuntimeError("results are not ready")
            self.results_viewed = True
            self.view["results_viewed"] = True
            return copy.deepcopy(self.view)

    def set_public_url(self, url: str) -> dict:
        cleaned = (url or "").strip()
        parsed = urlparse(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must be http or https")
        with self._cv:
            if self.public_url and self.public_url != cleaned:
                raise RuntimeError("public url is already set")
            self.public_url = cleaned
            os.environ["APP_PUBLIC_URL"] = cleaned
            self.view["public_url"] = cleaned
            return copy.deepcopy(self.view)

    def artifacts(self) -> dict:
        """Generated artifacts of the current run keyed by stage, content redacted and bounded."""
        with self._cv:
            return copy.deepcopy(self.view.get("artifacts") or {})

    def artifact_text(self, stage: str, index: int) -> str | None:
        """Full redacted text of one artifact, or None when the stage or index is unknown."""
        with self._cv:
            records = list((self.view.get("artifacts") or {}).get(stage) or [])
        if index < 0 or index >= len(records):
            return None
        record = records[index]
        return generator.content_of(record, limit=10_000_000) or str(record.get("content") or "")

    def episodes(self, limit: int = 5) -> list[dict]:
        """Newest recorded runs of this checkout; never a value from the environment."""
        from .episodes import recall

        return recall(self.repo_root, limit=max(1, min(int(limit), 50)), db_path=self.db_path)

    def view_seq(self) -> int:
        with self._cv:
            return int(self.view.get("seq") or 0)

    def events_since(self, since: int = 0, limit: int = 200) -> dict:
        """Events with seq > since (oldest first), plus the latest seq and phase."""
        with self._cv:
            events = [item for item in self.view.get("events") or [] if int(item.get("seq") or 0) > since]
            return {
                "seq": int(self.view.get("seq") or 0),
                "phase": self.view.get("phase"),
                "run_id": self.view.get("run_id"),
                "events": [dict(item) for item in events[:limit]],
                "more": len(events) > limit,
            }

    def _emit(self, kind: str, name: str = "", detail: str = "", **extra) -> None:
        """Append one timeline event and bump seq."""
        with self._cv:
            self._seq += 1
            record = {
                "seq": self._seq,
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "t": round(time.time() - self._run_started, 2) if self._run_started else 0.0,
                "kind": kind,
                "name": name,
                "detail": scrub(detail)[:400],
                **{key: value for key, value in extra.items() if value is not None},
            }
            events = self.view.setdefault("events", [])
            events.append(record)
            if len(events) > MAX_EVENTS:
                del events[: len(events) - MAX_EVENTS]
            self.view["seq"] = self._seq
            self._cv.notify_all()

    def _progress(self, kind: str, name: str = "", detail: str = "", **extra) -> None:
        """Progress from inside a node (the generation workers: kind ``generation`` once, then a
        ``worker`` event when each stage's files start and finish), so the timeline moves while
        a node runs. Live run 4 showed a 21-minute generate_artifacts node with no event."""
        self._emit(kind, name, detail, **extra)

    def _note(self, message: str) -> None:
        """Runner notes (the argv a stage runs) become timeline events, redacted."""
        text = scrub(str(message or "")).strip()
        if text:
            self._emit("note", "", text)

    def _on_node(self, node: str, update: dict, values: dict) -> None:
        """Translate one node update into events and refresh the live view."""
        if not isinstance(update, dict):
            update = {}
        seconds = round(time.time() - self._run_started, 2) if self._run_started else 0.0
        if node == "dispatch_stages":
            ready = [name for name in update.get("ready_stages") or [] if name]
            if ready:
                self._running = set(ready)
                self._emit("wave", "dispatch", "running " + ", ".join(ready), stages=ready)
            elif update.get("stop_at") == "report":
                self._running = set()
        for result in update.get("stage_results") or []:
            if isinstance(result, dict) and result.get("stage"):
                self._running.discard(result["stage"])
                self._emit("stage", result["stage"], f"attempt {result.get('attempt') or 1}: {result.get('status')}", status=result.get("status"), runner=result.get("runner"), seconds=seconds)
        for item in update.get("jev_decisions") or []:
            if isinstance(item, dict):
                self._emit("jev", str(item.get("kind") or ""), f"{item.get('schema') or ''} via {item.get('client') or ''}", decision=item.get("decision"))
        if node == "clarify_with_user" and update.get("recalled_answers"):
            recalled = update["recalled_answers"]
            self._emit("note", "recall", "reused from the last episode: " + ", ".join(f"{key}={value}" for key, value in sorted(recalled.items())), recalled=dict(recalled))
        if node == "plan_pipeline":
            chosen = (update.get("pipeline") or {}).get("id") or ""
            self._emit("plan", chosen, f"Jev ranked {chosen}" if chosen else "no pipeline")
        if node == "generate_artifacts":
            count = sum(len(records or []) for records in (update.get("generated_artifacts") or {}).values())
            if count:
                self._emit("artifacts", "generate_artifacts", f"{count} generated file(s)")
        if node == "report":
            self._emit("report", "report", str(update.get("status") or ""))
        if node not in STAGE_NAMES and node != "generated_stage":
            self._emit("node", node, "", seconds=seconds)
        with self._cv:
            timeline = self.view.setdefault("timeline", [])
            timeline.append({"node": node, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "t": seconds})
            if len(timeline) > MAX_EVENTS:
                del timeline[: len(timeline) - MAX_EVENTS]
        self._publish_progress(values)

    def _publish_progress(self, values: dict) -> None:
        """Refresh everything the dashboard shows while the graph runs (no gate change)."""
        from .graph import pipeline_view

        pipeline = pipeline_view(values.get("pipeline") or {})
        with self._cv:
            if self.view.get("phase") in {"results", "error"}:
                return
            self.view.update(
                {
                    "run_id": values.get("run_id") or self.view.get("run_id"),
                    "stages": _stage_board(values, self._running),
                    "pipeline": pipeline if pipeline.get("stages") else None,
                    "jev_decisions": list(values.get("jev_decisions") or []),
                    "quality_gate": dict(values.get("gate") or {}),
                    "retrieval": dict(values.get("retrieval") or {}),
                    "episodes": list(values.get("episodes") or []),
                    "readiness": dict(values.get("readiness") or {}),
                    "candidates": list(values.get("candidates") or []),
                    "artifacts": {
                        stage: generator.records_for_view(records)
                        for stage, records in (values.get("generated_artifacts") or {}).items()
                    },
                    "artifact_review": dict(values.get("artifact_review") or {}),
                    "policy": list(values.get("policy") or []),
                    "runner_plan": dict(values.get("runner_plan") or {}),
                    "worker_plan": dict(values.get("worker_plan") or {}),
                    "clarify_round": int(values.get("clarify_rounds") or 0),
                    "follow_up": dict(values.get("follow_up_decision") or {}),
                    "retrieval_evidence": list((values.get("ecosystem") or {}).get("evidence") or []),
                    "provision": dict(values.get("provision") or {}),
                    "injection": dict(values.get("injection") or {}),
                    "suite": dict((values.get("pipeline") or {}).get("suite") or {}),
                    "trace": list(values.get("trace") or []),
                    "running_stages": sorted(self._running),
                    "has_answers": bool(values.get("answers")) or self.view.get("has_answers"),
                }
            )
            self._seq += 1
            self.view["seq"] = self._seq
            self._cv.notify_all()

    def _load_profile(self) -> dict:
        """Read orchestrator.yaml from the run's checkout; an invalid file disables autopilot.

        The profile's ``config`` names (AWS_REGION, ...) are exported before the run starts
        even when ORCHESTRATOR_AUTOPILOT is off: they say where a push goes, not who answers.
        """
        self.profile = None
        disabled = os.getenv("ORCHESTRATOR_AUTOPILOT", "1").strip().lower() in {"0", "false", "off", "no"}
        try:
            loaded = run_profile.load(self._run_root)
        except run_profile.ProfileError as exc:
            return {"enabled": False, "profile": str(run_profile.path_for(self._run_root)), "error": scrub(str(exc)), "events": []}
        if loaded is None:
            if disabled:
                return {"enabled": False, "profile": "", "disabled_by": "ORCHESTRATOR_AUTOPILOT", "events": []}
            return {"enabled": False, "profile": "", "events": []}
        applied = run_profile.apply_config(loaded)
        if applied:
            print(f"orchestrator profile: config {', '.join(applied)} from {run_profile.PROFILE_FILE}", flush=True)
        if disabled:
            return {"enabled": False, "profile": loaded.path, "disabled_by": "ORCHESTRATOR_AUTOPILOT", "config": sorted(loaded.config), "events": []}
        self.profile = loaded
        return {
            "enabled": loaded.enabled,
            "profile": loaded.path,
            "platform": loaded.platform,
            "tier": loaded.tier,
            "approve": loaded.autopilot.approve,
            "answer_questions": loaded.autopilot.answer_questions,
            "min_confidence": loaded.autopilot.min_confidence,
            "config": sorted(loaded.config),
            "events": [],
        }

    def _autopilot(self, payload: dict) -> dict | None:
        """Answer a gate from the profile, or None to wait for a person. UAT is never answered."""
        loaded = self.profile
        gate = str(payload.get("gate") or "")
        if loaded is None or not loaded.enabled:
            return None
        if gate == "uat_signoff":
            self._autopilot_event(gate, "human", "UAT sign-off is always a person")
            return None
        with self._cv:
            phase = self.view.get("phase")
            questions = list(self.view.get("questions") or [])
            view = copy.deepcopy(self.view)
        if gate in {"ecosystem_qa", "follow_up_qa"}:
            if not loaded.autopilot.answer_questions:
                return None
            answers, unmatched = run_profile.answers_for(loaded, questions)
            if unmatched:
                self._autopilot_event(gate, "human", "no profile answer for " + ", ".join(unmatched))
                return None
            try:
                resume = self._resume_for(phase, {"gate": gate, "answers": answers})
            except ValueError as exc:
                self._autopilot_event(gate, "human", scrub(str(exc)))
                return None
            self._autopilot_event(gate, "answered", "answered " + ", ".join(sorted(answers)))
            with self._cv:
                self.view["phase"] = "running"
                self.view["status"] = "running"
                self.view["has_answers"] = True
            return resume
        if gate == "pipeline_approval":
            ok, reason = run_profile.approval(loaded, view)
            if not ok and reason.startswith("suite:") and not getattr(self, "_auto_rejected", False):
                # The platform suite is not in use: reject once with the reason, so the run plans
                # again instead of running on the host (live run 6); a second miss is a person's call.
                feedback = (
                    f"{reason}. Every fixed stage must run on the {loaded.platform} platform through the suite: "
                    "write the suite's files under the suggested paths with by-reference secrets only, "
                    "and keep every generated file under ci/, deploy/, docker/, or docs/deploy/."
                )
                try:
                    resume = self._resume_for(phase, {"gate": gate, "decision": "rejected", "feedback": feedback})
                except ValueError as exc:
                    self._autopilot_event(gate, "human", scrub(str(exc)))
                    return None
                self._auto_rejected = True
                self._autopilot_event(gate, "rejected", reason)
                with self._cv:
                    self.view["phase"] = "running"
                    self.view["status"] = "running"
                return {**resume, "by": "autopilot"}
            if not ok or _has_policy_errors(view.get("artifacts") or {}):
                self._autopilot_event(gate, "human", reason if not ok else "an artifact has a policy error")
                return None
            self._autopilot_event(gate, "approved", reason)
            with self._cv:
                self.view["phase"] = "running"
                self.view["status"] = "running"
            return {"decision": "approved", "by": "autopilot"}
        return None

    def _autopilot_event(self, gate: str, action: str, detail: str) -> None:
        record = run_profile.event(gate, action, scrub(detail))
        with self._cv:
            auto = self.view.setdefault("autopilot", {"enabled": True, "profile": "", "events": []})
            auto.setdefault("events", []).append(record)
        self._emit("autopilot", gate, f"{action}: {record['detail']}", action=action)
        print(f"orchestrator autopilot {gate}: {action} ({record['detail']})", flush=True)

    def _start_next(self) -> None:
        """When a run ends, start the next queued push (called from the run thread)."""
        with self._cv:
            self._active = False
            if self._closed or not self._queue:
                return
            item = self._queue.popleft()
        self.start(repo_root=item["repo_root"], trigger=item["trigger"])

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._queue.clear()
            self._cv.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)

    def _resume_for(self, phase: str, body: dict) -> dict:
        expected = {
            "questions": "ecosystem_qa",
            "follow_up": "follow_up_qa",
            "approval": "pipeline_approval",
            "uat": "uat_signoff",
        }[phase]
        gate = body.get("gate")
        if gate is not None and gate != expected:
            raise ValueError(f"this gate is {expected}")
        if phase in {"questions", "follow_up"}:
            answers = body.get("answers")
            if not isinstance(answers, dict):
                raise ValueError("answers must be an object")
            questions = self.view.get("questions") or []
            expected_ids = [question["id"] for question in questions]
            cleaned: dict[str, str] = {}
            for ident in expected_ids:
                value = answers.get(ident)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"missing answer for {ident}")
                cleaned[ident] = value.strip()
            extra = set(answers) - set(expected_ids)
            # The environment ids may be given even when Astra did not ask them (it recalled an
            # earlier run): an explicit answer beats the recalled one and is validated here.
            allowed_extra = {"env_target": ALLOWED_TARGETS, "env_platform": registry.PLATFORMS}
            for ident in sorted(extra & set(allowed_extra)):
                value = str(answers.get(ident) or "").strip().lower()
                if value not in allowed_extra[ident]:
                    raise ValueError(f"{ident} must be one of " + ", ".join(allowed_extra[ident]))
                cleaned[ident] = value
            if extra - set(allowed_extra):
                raise ValueError("unexpected answer ids")
            return cleaned
        decision = body.get("decision")
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        resume = {"decision": decision}
        feedback = body.get("feedback")
        if isinstance(feedback, str) and feedback.strip():
            resume["feedback"] = feedback.strip()
        return resume

    def _wait_for_resume(self) -> dict:
        with self._cv:
            while not self._resume_ready and not self._closed:
                self._cv.wait()
            if self._closed and not self._resume_ready:
                return {"_cancel": True}
            payload = self._resume_payload or {"_cancel": True}
            self._resume_ready = False
            self._resume_payload = None
            return payload

    def _set_error(self, message: str) -> None:
        with self._cv:
            if self.view.get("phase") == "results":
                return
            self.view["phase"] = "error"
            self.view["status"] = "error"
            self.view["error"] = scrub(message)
            self._seq += 1
            self.view["seq"] = self._seq
            self._cv.notify_all()
        print("orchestrator session phase=error", flush=True)

    def _touch_running(self) -> None:
        with self._cv:
            if self.view.get("phase") == "results":
                return
            self.view["phase"] = "running"
            self.view["status"] = "running"

    def _publish_interrupt(self, payload: dict, values: dict) -> None:
        from .graph import pipeline_view

        gate = str(payload.get("gate") or "")
        phase = {
            "ecosystem_qa": "questions",
            "follow_up_qa": "follow_up",
            "pipeline_approval": "approval",
            "uat_signoff": "uat",
        }.get(gate, "running")
        pipeline = pipeline_view(values.get("pipeline") or {})
        self._running = set()
        self._emit("gate", gate, f"waiting at {gate}", phase=phase, questions=[q.get("id") for q in payload.get("questions") or []])
        with self._cv:
            self.view.update(
                {
                    "phase": phase,
                    "status": values.get("status") or "running",
                    "run_id": values.get("run_id"),
                    "questions": list(payload.get("questions") or values.get("open_questions") or []),
                    "pipeline": pipeline if pipeline.get("stages") else None,
                    "stages": _stage_board(values, self._running),
                    "report": None,
                    "error": "",
                    "gate": gate,
                    "has_answers": bool(values.get("answers")) or self.view.get("has_answers"),
                    "jev_decisions": list(values.get("jev_decisions") or []),
                    "quality_gate": dict(values.get("gate") or {}),
                    "results_viewed": self.results_viewed,
                    "public_url": self.public_url,
                    "retrieval": dict(values.get("retrieval") or {}),
                    "episodes": list(values.get("episodes") or []),
                    "readiness": dict(values.get("readiness") or {}),
                    "candidates": list(payload.get("candidates") or values.get("candidates") or []),
                    "artifacts": dict(payload.get("artifacts") or {}),
                    "artifact_review": dict(payload.get("artifact_review") or values.get("artifact_review") or {}),
                    "policy": list(payload.get("policy") or values.get("policy") or []),
                    "runner_plan": dict(payload.get("runner_plan") or values.get("runner_plan") or {}),
                    "worker_plan": dict(payload.get("worker_plan") or values.get("worker_plan") or {}),
                    "clarify_round": int(payload.get("round") or values.get("clarify_rounds") or 0),
                    "follow_up": dict(values.get("follow_up_decision") or {}),
                    "retrieval_evidence": list(payload.get("retrieval_evidence") or (values.get("ecosystem") or {}).get("evidence") or []),
                    "provision": dict(payload.get("provision") or values.get("provision") or {}),
                    "injection": dict(payload.get("injection") or values.get("injection") or {}),
                    "suite": dict((values.get("pipeline") or {}).get("suite") or {}),
                    "running_stages": [],
                    "trace": list(values.get("trace") or []),
                }
            )
            self._seq += 1
            self.view["seq"] = self._seq
            self._cv.notify_all()
        print(f"orchestrator session {values.get('run_id')} phase={phase}", flush=True)

    def _publish_results(self, values: dict) -> None:
        from .graph import pipeline_view, visible_report

        report = visible_report(values)
        pipeline = pipeline_view(values.get("pipeline") or {})
        self._running = set()
        self._emit("done", "results", str(values.get("status") or report.get("status") or ""), status=values.get("status") or report.get("status"))
        with self._cv:
            self.view.update(
                {
                    "phase": "results",
                    "status": values.get("status") or report.get("status") or "escalated",
                    "run_id": values.get("run_id"),
                    "questions": list(values.get("open_questions") or []),
                    "pipeline": pipeline if pipeline.get("stages") else None,
                    "stages": _stage_board(values) if report.get("chosen_pipeline_ran") else [],
                    "report": report,
                    "error": "",
                    "gate": "",
                    "has_answers": bool(values.get("answers")),
                    "jev_decisions": list(values.get("jev_decisions") or []),
                    "quality_gate": dict(values.get("gate") or {}),
                    "results_viewed": self.results_viewed,
                    "public_url": self.public_url,
                    "retrieval": dict(values.get("retrieval") or {}),
                    "episodes": list(values.get("episodes") or []),
                    "readiness": dict(values.get("readiness") or {}),
                    "candidates": list(values.get("candidates") or []),
                    "artifacts": {
                        stage: generator.records_for_view(records)
                        for stage, records in (values.get("generated_artifacts") or {}).items()
                    },
                    "artifact_review": dict(values.get("artifact_review") or {}),
                    "policy": list(values.get("policy") or []),
                    "runner_plan": dict(values.get("runner_plan") or {}),
                    "worker_plan": dict(values.get("worker_plan") or {}),
                    "clarify_round": int(values.get("clarify_rounds") or 0),
                    "follow_up": dict(values.get("follow_up_decision") or {}),
                    "retrieval_evidence": list((values.get("ecosystem") or {}).get("evidence") or []),
                    "provision": dict(values.get("provision") or {}),
                    "injection": dict(values.get("injection") or {}),
                    "suite": dict((values.get("pipeline") or {}).get("suite") or {}),
                    "running_stages": [],
                    "trace": list(values.get("trace") or []),
                }
            )
            self._seq += 1
            self.view["seq"] = self._seq
            self._cv.notify_all()
        print(
            f"orchestrator session {values.get('run_id')} phase=results status={self.view['status']}",
            flush=True,
        )

    def _run(self) -> None:
        from langgraph.types import Command

        from .checkpoints import open_checkpointer
        from .graph import build_graph, interrupt_payload

        state = self._state or initial_state(self._run_root)
        thread = {"configurable": {"thread_id": state["run_id"]}, "recursion_limit": 160}
        pending: dict | Command = state
        try:
            parent = os.path.dirname(self.db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open_checkpointer(self.db_path) as saver:
                graph = build_graph(
                    saver,
                    jev=self.jev,
                    astra=self.astra,
                    commands=self.commands,
                    episodes_db=self.db_path,
                )
                token = set_printer(self._note)
                progress_token = set_progress(self._progress)
                try:
                    for _ in range(12):
                        self._touch_running()
                        for chunk in graph.stream(pending, thread, stream_mode="updates"):
                            if not isinstance(chunk, dict):
                                continue
                            for node, update in chunk.items():
                                if node == "__interrupt__":
                                    continue
                                self._on_node(str(node), update if isinstance(update, dict) else {}, graph.get_state(thread).values)
                        snapshot = graph.get_state(thread)
                        payload = interrupt_payload(graph, thread)
                        if payload is not None:
                            self._publish_interrupt(payload, snapshot.values)
                            resume = self._autopilot(payload)
                            if resume is None:
                                resume = self._wait_for_resume()
                            if not isinstance(resume, dict) or resume.get("_cancel"):
                                self._set_error("session closed before a human answer")
                                return
                            pending = Command(resume=resume)
                            continue
                        if not snapshot.next:
                            self._publish_results(snapshot.values)
                            return
                        raise RuntimeError(f"graph paused at {snapshot.next} without an interrupt")
                    raise RuntimeError("stopped after 12 interrupt resumes")
                finally:
                    reset_progress(progress_token)
                    reset_printer(token)
        except Exception as exc:  # noqa: BLE001 — the UI must show any session failure
            self._set_error(str(exc))
        finally:
            self._start_next()
