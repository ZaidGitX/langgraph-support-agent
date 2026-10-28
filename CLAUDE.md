# langgraph_support_app — project notes for Claude Code

Two products in one process: a customer-support agent and an agentic CI/CD orchestrator
(Astra plans, Jev routes through TypeSafe, LangGraph supervises). `docs/SYSTEM_MAP.md` is
the map of what exists; `docs/ORCHESTRATOR_RUNBOOK.md` is how to run it.

## Sprint 3

All orchestrator, workflow, provider, dashboard, and demo work follows the project skill
`.claude/skills/sprint3-agentic-cicd` (invoke `/sprint3-agentic-cicd`). It is the single
source of truth for the sprint: phase plan, component specs, scripts, templates, catalogs,
and guardrails. Start every session with:

```bash
python3 .claude/skills/sprint3-agentic-cicd/scripts/preflight.py --brief
python3 .claude/skills/sprint3-agentic-cicd/scripts/sprint3_progress.py
```

Install the project hooks once (they guard shell commands and edits in Claude Code and Cursor):

```bash
python3 .claude/skills/sprint3-agentic-cicd/scripts/install_hooks.py
```

## Environment

- Interpreter: `/opt/anaconda3/envs/tcs_practice_env/bin/python` (conda env
  `tcs_practice_env`). Use it for `pytest`, `ruff`, `uvicorn`, and `python -m orchestrator.*`.
- Checks: `bash .claude/skills/sprint3-agentic-cicd/scripts/run_sprint3_checks.sh`
- Frontend: `cd frontend && npm run build` (Node 26).
- `.env` holds the app keys; never print values, never commit `.env`, `var/`, or `dist/`.

## Invariants (tested)

No production edge; `uat` is last; only `orchestrator.yml` runs on push; Jev decides edges
with TypeSafe `Choice`/`Noul`, never a ChatOpenAI fallback; only approved stages run;
retries cap at 2; secrets by placeholder name only; generated commands pass the policy
before approval and before execution.
