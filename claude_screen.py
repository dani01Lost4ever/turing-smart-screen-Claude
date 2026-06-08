#!/usr/bin/env python3
"""
claude_screen.py - Claude Code status widget for a TURZX / Turing 3.5" USB screen.

MODES
  python claude_screen.py                       run the live display daemon (pushes to COM5)
  python claude_screen.py --preview             render sample frames to ./preview_*.png (no hardware)
  python claude_screen.py --set-state STATE      write the state file (called by Claude Code hooks)
                                                 STATE = attention | working | idle

DATA SOURCES (all local, no API keys, no network)
  * Usage:  ~/.claude/projects/**/*.jsonl   (token usage Claude Code writes locally)
  * State:  ~/.claude/claude-screen-state.json   (hooks write it, the daemon reads it)
"""

import os, sys, json, glob, time, math, argparse, threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter
try:
    from zoneinfo import ZoneInfo            # stdlib >=3.9 ; needs `pip install tzdata` on Windows
except ImportError:
    ZoneInfo = None

# ------------------------------- CONFIG -------------------------------
COM_PORT = "COM5"          # your screen's port (Device Manager showed COM5)
REVISION = "A"             # confirmed for your screen (Turing 3.5 / UsbPCMonitor protocol)
WIDTH, HEIGHT = 480, 320       # we design the dashboard in landscape (what render_frame draws)
NATIVE_W, NATIVE_H = 320, 480  # device's native (portrait) buffer
ROTATE = 90                    # rotate frame to fit the landscape panel; use 270 if upside down
BRIGHTNESS = 50                # 0-100 (rev A can run hot - keep <= 50%)
FRAME_SEC = 0.03           # animation pacing (panel serial throughput is the real limiter)
USAGE_REFRESH_SEC = 20     # recompute token usage this often (triggers one full redraw)
STATS_REFRESH_SEC = 300    # recompute the heavy footer stats this often (off the render loop)
CLAUDE_STATUS_URL = "https://status.claude.com/api/v2/summary.json"
CLAUDE_STATUS_REFRESH_SEC = 60   # poll the Anthropic status page this often

# --- Session gauge: auto-calibrated via claude-monitor (P90 dynamic limit) if available ---
USE_CLAUDE_MONITOR = True             # pip install claude-monitor ; falls back to budget below
PLAN = "max20"                        # pro | max5 | max20 | custom  (your Claude plan)

# Fallback budgets (used only if claude-monitor isn't installed). The weekly gauge always
# uses the local parser below, since claude-monitor doesn't model the weekly cap.
SESSION_WINDOW_HRS = 5
SESSION_TOKEN_BUDGET = 7_000_000      # session fallback only (8M * 74/85 ~= 7M)
# Re-calibrate when the screen % drifts from Claude Code's /usage %:
#   new_budget = old_budget * (screen% / real%)
# 2026-06-05 calibration: widget 26.3M tokens vs Claude reporting 45% -> 58M.
WEEKLY_TOKEN_BUDGET = 58_000_000      # weekly, local parser (fixed-window calibrated)

# Anthropic's weekly cap resets at a FIXED weekday + local hour (set yours below; find it in
# Claude Code via /usage). Anchoring in LOCAL TIME via zoneinfo is deliberate: a fixed UTC
# anchor would drift by 1h after autumn DST. With ZoneInfo it always means "Tuesday 21:00 in
# Rome," whatever the offset. Weekly_tokens snaps to 0 at the boundary so the confetti fires.
WEEKLY_TZ_NAME = "Europe/Rome"
WEEKLY_RESET_WEEKDAY = 1                              # Mon=0 .. Sun=6 ; 1 = Tuesday
WEEKLY_RESET_HOUR = 21                                # local hour-of-day for the reset
WEEKLY_PERIOD = timedelta(days=7)
# Which token fields the LOCAL parser counts (input+output+cache-creation):
COUNT_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens")

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
STATE_FILE = CLAUDE_DIR / "claude-screen-state.json"
PID_FILE = CLAUDE_DIR / "claude-screen.pid"      # daemon PID; stop_widget.bat reads it

ATTENTION_HOLD = 60        # seconds an "attention" state stays hot before auto-clearing
WORKING_HOLD = 45

# palette
BG    = (13, 13, 15)
FG    = (235, 232, 228)
MUTED = (118, 118, 128)
TRACK = (38, 38, 44)
CORAL = (217, 119, 87)     # claude clay
GREEN = (76, 187, 122)
AMBER = (224, 168, 70)
RED   = (224, 86, 86)
# ----------------------------------------------------------------------


def load_font(size, bold=False):
    candidates = (
        ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold else
        ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    )
    for p in candidates:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


FONTS = None
def fonts():
    global FONTS
    if FONTS is None:
        FONTS = {
            "title": load_font(20, bold=True),
            "label": load_font(16, bold=True),
            "pct":   load_font(30, bold=True),
            "small": load_font(13),
            "alert": load_font(22, bold=True),
            "tiny":  load_font(11),               # stat captions
            "stat":  load_font(15, bold=True),    # stat values
            "statbig": load_font(24, bold=True),  # hero stat values
        }
    return FONTS


# ----------------------------- STATE FILE -----------------------------
def write_state(state):
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"state": state, "ts": time.time()}))


def read_state():
    """Return the effective state, applying hold timeouts so it decays back to idle."""
    try:
        d = json.loads(STATE_FILE.read_text())
        state, age = d.get("state", "idle"), time.time() - d.get("ts", 0)
    except Exception:
        return "idle"
    if state == "attention" and age > ATTENTION_HOLD:
        return "idle"
    if state == "working" and age > WORKING_HOLD:
        return "idle"
    return state


# ------------------------------- USAGE --------------------------------
def _weekly_window_start(now_utc):
    """Most recent (WEEKLY_RESET_WEEKDAY, WEEKLY_RESET_HOUR) in WEEKLY_TZ before now.
    Anchored in local time so it survives DST shifts. Falls back to a fixed UTC offset
    if zoneinfo can't find the zone (e.g. Windows without `tzdata` installed)."""
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(WEEKLY_TZ_NAME)
        except Exception:
            tz = timezone(timedelta(hours=2))     # CEST fallback; install `tzdata` for DST safety
    else:
        tz = timezone(timedelta(hours=2))
    local = now_utc.astimezone(tz)
    back = (local.weekday() - WEEKLY_RESET_WEEKDAY) % 7
    cand = local.replace(hour=WEEKLY_RESET_HOUR, minute=0, second=0, microsecond=0) \
                - timedelta(days=back)
    if cand > local:                              # today is reset day but before the reset hour
        cand -= timedelta(days=7)
    return cand.astimezone(timezone.utc)


def _ts(rec):
    t = rec.get("timestamp")
    if not t:
        return None
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _compute_usage_local():
    """Local parser: ~/.claude/projects/**/*.jsonl, dedup by message id, sum tokens per window.
    Used for the weekly gauge always, and for session when claude-monitor isn't available."""
    now = datetime.now(timezone.utc)
    week_start = _weekly_window_start(now)
    weekly_reset_in = max((week_start + WEEKLY_PERIOD) - now, timedelta(0))
    load_cut = week_start - timedelta(days=1)   # a margin so week_start is always covered
    seen = {}  # message id -> (timestamp, tokens) ; keep the largest token count per id
    if PROJECTS_DIR.exists():
        for fp in glob.glob(str(PROJECTS_DIR / "**" / "*.jsonl"), recursive=True):
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        msg = rec.get("message") or {}
                        usage = msg.get("usage")
                        if not usage:
                            continue
                        ts = _ts(rec)
                        if ts is None or ts < load_cut:
                            continue
                        toks = sum(int(usage.get(k, 0) or 0) for k in COUNT_FIELDS)
                        mid = msg.get("id") or f"{fp}:{rec.get('uuid')}"
                        if mid not in seen or toks > seen[mid][1]:
                            seen[mid] = (ts, toks)
            except Exception:
                continue

    week_tokens = sum(t for ts, t in seen.values() if ts >= week_start)

    # Session: model Claude's FIXED 5h block, not a sliding window. A block starts
    # at the first message after an idle gap >= the window length, with its start
    # floored to the hour (matches claude-monitor / Anthropic's on-the-hour reset).
    # Tokens accrue only within the current block and snap to 0 when it ends, so
    # reset_in counts down to the real boundary instead of tracking the oldest
    # message aging out of a rolling lookback.
    win = timedelta(hours=SESSION_WINDOW_HRS)
    blk_start = blk_end = None
    blk_tokens = 0
    prev = None
    for ts, toks in sorted(seen.values()):          # (timestamp, tokens), oldest first
        if blk_start is None or ts >= blk_end or (ts - prev) >= win:
            blk_start = ts.replace(minute=0, second=0, microsecond=0)
            blk_end = blk_start + win
            blk_tokens = 0
        blk_tokens += toks
        prev = ts
    if blk_start is not None and blk_start <= now < blk_end:
        sess_tokens = blk_tokens                    # only the active block's tokens
        reset_in = blk_end - now
    else:
        sess_tokens = 0                             # no active block => fully reset
        reset_in = timedelta(0)
    return {
        "session_tokens": sess_tokens,
        "session_pct": min(sess_tokens / SESSION_TOKEN_BUDGET, 1.0) if SESSION_TOKEN_BUDGET else 0,
        "weekly_tokens": week_tokens,
        "weekly_pct": min(week_tokens / WEEKLY_TOKEN_BUDGET, 1.0) if WEEKLY_TOKEN_BUDGET else 0,
        "reset_in": reset_in,
        "weekly_reset_in": weekly_reset_in,
    }


def _session_via_monitor():
    """Session usage from claude-monitor: real token counting + P90 dynamic limit.
    Returns {session_tokens, session_pct, reset_in} or None if unavailable/empty."""
    try:
        from claude_monitor.data.analysis import analyze_usage
        from claude_monitor.core.plans import get_token_limit
    except Exception:
        return None
    try:
        res = analyze_usage(hours_back=192, use_cache=True, data_path=str(PROJECTS_DIR))
        blocks = res.get("blocks") or []
        if not blocks:
            return None
        now = datetime.now(timezone.utc)
        active = next((b for b in blocks if b.get("isActive")), None)
        limit = get_token_limit(PLAN, blocks) or SESSION_TOKEN_BUDGET   # P90-aware dynamic cap
        if active is None:
            return {"session_tokens": 0, "session_pct": 0.0, "reset_in": timedelta(0)}
        tokens = int(active.get("totalTokens", 0))
        end = datetime.fromisoformat(active["endTime"].replace("Z", "+00:00"))
        reset_in = max(end - now, timedelta(0))
        return {
            "session_tokens": tokens,
            "session_pct": min(tokens / limit, 1.0) if limit else 0.0,
            "reset_in": reset_in,
        }
    except Exception:
        return None


def compute_usage():
    """Weekly always from the local parser; session from claude-monitor when available."""
    usage = _compute_usage_local()
    if USE_CLAUDE_MONITOR:
        session = _session_via_monitor()
        if session:
            usage.update(session)        # override session_tokens / session_pct / reset_in
    return usage


# ---- dashboard-style stats (heavy scan; the daemon refreshes these on a background thread) ----
STATS_WINDOW_DAYS = 49                       # history window for stats + heatmap (7x7 grid)
MODEL_LABELS = [                             # longest-prefix-first, so 4-7 doesn't shadow 4-5
    ("claude-opus-4-8", "Opus 4.8"), ("claude-opus-4-7", "Opus 4.7"),
    ("claude-sonnet-4-6", "Sonnet 4.6"), ("claude-haiku-4-5", "Haiku 4.5"),
]


def _model_label(m):
    if not m:
        return "-"
    for k, v in MODEL_LABELS:
        if m.startswith(k):
            return v
    return m.replace("claude-", "").replace("-", " ").title()


def compute_stats(window_days=STATS_WINDOW_DAYS):
    """Slow-changing dashboard stats from local jsonl: streaks, active days, peak hour,
    favorite model, totals, and a per-day heatmap. Heavier (~secs) than compute_usage, so
    the daemon runs this off the render loop. Day/hour buckets use the machine's local tz."""
    now = datetime.now(timezone.utc)
    tz = datetime.now().astimezone().tzinfo
    cut = now - timedelta(days=window_days)
    cut_ts = cut.timestamp()
    seen, models = {}, {}                    # msg id -> (ts, tokens) ; msg id -> model
    if PROJECTS_DIR.exists():
        for fp in glob.glob(str(PROJECTS_DIR / "**" / "*.jsonl"), recursive=True):
            try:
                if os.path.getmtime(fp) < cut_ts:    # skip files untouched in the window
                    continue
            except OSError:
                continue
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        if '"usage"' not in line:    # cheap prefilter before json.loads
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        msg = rec.get("message") or {}
                        u = msg.get("usage")
                        if not u:
                            continue
                        ts = _ts(rec)
                        if ts is None or ts < cut:
                            continue
                        toks = sum(int(u.get(k, 0) or 0) for k in COUNT_FIELDS)
                        mid = msg.get("id") or f"{fp}:{rec.get('uuid')}"
                        if mid not in seen or toks > seen[mid][1]:
                            seen[mid] = (ts, toks)
                            models[mid] = msg.get("model")
            except Exception:
                continue

    day_tok = {}                             # local date -> tokens
    hour_msgs = [0] * 24                      # local hour -> message count
    model_count = {}
    for mid, (ts, toks) in seen.items():
        lt = ts.astimezone(tz)
        day_tok[lt.date()] = day_tok.get(lt.date(), 0) + toks
        hour_msgs[lt.hour] += 1
        m = models.get(mid)
        if m:
            model_count[m] = model_count.get(m, 0) + 1

    messages = len(seen)
    dayset = set(day_tok)
    today = datetime.now(tz).date()
    # current streak: consecutive active days up to today (tolerate today not started yet)
    cur, d = 0, (today if today in dayset else today - timedelta(days=1))
    while d in dayset:
        cur += 1
        d -= timedelta(days=1)
    # longest streak within the window
    longest = 0
    for d0 in dayset:
        if (d0 - timedelta(days=1)) not in dayset:       # a run starts here
            run, d = 1, d0 + timedelta(days=1)
            while d in dayset:
                run += 1
                d += timedelta(days=1)
            longest = max(longest, run)
    # heatmap: oldest..newest daily tokens for the trailing window
    heat = [day_tok.get(today - timedelta(days=window_days - 1 - i), 0) for i in range(window_days)]

    return {
        "messages": messages,
        "total_tokens": sum(t for _, t in seen.values()),
        "active_days": len(dayset),
        "current_streak": cur,
        "longest_streak": longest,
        "peak_hour": max(range(24), key=lambda h: hour_msgs[h]) if messages else 0,
        "fav_model": _model_label(max(model_count, key=model_count.get)) if model_count else "-",
        "heatmap": heat,
        "window_days": window_days,
    }


# the live frame reads this cache; the daemon refreshes it on a background thread
_STATS = None
_stats_dirty = False        # set when a refresh lands, so the daemon forces one full redraw


def _stats_loop(interval):
    """Background worker: recompute stats periodically so the ~secs scan never blocks
    the animation loop. Updates the _STATS cache and flags a redraw."""
    global _STATS, _stats_dirty
    while True:
        try:
            _STATS = compute_stats()
            _stats_dirty = True
        except Exception:
            pass
        time.sleep(interval)


# Claude services status (status.claude.com summary) — drawn in the top-right of the
# dashboard as a small status chip. Set by _claude_status_loop (daemon) AND by the
# GUI app's StatusPanel; whichever updates more recently wins.
_CLAUDE_STATUS = None        # {"indicator": "none|minor|major|critical|maintenance", ...}

# (label, color) per Atlassian status-page indicator
INDICATOR_CHIPS = {
    "none":        ("OK",     GREEN),
    "minor":       ("MINOR",  AMBER),
    "major":       ("MAJOR",  CORAL),
    "critical":    ("DOWN",   RED),
    "maintenance": ("MAINT",  MUTED),
}


def _claude_status_loop(interval=CLAUDE_STATUS_REFRESH_SEC):
    """Daemon-side background fetcher for status.claude.com. Uses urllib (stdlib) so
    the daemon doesn't depend on Qt or requests. The GUI app does its own fetch and
    writes to the same _CLAUDE_STATUS global, so both can run concurrently."""
    global _CLAUDE_STATUS
    import urllib.request
    while True:
        try:
            req = urllib.request.Request(CLAUDE_STATUS_URL,
                                          headers={"User-Agent": "ClaudeStatusBuddy/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
            st = data.get("status") or {}
            _CLAUDE_STATUS = {
                "indicator": st.get("indicator", "none"),
                "description": st.get("description", ""),
            }
        except Exception:
            pass                # transient network issue; keep last good
        time.sleep(interval)


def fmt_dur(td):
    s = int(td.total_seconds())
    if s <= 0:
        return "ready"
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    if d:
        return f"{d}d{h:02d}h"
    return f"{h}h{m:02d}m" if h else f"{m}m"


def usage_color(pct):
    return GREEN if pct < 0.6 else (AMBER if pct < 0.85 else RED)


# ------------------------------ DRAWING -------------------------------
def rounded_bar(d, x, y, w, h, pct, col):
    rad = h // 2
    d.rounded_rectangle([x, y, x + w, y + h], radius=rad, fill=TRACK)
    fw = max(h, int(w * pct))
    if pct > 0:
        d.rounded_rectangle([x, y, x + fw, y + h], radius=rad, fill=col)


def draw_gauge(d, x, y, w, label, pct, sub, F):
    d.text((x, y), label, font=F["label"], fill=FG)
    col = usage_color(pct)
    pct_txt = f"{int(round(pct * 100))}%"
    pw = d.textlength(pct_txt, font=F["pct"])
    d.text((x + w - pw, y - 8), pct_txt, font=F["pct"], fill=col)
    rounded_bar(d, x, y + 30, w, 14, pct, col)
    if sub:
        d.text((x, y + 50), sub, font=F["small"], fill=MUTED)


# ---- the buddy: a pixel sprite we animate (hover / blink / shuffle / alarmed shake) ----
BUDDY = [
    "..##########..",
    ".############.",
    ".############.",
    ".############.",
    "##############",
    "##############",
    ".############.",
    ".############.",
]
BUDDY_EYES = [(4, 2), (9, 2)]               # (col, row) top of each 1x2 eye
BUDDY_LEGS_L, BUDDY_LEGS_R = [2, 4], [9, 11]
BUDDY_LEG_ROW = 8
BUDDY_LEG_LEN = 3                           # long legs -> leggier silhouette, less "fat"

# placement on the landscape canvas, and the patch we stream every tick
BUDDY_CX, BUDDY_CY, BUDDY_S = 92, 152, 8
# tile must enclose the buddy AND all his motion (hover, hop, squash, shake) AND the
# overhead effects (sleepy z / thinking dots / alert "!"), or they clip at the edge.
BUDDY_TILE = (20, 70, 144, 138)   # (x, y, w, h) — sized to content bbox (29-155, 79-200) + margin


def _blink_open(t, fast):
    interval = 2.4 if fast else 3.6          # seconds between blinks
    bp = t % interval
    if bp > interval - 0.14:
        return 0.0                           # eyes shut
    if bp > interval - 0.24:
        return 0.5                           # eyelid halfway
    return 1.0


def _eye(d, ex, ey, ew, eh, shape):
    """shape '>' or '<' draws a chevron squint inside an ew x eh eye box."""
    w = max(2, ew // 4)
    pts = ([(ex, ey), (ex + ew, ey + eh // 2), (ex, ey + eh)] if shape == ">"
           else [(ex + ew, ey), (ex, ey + eh // 2), (ex + ew, ey + eh)])
    d.line(pts, fill=BG, width=w, joint="curve")


def _glance(t):
    """Idle look-around: drift the eyes left, then right, on a slow cycle (mostly centered)."""
    p = t % 6.0
    if 1.0 < p < 1.5:  return -1.0
    if 1.5 <= p < 2.1: return -0.5
    if 3.2 < p < 3.7:  return 1.0
    if 3.7 <= p < 4.3: return 0.5
    return 0.0


def _draw_overhead(d, state, cx, head_y, s, t):
    """Little thought above the head: thinking dots (working), '!' (attention), sleepy z (idle)."""
    if state == "working":                                   # cycling 1..3 thinking dots
        lit = int(t * 2) % 3 + 1
        for i in range(3):
            x = cx - 11 + i * 9
            col = CORAL if i < lit else TRACK
            d.ellipse([x, head_y - 12, x + 5, head_y - 7], fill=col)
    elif state == "attention":                               # a bobbing exclamation
        y = head_y - 22 + round(2 * math.sin(t * 12))
        d.rectangle([cx - 2, y, cx + 2, y + 12], fill=AMBER)
        d.rectangle([cx - 2, y + 15, cx + 2, y + 19], fill=AMBER)
    else:                                                    # sleepy z rising + fading
        p = t % 3.6
        if p < 1.8:
            prog = p / 1.8
            x = cx + 12 + int(prog * 12)
            y = head_y - 6 - int(prog * 16)
            col = tuple(int(MUTED[i] + (BG[i] - MUTED[i]) * prog) for i in range(3))
            d.line([(x, y), (x + 6, y), (x, y + 6), (x + 6, y + 6)], fill=col, width=2)


def draw_buddy(d, cx, cy, s, t, state):
    """t is elapsed seconds, so motion is smooth and framerate-independent."""
    work = state == "working"
    alert = state == "attention"
    voff = round((4 if work else 3) * math.sin(t * (3.4 if work else 1.7)))    # hover
    if not work and not alert:                                                 # idle happy hop
        hp = t % 7.0
        if hp < 0.5:
            voff -= round(8 * math.sin(hp / 0.5 * math.pi))
    glance = 0.0 if (work or alert) else _glance(t)
    dx = (round(4 * math.sin(t * 22)) if alert else 0) + round(2 * glance)      # shake + lean
    color = RED if alert else CORAL

    W, H = len(BUDDY[0]), len(BUDDY)                          # 14 cols, 8 body rows
    gh = (BUDDY_LEG_ROW + BUDDY_LEG_LEN) * s
    oy = cy - gh // 2 + voff
    baseline = oy + H * s                                     # body bottom; feet plant here

    # squash & stretch (breathing): bottom-anchored, ~volume preserving
    br = math.sin(t * (5.2 if work else 2.2))
    cell_w, cell_h = s * (1.0 - 0.05 * br), s * (1.0 + 0.06 * br)
    left = cx + dx - (W * cell_w) / 2.0
    xe = [round(left + c * cell_w) for c in range(W + 1)]     # column edges
    ye = [round(baseline - (H - r) * cell_h) for r in range(H + 1)]   # row edges (bottom-anchored)

    for r, row in enumerate(BUDDY):                          # body
        for c, ch in enumerate(row):
            if ch == "#":
                d.rectangle([xe[c], ye[r], xe[c + 1] - 1, ye[r + 1] - 1], fill=color)

    step = int(t * 6) % 2                                    # walk shuffle when working
    lift = s // 3
    for grp, gs in ((BUDDY_LEGS_L, 0), (BUDDY_LEGS_R, 1)):
        lf = lift if (work and step == gs) else 0
        for col in grp:
            d.rectangle([xe[col], baseline - lf,
                         xe[col + 1] - 1, baseline + BUDDY_LEG_LEN * s - 1 - lf], fill=color)

    # eyes: >_< squint while working, blinking slits when idle, wide-open when alarmed
    ex_off = round(glance * 3)
    if work:
        for (c, r), shape in zip(BUDDY_EYES, (">", "<")):
            _eye(d, xe[c], ye[r], xe[c + 1] - xe[c], ye[r + 2] - ye[r], shape)
    else:
        openf = 1.0 if alert else _blink_open(t, work)
        for (c, r) in BUDDY_EYES:
            ex0, ex1 = xe[c] + ex_off, xe[c + 1] - 1 + ex_off
            ey0, ey1 = ye[r], ye[r + 2] - 1
            eh = int((ey1 - ey0) * openf)
            if eh > 0:
                d.rectangle([ex0, ey1 - eh, ex1, ey1], fill=BG)

    _draw_overhead(d, state, cx + dx, ye[0], s, t)           # thought above the head


def _fmt_hour(h):
    return f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}"


def _heat_color(frac):
    """Empty cells = TRACK; activity ramps toward CORAL (with a floor so any day shows)."""
    if frac <= 0:
        return TRACK
    t = 0.30 + 0.70 * min(frac, 1.0)
    return tuple(int(TRACK[i] + (CORAL[i] - TRACK[i]) * t) for i in range(3))


def draw_stats(d, stats, F):
    """Dashboard footer: a square activity heatmap (hero, left) + two big stat chips (right)."""
    y0 = 230
    d.line([(14, y0), (WIDTH - 14, y0)], fill=TRACK, width=1)        # divider

    # square heatmap (hero): 7 rows (weekday) x 7 cols (weeks), newest at bottom-right
    heat = stats["heatmap"]
    mx = max(heat) or 1
    cell, gap, hx, hy = 10, 2, 16, 236
    for i, v in enumerate(heat):
        x = hx + (i // 7) * (cell + gap)
        y = hy + (i % 7) * (cell + gap)
        d.rounded_rectangle([x, y, x + cell, y + cell], radius=2, fill=_heat_color(v / mx))
    side = 7 * (cell + gap) - gap

    # two hero stat chips, side by side, vertically centered against the heatmap
    chips = [("STREAK", f"{stats['current_streak']}d"),
             ("TOP MODEL", stats["fav_model"])]
    for idx, (cap, val) in enumerate(chips):
        cx = hx + side + 24 + idx * 172
        d.text((cx, 256), cap, font=F["tiny"], fill=MUTED)
        d.text((cx, 270), val, font=F["statbig"], fill=FG)


def draw_claude_status(d, F):
    """Top-right status chip: 'STATUS' caption + colored dot + indicator word.
    No-op when _CLAUDE_STATUS hasn't been populated yet (first launch / no network)."""
    if not _CLAUDE_STATUS:
        return
    ind = _CLAUDE_STATUS.get("indicator", "none")
    label, color = INDICATOR_CHIPS.get(ind, ("?", MUTED))
    right = WIDTH - 12
    cap_w = d.textlength("STATUS", font=F["tiny"])
    d.text((right - cap_w, 22), "STATUS", font=F["tiny"], fill=MUTED)
    val_w = d.textlength(label, font=F["stat"])
    dot_size, gap = 9, 6
    text_x = right - val_w
    dot_x = text_x - gap - dot_size
    val_y = 36
    d.ellipse([dot_x, val_y + 5, dot_x + dot_size, val_y + 5 + dot_size], fill=color)
    d.text((text_x, val_y), label, font=F["stat"], fill=color)


def render_buddy_tile(t, state):
    """Just the buddy's patch (over plain background) - this is what streams each tick."""
    lx, ly, tw, th = BUDDY_TILE
    tile = Image.new("RGB", (tw, th), BG)
    draw_buddy(ImageDraw.Draw(tile), BUDDY_CX - lx, BUDDY_CY - ly, BUDDY_S, t, state)
    return tile


def render_frame(t, usage, state):
    """The full landscape frame. Background is static per (state, usage); the buddy is
    drawn here too, but the live loop overpaints just his patch afterwards."""
    img = Image.new("RGBA", (WIDTH, HEIGHT), BG + (255,))
    d = ImageDraw.Draw(img)
    F = fonts()

    draw_buddy(d, BUDDY_CX, BUDDY_CY, BUDDY_S, t, state)            # mascot (left)

    d.text((176, 26), "Claude Code", font=F["title"], fill=FG)     # header
    status_line = {"working": "working...", "attention": "needs you!", "idle": "idle"}[state]
    sc = RED if state == "attention" else (CORAL if state == "working" else MUTED)
    d.text((176, 52), status_line, font=F["small"], fill=sc)

    draw_claude_status(d, F)                                       # status.claude.com chip (top-right)

    gx, gw = 176, WIDTH - 176 - 28                                 # gauges (right)
    draw_gauge(d, gx, 86, gw, "5h session", usage["session_pct"],
               f"{usage['session_tokens']:,} tok  -  resets {fmt_dur(usage['reset_in'])}", F)
    draw_gauge(d, gx, 160, gw, "weekly", usage["weekly_pct"],
               f"{usage['weekly_tokens']:,} tok  -  resets {fmt_dur(usage['weekly_reset_in'])}", F)

    if _STATS and state != "attention":                            # dashboard footer (skip in alert)
        draw_stats(d, _STATS, F)

    if state == "attention":                                       # solid alert border + banner
        for i in range(6):
            d.rectangle([i, i, WIDTH - 1 - i, HEIGHT - 1 - i], outline=RED)
        bw = 210
        bx = (WIDTH - bw) // 2
        d.rounded_rectangle([bx, 270, bx + bw, 304], radius=17, fill=RED)
        msg = "! NEEDS YOU !"
        mw = d.textlength(msg, font=F["alert"])
        d.text(((WIDTH - mw) / 2, 275), msg, font=F["alert"], fill=(20, 16, 14))

    return img.convert("RGB")


# ---- confetti celebration when a limit resets --------------------------------
CELEBRATE_SEC = 3.6
CELEBRATE_REGION = (8, 18, 160, 292)        # (x,y,w,h) left/buddy area we stream the party in
RESET_DROP = 0.25                           # a gauge dropping this much = a reset
RESET_MIN = 0.30                            # ...only celebrate if it had been at least this full
CONFETTI_COLORS = [CORAL, GREEN, AMBER, RED, (91, 141, 239), (169, 120, 224), (240, 240, 235)]
_GRAVITY = 430.0


def _confetti(seed):
    import random
    rng = random.Random(seed)
    cx, cy, parts = BUDDY_CX, BUDDY_CY - 12, []
    for _ in range(36):                      # a fountain bursting from the buddy
        ang = rng.uniform(-2.5, -0.65)       # upward-ish (y is down, so negative)
        spd = rng.uniform(130, 320)
        parts.append(dict(x=cx, y=cy, vx=spd * math.cos(ang), vy=spd * math.sin(ang),
                          size=rng.randint(4, 9), col=rng.choice(CONFETTI_COLORS),
                          diamond=rng.random() < 0.5, spin=rng.uniform(2, 7)))
    return parts


def _draw_confetti(d, parts, t):
    for p in parts:
        x = p["x"] + p["vx"] * t
        y = p["y"] + p["vy"] * t + 0.5 * _GRAVITY * t * t
        s = p["size"] * (0.6 + 0.4 * abs(math.sin(p["spin"] * t)))   # flutter
        if p["diamond"]:
            d.polygon([(x, y - s), (x + s, y), (x, y + s), (x - s, y)], fill=p["col"])
        else:
            d.rectangle([x - s / 2, y - s / 2, x + s / 2, y + s / 2], fill=p["col"])


def render_celebration_frame(t, usage, label, parts):
    img = render_frame(t, usage, "working").convert("RGBA")   # dashboard + happy >_< buddy
    d = ImageDraw.Draw(img)
    _draw_confetti(d, parts, t)
    F = fonts()
    lw = d.textlength(label, font=F["alert"])
    ly = 24 + int(4 * math.sin(t * 8))                        # little bounce
    d.text((max(8, BUDDY_CX - lw / 2), ly), label, font=F["alert"], fill=FG)
    return img.convert("RGB")


# ------------------------------ DISPLAY -------------------------------
def init_lcd():
    """Create the LcdComm for your screen. Revision A = Turing 3.5 / UsbPCMonitor."""
    try:
        from library.lcd.lcd_comm import Orientation  # Orientation lives here, not in the rev module
        if REVISION == "A":
            from library.lcd.lcd_comm_rev_a import LcdCommRevA as Lcd
        elif REVISION == "B":
            from library.lcd.lcd_comm_rev_b import LcdCommRevB as Lcd
        else:
            from library.lcd.lcd_comm_rev_c import LcdCommRevC as Lcd  # Turing 2.1"/5"/8.8"
        # construct with the panel's NATIVE (portrait) size; SetOrientation rotates it
        lcd = Lcd(com_port=COM_PORT, display_width=NATIVE_W, display_height=NATIVE_H)
    except Exception as e:
        print("Could not load the display library.\n"
              "Run this script from inside your turing-smart-screen folder\n"
              "(so 'library/...' is importable), and check REVISION matches your screen.\n"
              f"Detail: {e}")
        return None
    lcd.Reset()
    lcd.InitializeComm()
    lcd.SetBrightness(level=BRIGHTNESS)
    # keep the device in its native portrait buffer; we rotate frames ourselves in push()
    lcd.SetOrientation(orientation=Orientation.PORTRAIT)
    lcd.Clear()
    return lcd


def _native_xy(lx, ly, tw, th):
    """Where a landscape tile (lx,ly,tw,th) lands in the rotated native buffer."""
    if ROTATE == 90:
        return ly, WIDTH - lx - tw
    if ROTATE == 270:
        return HEIGHT - ly - th, lx
    return lx, ly            # 0/180 not used for partial updates


def _blit(lcd, img, nx, ny):
    try:
        lcd.DisplayPILImage(img, x=nx, y=ny)
    except (AttributeError, TypeError):
        img.save("_blit.png")
        lcd.DisplayBitmap("_blit.png", x=nx, y=ny)


def send_full(lcd, landscape_img):
    _blit(lcd, landscape_img.rotate(ROTATE, expand=True), 0, 0)


def send_tile(lcd, tile, lx, ly, tw, th):
    nx, ny = _native_xy(lx, ly, tw, th)
    _blit(lcd, tile.rotate(ROTATE, expand=True), nx, ny)


def celebrate(lcd, usage, label):
    """Stream a confetti party in CELEBRATE_REGION, then restore the dashboard."""
    lx, ly, tw, th = CELEBRATE_REGION
    parts = _confetti(int(time.time() * 1000))
    t0 = time.time()
    while time.time() - t0 < CELEBRATE_SEC:
        t = time.time() - t0
        frame = render_celebration_frame(t, usage, label, parts)
        send_tile(lcd, frame.crop((lx, ly, lx + tw, ly + th)), lx, ly, tw, th)
        time.sleep(FRAME_SEC)
    send_full(lcd, render_frame(time.time() - t0, usage, read_state()))   # clean restore


# ------------------------------- MAIN ---------------------------------
def run_daemon():
    global _stats_dirty
    lcd = init_lcd()
    if lcd is None:
        sys.exit(1)
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))          # so stop_widget.bat can find us
    threading.Thread(target=_stats_loop, args=(STATS_REFRESH_SEC,), daemon=True).start()
    threading.Thread(target=_claude_status_loop, daemon=True).start()
    t0 = time.time()
    usage = compute_usage()
    last_usage = t0
    last_state = None
    prev_s, prev_w = usage["session_pct"], usage["weekly_pct"]
    send_full(lcd, render_frame(0.0, usage, read_state()))   # paint everything once
    try:
        while True:
            now = time.time()
            t = now - t0
            state = read_state()
            need_full = state != last_state                  # state change -> repaint bg + colors
            last_state = state
            if _stats_dirty:                                 # fresh footer stats landed
                _stats_dirty = False
                need_full = True
            if now - last_usage > USAGE_REFRESH_SEC:          # refresh gauges occasionally
                usage = compute_usage()
                last_usage = now
                need_full = True
                label = None                                  # detect a limit reset (sharp drop)
                if prev_s >= RESET_MIN and usage["session_pct"] < prev_s - RESET_DROP:
                    label = "5H RESET!"
                elif prev_w >= RESET_MIN and usage["weekly_pct"] < prev_w - RESET_DROP:
                    label = "WEEKLY RESET!"
                prev_s, prev_w = usage["session_pct"], usage["weekly_pct"]
                if label:
                    celebrate(lcd, usage, label)
                    last_state = None
                    continue
            if need_full:
                send_full(lcd, render_frame(t, usage, state))
            else:
                send_tile(lcd, render_buddy_tile(t, state), *BUDDY_TILE)  # fast: just the buddy
            time.sleep(FRAME_SEC)
    except KeyboardInterrupt:
        lcd.Clear()
        lcd.ScreenOff()
    finally:
        try: PID_FILE.unlink()
        except OSError: pass


def run_preview():
    global _STATS
    mock = {"session_tokens": 5_021_440, "session_pct": 0.70,
            "weekly_tokens": 74_400_000, "weekly_pct": 0.37,
            "reset_in": timedelta(hours=2, minutes=14),
            "weekly_reset_in": timedelta(days=3, hours=5)}
    _STATS = compute_stats()                              # real footer stats for the mockup
    for state in ("idle", "working", "attention"):
        render_frame(0.4, mock, state).save(f"preview_{state}.png")
    render_frame(3.55, mock, "idle").save("preview_idle_blink.png")  # caught mid-blink
    print("wrote preview_*.png")


def run_demo():
    """Cycle idle -> working -> attention on the real screen, ignoring hooks, to test visuals."""
    lcd = init_lcd()
    if lcd is None:
        sys.exit(1)
    threading.Thread(target=_stats_loop, args=(STATS_REFRESH_SEC,), daemon=True).start()
    t0 = time.time()
    usage = compute_usage()
    cycle, seg = ["idle", "working", "attention"], 5.0   # seconds per state
    last_state = None
    print("DEMO: cycling idle / working / attention every 5s. Ctrl+C to stop.")
    try:
        while True:
            t = time.time() - t0
            state = cycle[int(t // seg) % len(cycle)]
            if state != last_state:
                last_state = state
                send_full(lcd, render_frame(t, usage, state))
            else:
                send_tile(lcd, render_buddy_tile(t, state), *BUDDY_TILE)
            time.sleep(FRAME_SEC)
    except KeyboardInterrupt:
        lcd.Clear()
        lcd.ScreenOff()


def run_celebrate():
    """Fire the confetti once on the real screen, to test it without waiting for a reset."""
    global _STATS
    lcd = init_lcd()
    if lcd is None:
        sys.exit(1)
    _STATS = compute_stats()                 # so the footer shows in this one-shot test
    usage = compute_usage()
    send_full(lcd, render_frame(0.0, usage, "idle"))
    celebrate(lcd, usage, "5H RESET!")
    print("celebration done")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--demo", action="store_true", help="cycle states on-screen to test visuals")
    ap.add_argument("--celebrate", action="store_true", help="fire the confetti once to test it")
    ap.add_argument("--set-state", choices=["attention", "working", "idle"])
    args = ap.parse_args()
    if args.set_state:
        write_state(args.set_state)
    elif args.preview:
        run_preview()
    elif args.demo:
        run_demo()
    elif args.celebrate:
        run_celebrate()
    else:
        run_daemon()


if __name__ == "__main__":
    main()
