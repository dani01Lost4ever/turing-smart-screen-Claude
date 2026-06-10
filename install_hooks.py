#!/usr/bin/env python3
"""
install_hooks.py - register the screen's state hooks with Claude Code.

It merges these hooks into your Claude Code settings.json (default: ~/.claude/settings.json):
    UserPromptSubmit -> working     (you sent a prompt; mascot speeds up)
    PreToolUse[AskUserQuestion] -> attention   (Claude opened a question dialog and is WAITING
                                                for you -> the "needs you" alert; this is the
                                                event that AskUserQuestion does NOT raise as a
                                                Notification, so without it the screen stayed calm)
    PostToolUse      -> working     (a tool finished -> Claude is active again; also fires when
                                     you answer a question, which clears the attention alert. Firing
                                     on every tool keeps the buddy "working" through a long task.)
    Notification     -> attention   (catch-all: permission prompts, idle "waiting for you", MCP
                                     elicitation dialogs)
    Stop             -> idle         (turn ended -> calm)

The hook command points at set_state.py (tiny, no PIL import) so the per-tool-call PostToolUse
hook stays cheap. That single user-level settings file is read by Claude Code whether you launch
from the terminal OR the desktop app / IDE, so all are covered at once.

USAGE
    python install_hooks.py --script /abs/path/to/set_state.py
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
    "PreToolUse":       ("attention", "AskUserQuestion"),
    "PostToolUse":      ("working",   ""),
    "Notification":     ("attention", ""),
    "Stop":             ("idle",      ""),
}
# Substrings that mark a hook as ours, so we can dedupe and cleanly remove/upgrade. Includes the
# legacy "claude_screen.py --set-state" form so re-running this migrates old installs.
TAGS = ("set_state.py", "claude_screen.py --set-state")


def default_python():
    """Prefer a windowless interpreter on Windows so no console flashes on each event."""
    if os.name == "nt":
        cand = Path(sys.executable).with_name("pythonw.exe")
        return str(cand) if cand.exists() else "pythonw"
    return "python3"


def build_command(python_exe, script_path, state):
    return f'{python_exe} "{script_path}" {state}'


def _is_ours(cmd):
    return any(tag in cmd for tag in TAGS)


def _targets_state(cmd, state):
    """True if this (ours) command sets the given state, in either the new positional form
    ('... set_state.py attention') or the legacy form ('... --set-state attention')."""
    return cmd.strip().split()[-1:] == [state] or f"--set-state {state}" in cmd


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
            if _is_ours(cmd) and _targets_state(cmd, state):
                return True
    return False


def install(settings, python_exe, script_path):
    # Drop any of our previous hooks first so re-running upgrades a stale install (e.g. the
    # old claude_screen.py form, or a changed matcher) instead of leaving duplicates behind.
    remove(settings)
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
        added.append(f"{event}{f'[{matcher}]' if matcher else ''} -> {state}")
    return added


def remove(settings):
    hooks = settings.get("hooks", {})
    removed = []
    for event in list(hooks.keys()):
        new_groups = []
        for g in hooks[event]:
            kept = [h for h in g.get("hooks", []) if not _is_ours(h.get("command", ""))]
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
