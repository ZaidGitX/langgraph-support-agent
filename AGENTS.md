# Agent instructions for langgraph_support_app

This file is read by Cursor and other Agent Skills-compatible tools. Claude Code reads
`CLAUDE.md`, which says the same thing.

- Sprint 3 work on the agentic CI/CD orchestrator follows the project skill at
  `.claude/skills/sprint3-agentic-cicd/SKILL.md` (Cursor loads `.claude/skills/` natively;
  invoke it as `/sprint3-agentic-cicd`). Read its `references/00-baseline-architecture.md`
  first and `references/12-phase-plan.md` to find the current phase.
- Project interpreter: `/opt/anaconda3/envs/tcs_practice_env/bin/python`.
- Gate before saying a phase is done:
  `bash .claude/skills/sprint3-agentic-cicd/scripts/run_sprint3_checks.sh`.
- Hooks for Cursor live in `.cursor/hooks.json` (installed by
  `python3 .claude/skills/sprint3-agentic-cicd/scripts/install_hooks.py`). They deny
  destructive commands, production targets, and secret printing, and ask before cloud,
  registry, or remote mutations.
- Never write a credential value anywhere; reference names only. Never add a production
  target. Only `.github/workflows/orchestrator.yml` may trigger on push.
