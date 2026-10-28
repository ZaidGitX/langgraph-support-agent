"""Offline rehearsal of the Sprint 3 demo.

Runs the orchestrator the way the demo does, through the FastAPI session API the dashboard
polls and answers, but with the deterministic test doubles in place of Astra and Jev, so it
needs no network and no key. Three scenarios on this checkout:

1. ``local``      a stochastic route whose generated container scan executes on the local runner
2. ``cloud_plan`` a route whose dev deploy wants AWS CodeBuild and therefore records ``planned``
3. ``recall``     a second run that recalls the first episode before it writes the questions

Two fake credential values are planted in the environment first; the rehearsal fails if either
value reaches readiness, a session view, a report, or a generated file.

    /opt/anaconda3/envs/tcs_practice_env/bin/python scripts/rehearse_demo.py [--out DIR]

Outputs go to ``var/rehearsal/<timestamp>/`` (views per phase, reports, a summary table).
Rehearsal only: the doubles live in ``tests/`` and are never wired into the app or CI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP_NAMES = {"OPENAI_API_KEY", "OPENAI_MODEL", "TYPESAFE_API_KEY", "TYPESAFE_MODEL"}
PLANTED = ("AWS_SECRET_ACCESS_KEY", "JENKINS_API_TOKEN")
ANSWERS = {
    "env_target": "dev",
    "env_platform": "docker",
    "test_depth": "unit and integration",
    "release_policy": "manual approval",
    "scan_policy": "high",
}
API = "/api/orchestrator"


def _plant_fake_values() -> dict[str, str]:
    """Unset every cloud name (as conftest does), then plant two recognisable fake values."""
    from orchestrator import credentials

    for name in credentials.all_names():
        if name not in APP_NAMES:
            os.environ.pop(name, None)
    for name in ("ORCHESTRATOR_AUTOSTART", "ORCHESTRATOR_DISPATCH"):
        os.environ.pop(name, None)
    planted = {name: "rehearsal-fake-" + name.lower().replace("_", "-") + "-" + "0123456789" for name in PLANTED}
    os.environ.update(planted)
    return planted


def _answers_for(view: dict) -> dict[str, str]:
    return {q["id"]: ANSWERS.get(q["id"], f"rehearsal answer for {q['id']}") for q in view.get("questions") or []}


def _wait(service, phases: set[str], timeout: float) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = service.snapshot()
        if last["phase"] in phases:
            return last
        if last["phase"] == "error":
            raise RuntimeError(f"session error: {last.get('error')}")
        time.sleep(0.05)
    raise RuntimeError(f"timed out waiting for {sorted(phases)}; last phase {last and last['phase']}")


def _dump(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, default=str) + "\n", encoding="utf-8")


def run_scenario(name: str, astra, out: Path, db_path: str) -> dict:
    """Drive one session over HTTP exactly as the dashboard does. Returns what was observed."""
    from fastapi.testclient import TestClient

    from app.main import app
    from orchestrator.session import SessionService
    from tests.test_orchestrator import FakeJev, _echo

    service = SessionService(db_path=db_path, jev=FakeJev(), astra=astra, commands=_echo(), repo_root=str(ROOT))
    folder = out / name
    views: dict[str, dict] = {}
    started_at = time.time()
    with TestClient(app) as client:
        original = getattr(app.state, "sessions", None)
        app.state.sessions = service
        try:
            readiness = client.get(f"{API}/readiness").json()
            episodes_before = client.get(f"{API}/episodes?limit=5").json()
            run_id = client.post(f"{API}/session").json()["run_id"]
            view = _wait(service, {"questions"}, 120)
            views["questions"] = view
            client.post(f"{API}/session/answer", json={"gate": "ecosystem_qa", "answers": _answers_for(view)}).raise_for_status()
            view = _wait(service, {"follow_up", "approval"}, 120)
            if view["phase"] == "follow_up":
                views["follow_up"] = view
                client.post(f"{API}/session/answer", json={"gate": "follow_up_qa", "answers": _answers_for(view)}).raise_for_status()
                view = _wait(service, {"approval"}, 120)
            views["approval"] = view
            artifacts = client.get(f"{API}/session/artifacts").json()
            texts = {
                f"{stage}/{index}": client.get(f"{API}/session/artifacts/{stage}/{index}").text
                for stage, records in artifacts.items()
                for index in range(len(records))
            }
            client.post(f"{API}/session/answer", json={"gate": "pipeline_approval", "decision": "approved"}).raise_for_status()
            view = _wait(service, {"uat", "results"}, 180)
            if view["phase"] == "uat":
                views["uat"] = view
                client.post(f"{API}/session/answer", json={"gate": "uat_signoff", "decision": "approved"}).raise_for_status()
                view = _wait(service, {"results"}, 180)
            views["results"] = view
            episodes_after = client.get(f"{API}/episodes?limit=5").json()
        finally:
            service.close()
            app.state.sessions = original
    for phase, payload in views.items():
        _dump(folder / f"view-{phase}.json", payload)
    _dump(folder / "readiness.json", readiness)
    _dump(folder / "artifacts.json", artifacts)
    _dump(folder / "episodes.json", {"before": episodes_before, "after": episodes_after})
    report = views["results"].get("report") or {}
    (folder / "report.md").write_text(str(report.get("report_markdown") or ""), encoding="utf-8")
    _dump(folder / "report.json", report)
    return {
        "name": name,
        "run_id": run_id,
        "seconds": round(time.time() - started_at, 1),
        "phases": list(views),
        "views": views,
        "readiness": readiness,
        "artifacts": artifacts,
        "artifact_texts": texts,
        "episodes_before": episodes_before,
        "episodes_after": episodes_after,
        "folder": folder,
    }


def _production_lines(text: str) -> list[str]:
    """Lines that name production as a target; guard sentences are the documented exemption."""
    lines = []
    for line in text.splitlines():
        if not re.search(r"\bproduction\b|\bprod\b", line, re.IGNORECASE):
            continue
        # The documented exemptions: guard sentences and the two report lines that say production
        # is not a tier and that Aurora is the deferred (not connected) binding.
        if re.search(r"refus|never|no production|not a target|not a tier|forbid|deferred production binding", line, re.IGNORECASE):
            continue
        lines.append(line.strip())
    return lines


def check(results: dict[str, dict], planted: dict[str, str]) -> list[tuple[str, bool, str]]:
    """The acceptance checklist of references/10-testing-and-demo.md that a rehearsal can prove."""
    local, cloud, recall = results["local"], results["cloud_plan"], results["recall"]
    rows: list[tuple[str, bool, str]] = []

    everything = json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "folder"} for k, v in results.items()}, default=str)
    leaked = [name for name, value in planted.items() if value in everything]
    providers = local["readiness"].get("providers") or {}
    rows.append(
        (
            "readiness renders before a session; no value appears",
            bool(providers) and not leaked and "mode" in providers.get("aws", {}),
            f"{len(providers)} providers, aws mode={providers.get('aws', {}).get('mode')}, leaked={leaked or 'none'}",
        )
    )

    approval = local["views"]["approval"]
    chosen = [c for c in approval.get("candidates") or [] if c.get("chosen")]
    generated = [s for s in approval.get("stages") or [] if s.get("kind") == "generated"]
    rows.append(
        (
            "a stochastic candidate with a generated stage is chosen",
            bool(chosen) and chosen[0].get("mode") == "stochastic" and bool(generated),
            f"chosen={chosen[0]['id'] if chosen else None} mode={chosen[0].get('mode') if chosen else None} generated={[s['name'] for s in generated]}",
        )
    )

    records = [r for rs in (approval.get("artifacts") or {}).values() for r in rs]
    rows.append(
        (
            "generated artifacts are visible before approval with policy verdicts",
            bool(records) and all((r.get("policy") or {}).get("ok") for r in records) and all(r.get("content") for r in records),
            f"{len(records)} artifact(s): " + ", ".join(f"{r['path']} ok={(r.get('policy') or {}).get('ok')}" for r in records),
        )
    )

    report = local["views"]["results"]["report"]
    rows_by_stage = {row["stage"]: row for row in report.get("stage_evidence") or []}
    scan = rows_by_stage.get("container_scan") or {}
    rows.append(
        (
            "the generated local stage executed and printed generated-ok",
            scan.get("status") == "pass" and "generated-ok" in (scan.get("evidence") or ""),
            f"container_scan status={scan.get('status')} runner={scan.get('runner')} kind={scan.get('kind')}",
        )
    )

    cloud_report = cloud["views"]["results"]["report"]
    deploy = next((row for row in cloud_report.get("stage_evidence") or [] if row["stage"] == "deploy_dev"), {})
    missing = set((deploy.get("plan") or {}).get("missing") or [])
    next_step = ((deploy.get("checks") or [{}])[0]).get("next_step") or ""
    rows.append(
        (
            "a cloud stage records planned with the missing names and the gate passes",
            deploy.get("status") == "planned"
            and {"AWS_REGION", "AWS_ACCESS_KEY_ID"} <= missing
            and (cloud_report.get("quality_gate") or {}).get("route") == "pass"
            and cloud_report.get("status") == "passed"
            and cloud_report.get("aws_called") is False
            and next_step.startswith("Set "),
            f"deploy_dev status={deploy.get('status')} missing={sorted(missing)} gate={(cloud_report.get('quality_gate') or {}).get('route')} status={cloud_report.get('status')} planned={cloud_report.get('planned_stages')}",
        )
    )

    prod_hits: list[str] = []
    for result in results.values():
        prod_hits += _production_lines(json.dumps(result["views"], default=str))
        prod_hits += _production_lines(str((result["views"]["results"].get("report") or {}).get("report_markdown") or ""))
        for text in result["artifact_texts"].values():
            prod_hits += _production_lines(text)
    rows.append(
        (
            "UAT blocked until sign-off; no production reference anywhere",
            all("uat" in r["phases"] for r in results.values()) and not prod_hits and report.get("past_uat") is False,
            f"phases={local['phases']} production_lines={prod_hits[:3] or 'none'}",
        )
    )

    recalled = [ep.get("run_id") for ep in recall["views"]["questions"].get("episodes") or []]
    after = [ep.get("run_id") for ep in recall["episodes_after"]]
    rows.append(
        (
            "the episode is recorded and the second run recalls it",
            local["run_id"] in recalled and {local["run_id"], cloud["run_id"], recall["run_id"]} <= set(after),
            f"recalled_at_questions={recalled} listed_after={after}",
        )
    )

    from orchestrator import generator

    docs = {}
    for result in (local, cloud):
        environment = (result["views"]["results"]["report"].get("chosen_pipeline") or {}).get("environment") or {}
        path = generator.docs_path(str(ROOT), result["run_id"], environment)
        docs[result["name"]] = (path.is_file(), generator.doc_relative_path(environment))
    rows.append(
        (
            "the deploy document was written under the policy's docs/deploy prefix",
            all(found for found, _ in docs.values()),
            ", ".join(f"{name}: {rel} {'written' if found else 'MISSING'}" for name, (found, rel) in docs.items()),
        )
    )

    rows.append(
        (
            "a follow-up round ran once on the real checkout",
            "follow_up" in local["phases"] and local["views"]["approval"].get("clarify_round") == 2,
            f"phases={local['phases']} clarify_round={local['views']['approval'].get('clarify_round')}",
        )
    )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", help="output folder (default var/rehearsal/<timestamp>)")
    args = parser.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out).resolve() if args.out else ROOT / "var" / "rehearsal" / stamp
    out.mkdir(parents=True, exist_ok=True)
    planted = _plant_fake_values()

    from tests.test_orchestrator import GeneratedAstra, PlannedCloudAstra

    db_path = str(out / "session.sqlite")
    results: dict[str, dict] = {}
    for name, astra in (("local", GeneratedAstra()), ("cloud_plan", PlannedCloudAstra()), ("recall", GeneratedAstra())):
        print(f"==> {name}", flush=True)
        results[name] = run_scenario(name, astra, out, db_path)
        print(f"    run {results[name]['run_id']} phases={results[name]['phases']} in {results[name]['seconds']}s", flush=True)

    rows = check(results, planted)
    lines = ["# Sprint 3 demo rehearsal (offline, test doubles)", "", f"Checkout: `{ROOT}`  ", f"Output: `{out}`", "", "| Check | Result | Evidence |", "| --- | --- | --- |"]
    for label, ok, evidence in rows:
        lines.append(f"| {label} | {'pass' if ok else 'FAIL'} | {evidence.replace('|', '/')} |")
    summary = "\n".join(lines) + "\n"
    (out / "summary.md").write_text(summary, encoding="utf-8")
    print()
    print(summary)
    failed = [label for label, ok, _ in rows if not ok]
    print(f"rehearsal: {'ALL PASS' if not failed else 'FAILED: ' + '; '.join(failed)}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
