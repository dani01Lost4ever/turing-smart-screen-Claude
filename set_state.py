#!/usr/bin/env python3
"""set_state.py - the widget's hook entry point. Writes ~/.claude/claude-screen-state.json
so the running daemon / desktop app can reflect Claude Code's lifecycle on the screen.

This is deliberately tiny and dependency-free (no PIL, no claude_screen import): the hooks
fire on EVERY tool call, so each invocation must be just Python startup + a small JSON write,
not a ~150ms PIL import. claude_screen.py keeps its own --set-state for backwards compat.

    python set_state.py attention | working | idle
"""
import sys, json, time
from pathlib import Path

STATE_FILE = Path.home() / ".claude" / "claude-screen-state.json"


def main():
    state = sys.argv[1] if len(sys.argv) > 1 else "idle"
    if state not in ("attention", "working", "idle"):
        sys.exit(f"usage: set_state.py attention|working|idle (got {state!r})")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"state": state, "ts": time.time()}))


if __name__ == "__main__":
    main()
