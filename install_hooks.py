#!/usr/bin/env python3
"""
install_hooks.py - register the claude_screen.py state hooks with Claude Code.

It merges three hooks into your Claude Code settings.json (default: ~/.claude/settings.json):
    UserPromptSubmit -> --set-state working     (mascot speeds up)
    Notification     -> --set-state attention   (flashing alert; only real attention prompts)
    Stop             -> --set-state idle         (calm)

That single user-level file is read by Claude Code whether you launch it from the terminal
OR from inside the desktop app, so both are covered at once.

NOTE on "Claude Desktop": the consumer Claude chat desktop app has no lifecycle-hook system
(it only supports MCP connectors via claude_desktop_config.json), so there is nothing to
register there. This script intentionally does not touch it.

USAGE
    python install_hooks.py --script /abs/path/to/claude_screen.py
    python install_hooks.py --script ... --dry-run      # show changes, write nothing
    python install_hooks.py --remove                    # remove the hooks we added
    optional: --settings /abs/path/settings.json   --python /abs/path/to/pythonw.exe
"""

import os, sys, json, shutil, argparse
from pathlib import Path
from datetime import datetime

# event -> (state, matcher).  matcher "" means "fire on every occurrence".
HOOKS = {
    "UserPromptSubmit": ("working",   ""),
    "Notification":     ("attention", "permission_prompt|idle_prompt|elicitation_dialog"),
    "Stop":             ("idle",      ""),
}
TAG = "claude_screen.py"  # used to recognise (and later remove) our own hooks


def default_python():
    """Prefer a windowless interpreter on Windows so no console flashes on each event."""
    if os.name == "nt":
        cand = Path(sys.executable).with_name("pythonw.exe")
        return str(cand) if cand.exists() else "pythonw"
    return "python3"


def build_command(python_exe, script_path, state):
    return f'{python_exe} "{script_path}" --set-state {state}'


def load_settings(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: {path} is not valid JSON ({e}).\n"
                 "Fix or move it first; refusing to overwrite to avoid losing settings.")


def has_our_hook(groups, state):
    for g in groups:
        for h in g.get("hooks", []):
            cmd = h.get("command", "")
            if TAG in cmd and f"--set-state {state}" in cmd:
                return True
    return False


def install(settings, python_exe, script_path):
    hooks = settings.setdefault("hooks", {})
    added = []
    for event, (state, matcher) in HOOKS.items():
        groups = hooks.setdefault(event, [])
        if has_our_hook(groups, state):
            continue
        group = {"hooks": [{"type": "command",
                            "command": build_command(python_exe, script_path, state)}]}
        if matcher:
            group["matcher"] = matcher
        groups.append(group)
        added.append(f"{event} -> {state}")
    return added


def remove(settings):
    hooks = settings.get("hooks", {})
    removed = []
    for event in list(hooks.keys()):
        new_groups = []
        for g in hooks[event]:
            kept = [h for h in g.get("hooks", []) if TAG not in h.get("command", "")]
            if len(kept) != len(g.get("hooks", [])):
                removed.append(event)
            if kept:
                g["hooks"] = kept
                new_groups.append(g)
            elif not g.get("hooks"):
                pass  # drop empty group
        if new_groups:
            hooks[event] = new_groups
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    return removed


def backup(path):
    if path.exists():
        b = path.with_name(path.name + "." + datetime.now().strftime("%Y%m%d-%H%M%S") + ".bak")
        shutil.copy2(path, b)
        return b
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", help="absolute path to claude_screen.py")
    ap.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"))
    ap.add_argument("--python", default=default_python())
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    settings_path = Path(args.settings).expanduser().resolve()
    settings = load_settings(settings_path)

    if args.remove:
        changed = remove(settings)
        verb = "Would remove" if args.dry_run else "Removed"
        print(f"{verb}: {', '.join(sorted(set(changed))) or 'nothing (no matching hooks found)'}")
    else:
        if not args.script:
            sys.exit("ERROR: --script /abs/path/to/claude_screen.py is required to install.")
        script_path = Path(args.script).expanduser().resolve()
        if not script_path.exists():
            print(f"WARNING: {script_path} does not exist yet (installing the hook anyway).")
        added = install(settings, args.python, str(script_path))
        verb = "Would add" if args.dry_run else "Added"
        print(f"{verb}: {', '.join(added) or 'nothing (already installed)'}")

    if args.dry_run:
        print("\n--- resulting settings.json (not written) ---")
        print(json.dumps(settings, indent=2))
        return

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    b = backup(settings_path)
    if b:
        print(f"Backed up existing settings to {b}")
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {settings_path}")


if __name__ == "__main__":
    main()
