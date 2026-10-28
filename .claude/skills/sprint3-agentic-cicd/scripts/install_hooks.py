#!/usr/bin/env python3
"""Install or remove the sprint 3 hooks for Claude Code and Cursor.

  python3 install_hooks.py                # project: .claude/settings.local.json + .cursor/hooks.json
  python3 install_hooks.py --user         # user: ~/.claude/settings.json + ~/.cursor/hooks.json (absolute paths)
  python3 install_hooks.py --dry-run      # show the merge without writing
  python3 install_hooks.py --uninstall    # remove every entry that points at this skill's hooks

Idempotent: entries are matched by the hook script path. Existing hooks from other tools are
kept. A timestamped backup of each file is written before it changes.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
HOOKS_DIR = SKILL_DIR / "hooks"
MARKER = "sprint3-agentic-cicd/hooks/"


def project_root() -> Path:
    return SKILL_DIR.parents[2]


def load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON ({exc}); fix it before installing hooks")


def write_json(path: Path, data: dict, dry_run: bool) -> None:
    text = json.dumps(data, indent=2) + "\n"
    if dry_run:
        print(f"--- would write {path}\n{text}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            current = None
        if current is not None and json.dumps(current, sort_keys=True) == json.dumps(data, sort_keys=True):
            print(f"unchanged: {path}")
            return
        backup = path.with_name(f"{path.name}.bak-{int(time.time())}")
        shutil.copy2(path, backup)
        print(f"backup: {backup}")
    path.write_text(text, encoding="utf-8")
    print(f"wrote: {path}")


def entry_has_marker(entry: dict) -> bool:
    blob = json.dumps(entry)
    return MARKER in blob


def merge_claude(existing: dict, fragment: dict, user_level: bool, root: Path) -> dict:
    result = copy.deepcopy(existing)
    hooks = result.setdefault("hooks", {})
    for event, groups in fragment["hooks"].items():
        current = hooks.setdefault(event, [])
        current[:] = [g for g in current if not entry_has_marker(g)]
        for group in groups:
            group = copy.deepcopy(group)
            if user_level:
                for h in group.get("hooks", []):
                    h["command"] = h["command"].replace("${CLAUDE_PROJECT_DIR}", str(root))
            current.append(group)
    return result


def merge_cursor(existing: dict, fragment: dict, user_level: bool, root: Path) -> dict:
    result = copy.deepcopy(existing) if existing else {"version": 1, "hooks": {}}
    result.setdefault("version", 1)
    hooks = result.setdefault("hooks", {})
    for event, entries in fragment["hooks"].items():
        current = hooks.setdefault(event, [])
        current[:] = [e for e in current if not entry_has_marker(e)]
        for entry in entries:
            entry = copy.deepcopy(entry)
            if user_level:
                entry["command"] = entry["command"].replace("python3 .claude/", f"python3 {root}/.claude/")
            current.append(entry)
    return result


def strip(existing: dict, cursor: bool) -> dict:
    result = copy.deepcopy(existing)
    hooks = result.get("hooks", {})
    for event in list(hooks):
        hooks[event] = [e for e in hooks[event] if not entry_has_marker(e)]
        if not hooks[event]:
            del hooks[event]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--claude-only", action="store_true")
    parser.add_argument("--cursor-only", action="store_true")
    args = parser.parse_args()
    root = project_root()
    claude_fragment = load_json(HOOKS_DIR / "claude-hooks.json")
    cursor_fragment = load_json(HOOKS_DIR / "cursor-hooks.json")
    if args.user:
        claude_path = Path.home() / ".claude" / "settings.json"
        cursor_path = Path.home() / ".cursor" / "hooks.json"
    else:
        claude_path = root / ".claude" / "settings.local.json"
        cursor_path = root / ".cursor" / "hooks.json"

    if not args.cursor_only:
        existing = load_json(claude_path)
        merged = strip(existing, False) if args.uninstall else merge_claude(existing, claude_fragment, args.user, root)
        write_json(claude_path, merged, args.dry_run)
    if not args.claude_only:
        existing = load_json(cursor_path)
        merged = strip(existing, True) if args.uninstall else merge_cursor(existing, cursor_fragment, args.user, root)
        write_json(cursor_path, merged, args.dry_run)
    if not args.uninstall and not args.dry_run:
        print("done. Claude Code reloads project settings on the next session; Cursor reads .cursor/hooks.json on the next agent run.")
        print("bypass for one command: SPRINT3_HOOKS_OFF=1; strict stop reminders: SPRINT3_STRICT_STOP=1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
