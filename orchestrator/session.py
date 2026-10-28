"""One human session over the orchestrator graph.

The web UI and the tests use this service. It does not invent ecosystem
answers, pipeline approval, or UAT sign-off.
"""

from __future__ import annotations

import copy
import os
import threading
from urllib.parse import urlparse

from app import config

from .llm import scrub
from .state import initial_state


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
    }


def _stage_board(values: dict) -> list[dict]:
    from .state import latest_results

    latest = latest_results(values)
    board = []
    for stage in (values.get("pipeline") or {}).get("stages") or []:
        row = latest.get(stage["name"]) or {}
        board.append(
            {
                "name": stage["name"],
                "depends_on": list(stage.get("depends_on") or []),
                "purpose": stage.get("purpose") or "",
                "status": row.get("status") or "pending",
                "evidence": row.get("evidence") or "",
                "brief": row.get("brief") or "",
                "watch_for": row.get("watch_for") or "",
                "attempt": row.get("attempt"),
                "exit_code": row.get("exit_code"),
            }
        )
    return board


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

    def snapshot(self) -> dict:
        with self._cv:
            return copy.deepcopy(self.view)

    def start(self) -> dict:
        with self._cv:
            if self._thread and self._thread.is_alive():
                return copy.deepcopy(self.view)
            self._closed = False
            self._resume_ready = False
            self._resume_payload = None
            self.results_viewed = False
            self._state = initial_state(self.repo_root)
            self.view = _empty_view()
            self.view["run_id"] = self._state["run_id"]
            self.view["phase"] = "starting"
            self.view["status"] = "running"
            self.view["public_url"] = self.public_url
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
            if phase not in {"questions", "approval", "uat"}:
                raise RuntimeError(f"no human gate is open (phase={phase})")
            resume = self._resume_for(phase, body)
            self._resume_payload = resume
            self._resume_ready = True
            self.view["phase"] = "running"
            self.view["status"] = "running"
            if phase == "questions":
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

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)

    def _resume_for(self, phase: str, body: dict) -> dict:
        expected = {
            "questions": "ecosystem_qa",
            "approval": "pipeline_approval",
            "uat": "uat_signoff",
        }[phase]
        gate = body.get("gate")
        if gate is not None and gate != expected:
            raise ValueError(f"this gate is {expected}")
        if phase == "questions":
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
            if extra:
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
            "pipeline_approval": "approval",
            "uat_signoff": "uat",
        }.get(gate, "running")
        pipeline = pipeline_view(values.get("pipeline") or {})
        with self._cv:
            self.view.update(
                {
                    "phase": phase,
                    "status": values.get("status") or "running",
                    "run_id": values.get("run_id"),
                    "questions": list(payload.get("questions") or values.get("open_questions") or []),
                    "pipeline": pipeline if pipeline.get("stages") else None,
                    "stages": _stage_board(values),
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
                }
            )
        print(f"orchestrator session {values.get('run_id')} phase={phase}", flush=True)

    def _publish_results(self, values: dict) -> None:
        from .graph import pipeline_view, visible_report

        report = visible_report(values)
        pipeline = pipeline_view(values.get("pipeline") or {})
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
                }
            )
        print(
            f"orchestrator session {values.get('run_id')} phase=results status={self.view['status']}",
            flush=True,
        )

    def _run(self) -> None:
        from langgraph.types import Command

        from .checkpoints import open_checkpointer
        from .graph import build_graph, interrupt_payload

        state = self._state or initial_state(self.repo_root)
        thread = {"configurable": {"thread_id": state["run_id"]}, "recursion_limit": 120}
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
                for _ in range(12):
                    self._touch_running()
                    graph.invoke(pending, thread)
                    snapshot = graph.get_state(thread)
                    payload = interrupt_payload(graph, thread)
                    if payload is not None:
                        self._publish_interrupt(payload, snapshot.values)
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
        except Exception as exc:  # noqa: BLE001 — the UI must show any session failure
            self._set_error(str(exc))
