#!/usr/bin/env python3
"""
claude_app.py - PySide6 GUI for the Claude Code Status Buddy.

A desktop window that hosts the same dashboard renderer as claude_screen.py
(reusing render_frame / compute_usage / compute_stats / draw_buddy). Phase 1:
live preview + state override menu + background stats. Settings, status.claude
component, and panel-push come in subsequent tasks.

Run with:
    pyw -3.13 claude_app.py    # no console (normal use)
    py  -3.13 claude_app.py    # with console (debugging)
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QAction, QColor, QImage, QKeySequence, QPixmap
from PySide6.QtNetwork import (QNetworkAccessManager, QNetworkReply, QNetworkRequest)
from PySide6.QtWidgets import (QApplication, QColorDialog, QComboBox, QDockWidget,
                                QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                                QLabel, QMainWindow, QMessageBox, QPushButton,
                                QScrollArea, QSpinBox, QStyle, QToolBar, QVBoxLayout,
                                QWidget)

import claude_screen as cs


# Settings persisted to disk (read by load_theme, written by save_theme).
THEME_KEYS = ("BG", "FG", "MUTED", "TRACK", "CORAL", "GREEN", "AMBER", "RED",
              "WEEKLY_RESET_WEEKDAY", "WEEKLY_RESET_HOUR",
              "SESSION_TOKEN_BUDGET", "WEEKLY_TOKEN_BUDGET",
              "BRIGHTNESS")
# Snapshot defaults at import time so Reset can restore them.
THEME_DEFAULTS = {k: getattr(cs, k) for k in THEME_KEYS}
DEFAULT_THEME_PATH = cs.CLAUDE_DIR / "claude-app-theme.json"


def save_theme(path):
    data = {k: list(v) if isinstance(v, tuple) else v for k, v in
            ((k, getattr(cs, k)) for k in THEME_KEYS)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def load_theme(path):
    """Apply a theme JSON to the claude_screen globals. Silently skips unknown keys
    (so older themes load against newer code without errors)."""
    data = json.loads(path.read_text())
    for k, v in data.items():
        if k not in THEME_KEYS:
            continue
        if isinstance(v, list) and len(v) == 3:      # color tuple
            v = tuple(int(x) for x in v)
        setattr(cs, k, v)


def reset_theme():
    for k, v in THEME_DEFAULTS.items():
        setattr(cs, k, v)


# Palette knobs the settings panel exposes. Order = display order.
PALETTE_KEYS = ("BG", "FG", "MUTED", "TRACK", "CORAL", "GREEN", "AMBER", "RED")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class NoScrollSpinBox(QSpinBox):
    """QSpinBox that ignores wheel events unless explicitly focused. Stops the spinbox
    from eating the parent ScrollArea's scroll and silently bumping values when you
    hover-scroll past it. To change the value: click first, then scroll/arrow/type."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class NoScrollComboBox(QComboBox):
    """Same wheel-protection rule as NoScrollSpinBox, for the weekday selector."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


def pil_to_qpixmap(img):
    """PIL.Image -> QPixmap via RGBA. .copy() detaches the QImage from the temp buffer."""
    rgba = img.convert("RGBA")
    qimg = QImage(rgba.tobytes(), rgba.width, rgba.height, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qimg.copy())


class DashboardPreview(QLabel):
    """Renders the dashboard live at ~30Hz. State follows ~/.claude/claude-screen-state.json
    (so the Claude Code hooks still work) unless overridden via set_override_state()."""

    # Emits the full landscape PIL frame + state + whether the panel needs a full redraw
    # (so the panel controller can choose send_full vs send_tile).
    frameReady = Signal(object, str, bool)

    def __init__(self):
        super().__init__()
        self.setFixedSize(cs.WIDTH, cs.HEIGHT)
        self.setStyleSheet("background: black; border: 1px solid #333;")
        self._t0 = time.monotonic()
        self._usage = cs.compute_usage()
        self._last_usage = time.monotonic()
        self._override = None
        self._last_state = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)                       # ~30 fps

    def set_override_state(self, state):
        """None = follow the state file ; 'idle'/'working'/'attention' = force."""
        self._override = state

    def force_usage_refresh(self):
        """Recompute usage on the very next tick (used when a weekly setting changes)."""
        self._last_usage = 0

    def current_state(self):
        return self._override or cs.read_state()

    def _tick(self):
        now = time.monotonic()
        usage_refreshed = False
        if now - self._last_usage > cs.USAGE_REFRESH_SEC:
            self._usage = cs.compute_usage()
            self._last_usage = now
            usage_refreshed = True
        state = self.current_state()
        img = cs.render_frame(now - self._t0, self._usage, state)
        self.setPixmap(pil_to_qpixmap(img))
        # Panel needs a full redraw on state changes or after a usage refresh
        # (gauges moved); otherwise the buddy tile is enough.
        need_full = usage_refreshed or state != self._last_state
        self._last_state = state
        self.frameReady.emit(img, state, need_full)


class ColorSwatch(QPushButton):
    """Tiny color button: shows the current color, opens a picker on click,
    and writes the new RGB tuple back to a claude_screen module global."""
    colorChanged = Signal()

    def __init__(self, attr_name):
        super().__init__()
        self._attr = attr_name
        self.setFixedSize(60, 22)
        self.setFlat(True)
        self.setCursor(Qt.PointingHandCursor)
        self._refresh()
        self.clicked.connect(self._pick)

    def _current_rgb(self):
        return getattr(cs, self._attr)

    def _refresh(self):
        r, g, b = self._current_rgb()
        self.setStyleSheet(f"background:rgb({r},{g},{b}); border:1px solid #444; border-radius:3px;")

    def _pick(self):
        r, g, b = self._current_rgb()
        col = QColorDialog.getColor(QColor(r, g, b), self, f"Pick {self._attr}")
        if col.isValid():
            setattr(cs, self._attr, (col.red(), col.green(), col.blue()))
            self._refresh()
            self.colorChanged.emit()


class SettingsPanel(QWidget):
    """Live-apply controls for palette, weekly reset, weekly budget, panel brightness.
    Mutating any control writes the new value to claude_screen's module globals so the
    next render tick reflects it. Weekly changes also force an immediate usage recompute."""

    weeklyChanged = Signal()              # fires when a weekly-related setting changes
    changed = Signal()                    # fires on ANY change (used for auto-save)

    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(10)

        # Palette
        gb = QGroupBox("Palette")
        grid = QFormLayout(gb)
        grid.setLabelAlignment(Qt.AlignRight)
        self._swatches = []
        for key in PALETTE_KEYS:
            sw = ColorSwatch(key)
            sw.colorChanged.connect(self.changed)
            grid.addRow(QLabel(key), sw)
            self._swatches.append(sw)
        outer.addWidget(gb)

        # Weekly reset (weekday + hour, in WEEKLY_TZ_NAME local time)
        gb = QGroupBox(f"Weekly reset ({cs.WEEKLY_TZ_NAME})")
        f = QFormLayout(gb)
        self._wd = NoScrollComboBox()
        self._wd.addItems(WEEKDAYS)
        self._wd.setCurrentIndex(cs.WEEKLY_RESET_WEEKDAY)
        self._wd.currentIndexChanged.connect(self._on_weekday)
        f.addRow("Day", self._wd)

        self._hr = NoScrollSpinBox()
        self._hr.setRange(0, 23)
        self._hr.setSuffix(":00")
        self._hr.setValue(cs.WEEKLY_RESET_HOUR)
        self._hr.valueChanged.connect(self._on_hour)
        f.addRow("Hour", self._hr)
        outer.addWidget(gb)

        # Token budgets (re-calibration without editing code). Formula:
        # new_budget = old_budget * (screen% / real% from Claude's /usage).
        gb = QGroupBox("Token budgets (calibration)")
        f = QFormLayout(gb)
        self._sbudget = NoScrollSpinBox()
        self._sbudget.setRange(1, 1_000_000_000)
        self._sbudget.setSingleStep(500_000)
        self._sbudget.setSuffix(" tokens")
        self._sbudget.setGroupSeparatorShown(True)
        self._sbudget.setValue(cs.SESSION_TOKEN_BUDGET)
        self._sbudget.valueChanged.connect(self._on_session_budget)
        f.addRow("5h session", self._sbudget)

        self._budget = NoScrollSpinBox()
        self._budget.setRange(1, 1_000_000_000)
        self._budget.setSingleStep(1_000_000)
        self._budget.setSuffix(" tokens")
        self._budget.setGroupSeparatorShown(True)
        self._budget.setValue(cs.WEEKLY_TOKEN_BUDGET)
        self._budget.valueChanged.connect(self._on_budget)
        f.addRow("Weekly", self._budget)
        outer.addWidget(gb)

        # Panel brightness (only meaningful when panel push is on; harmless otherwise)
        gb = QGroupBox("Panel brightness")
        f = QFormLayout(gb)
        self._bright = NoScrollSpinBox()
        self._bright.setRange(0, 100)
        self._bright.setSuffix(" %")
        self._bright.setValue(cs.BRIGHTNESS)
        self._bright.valueChanged.connect(self._on_brightness)
        f.addRow("Level", self._bright)
        outer.addWidget(gb)

        outer.addStretch()

    def _on_weekday(self, idx):
        cs.WEEKLY_RESET_WEEKDAY = idx
        self.weeklyChanged.emit()
        self.changed.emit()

    def _on_hour(self, h):
        cs.WEEKLY_RESET_HOUR = h
        self.weeklyChanged.emit()
        self.changed.emit()

    def _on_budget(self, b):
        cs.WEEKLY_TOKEN_BUDGET = b
        self.weeklyChanged.emit()
        self.changed.emit()

    def _on_session_budget(self, b):
        cs.SESSION_TOKEN_BUDGET = b
        self.weeklyChanged.emit()            # forces a usage recompute so the gauge updates now
        self.changed.emit()

    def _on_brightness(self, b):
        cs.BRIGHTNESS = b
        self.changed.emit()
        # MainWindow connects this signal to PanelController.set_brightness for live-apply.

    def reload_from_globals(self):
        """Repaint all controls from the current cs.* values WITHOUT firing change
        signals. Used after loading a theme so we don't spuriously re-save / recompute."""
        for sw in self._swatches:
            sw._refresh()
        self._wd.blockSignals(True);      self._wd.setCurrentIndex(cs.WEEKLY_RESET_WEEKDAY);  self._wd.blockSignals(False)
        self._hr.blockSignals(True);      self._hr.setValue(cs.WEEKLY_RESET_HOUR);             self._hr.blockSignals(False)
        self._sbudget.blockSignals(True); self._sbudget.setValue(cs.SESSION_TOKEN_BUDGET);     self._sbudget.blockSignals(False)
        self._budget.blockSignals(True);  self._budget.setValue(cs.WEEKLY_TOKEN_BUDGET);       self._budget.blockSignals(False)
        self._bright.blockSignals(True);  self._bright.setValue(cs.BRIGHTNESS);                self._bright.blockSignals(False)


# Atlassian Statuspage indicator -> RGB. See https://status.claude.com/api/v2/summary.json.
INDICATOR_COLORS = {
    "none":        (76, 187, 122),   # GREEN
    "minor":       (224, 168, 70),   # AMBER
    "major":       (217, 119, 87),   # CORAL
    "critical":    (224, 86, 86),    # RED
    "maintenance": (118, 118, 128),  # MUTED
}
COMPONENT_COLORS = {
    "operational":          (76, 187, 122),
    "degraded_performance": (224, 168, 70),
    "partial_outage":       (217, 119, 87),
    "major_outage":         (224, 86, 86),
    "under_maintenance":    (118, 118, 128),
}


def _short_component(name):
    """'Claude Console (platform.claude.com)' -> 'Console'  ;  keep 'claude.ai' intact."""
    s = name.split(" (")[0]
    return s if s == "claude.ai" else s.replace("Claude ", "")


class StatusPanel(QWidget):
    """Polls status.claude.com (Atlassian Statuspage v2 summary) on a timer and renders
    a compact strip: overall indicator + per-component dots + active-incident line.
    Uses QNetworkAccessManager so network I/O stays on the Qt event loop (no thread)."""

    REFRESH_SEC = 60
    URL = QUrl("https://status.claude.com/api/v2/summary.json")

    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        self._indicator = QLabel("status: connecting…")
        outer.addWidget(self._indicator)

        self._comp_row = QWidget()
        QHBoxLayout(self._comp_row).setContentsMargins(0, 0, 0, 0)
        self._comp_row.layout().setSpacing(14)
        outer.addWidget(self._comp_row)

        self._incident = QLabel("")
        self._incident.setStyleSheet("color: rgb(224, 86, 86);")
        self._incident.setWordWrap(True)
        self._incident.hide()
        outer.addWidget(self._incident)

        self._net = QNetworkAccessManager(self)
        self._net.finished.connect(self._on_reply)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._fetch)
        self._timer.start(self.REFRESH_SEC * 1000)
        self._fetch()

    def _fetch(self):
        req = QNetworkRequest(self.URL)
        req.setHeader(QNetworkRequest.UserAgentHeader, b"ClaudeStatusBuddy/1.0")
        self._net.get(req)

    def _on_reply(self, reply):
        if reply.error() != QNetworkReply.NoError:
            self._indicator.setText(f"status: offline ({reply.errorString()})")
            reply.deleteLater()
            return
        try:
            data = json.loads(bytes(reply.readAll()).decode("utf-8", errors="ignore"))
        except Exception as e:
            self._indicator.setText(f"status: parse error ({e})")
            reply.deleteLater()
            return
        self._apply(data)
        reply.deleteLater()

    def _apply(self, data):
        st = data.get("status") or {}
        ind = st.get("indicator", "none")
        desc = st.get("description", "Unknown")
        # share with the renderer so the dashboard + panel show the status chip too
        cs._CLAUDE_STATUS = {"indicator": ind, "description": desc}
        r, g, b = INDICATOR_COLORS.get(ind, (118, 118, 128))
        self._indicator.setText(
            f'<span style="color: rgb({r},{g},{b}); font-size:18px;">●</span> '
            f'<span style="font-weight:bold;">{desc}</span>'
        )

        # Rebuild the component row (small list, fine to clear-and-fill)
        layout = self._comp_row.layout()
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for c in data.get("components", []):
            if c.get("group"):                                 # skip group containers
                continue
            cr, cg, cb = COMPONENT_COLORS.get(c["status"], (118, 118, 128))
            lab = QLabel(
                f'<span style="color: rgb({cr},{cg},{cb});">●</span> {_short_component(c["name"])}'
            )
            lab.setToolTip(f'{c["name"]}: {c["status"].replace("_", " ")}')
            layout.addWidget(lab)
        layout.addStretch()

        incidents = data.get("incidents") or []
        if incidents:
            inc = incidents[0]
            update = (inc.get("incident_updates") or [{}])[0].get("body", "") or ""
            self._incident.setText(f'⚠ {inc.get("name", "Incident")} - {update[:200]}')
            self._incident.show()
        else:
            self._incident.hide()


class PanelController(QObject):
    """Owns the USB panel when connected. Streams frames from the app on a background
    worker so serial-write latency doesn't stutter the GUI. Coordinates with any running
    claude_screen daemon via the PID file (kills it before opening COM5)."""

    connectionChanged = Signal(bool, str)         # connected, status text

    def __init__(self):
        super().__init__()
        self.lcd = None
        self._queue = queue.Queue(maxsize=2)      # bounded -> auto-drop frames if serial lags
        self._stop = threading.Event()
        self._worker = None

    @property
    def connected(self):
        return self.lcd is not None

    def _stop_daemon_if_running(self):
        """If another claude_screen process holds the port (via PID file), kill it."""
        try:
            pid = int(cs.PID_FILE.read_text().strip())
        except Exception:
            return
        # Verify alive, then taskkill (Windows) / SIGTERM elsewhere
        alive = False
        try:
            os.kill(pid, 0)
            alive = True
        except (ProcessLookupError, OSError):
            alive = False
        if alive:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                capture_output=True)
            else:
                try:
                    os.kill(pid, 15)
                except OSError:
                    pass
            # Wait for the process to actually die and release the USB serial handle. A force
            # kill is async, so opening the port too soon races and init fails; ~2s is a safe
            # margin on Windows. (init_lcd now degrades gracefully if it's still held anyway.)
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                    time.sleep(0.1)
                except OSError:
                    break                         # process gone
            time.sleep(0.4)                       # small extra margin for the handle to release
        try:
            cs.PID_FILE.unlink()
        except OSError:
            pass

    def connect_panel(self):
        if self.connected:
            return
        self._stop_daemon_if_running()
        self.lcd = cs.init_lcd()                  # slow: Reset + InitializeComm (~5s)
        if self.lcd is None:
            self.connectionChanged.emit(False, "Panel: failed to open (check COM port)")
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()
        self.connectionChanged.emit(True, f"Panel: connected on {cs.COM_PORT}")

    def disconnect_panel(self):
        if not self.connected:
            return
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=2.0)
            self._worker = None
        try:
            self.lcd.Clear()
            self.lcd.ScreenOff()
        except Exception:
            pass
        self.lcd = None
        self.connectionChanged.emit(False, "Panel: disconnected")

    def set_brightness(self, level):
        if self.lcd is not None:
            try:
                self.lcd.SetBrightness(level=level)
            except Exception:
                pass

    def on_frame_ready(self, img, state, need_full):
        if not self.connected:
            return
        try:
            self._queue.put_nowait((img, state, need_full))
        except queue.Full:
            pass                                  # serial too slow; drop this frame

    def _run(self):
        """Worker loop: pop frames and write to the panel. Tile-only fast path for
        the buddy animation; full-frame when state changes or gauges move."""
        last_state = None
        while not self._stop.is_set():
            try:
                img, state, need_full = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if need_full or state != last_state:
                    cs.send_full(self.lcd, img)
                    last_state = state
                else:
                    lx, ly, tw, th = cs.BUDDY_TILE
                    cs.send_tile(self.lcd, img.crop((lx, ly, lx + tw, ly + th)),
                                  lx, ly, tw, th)
            except Exception:
                pass                              # transient serial error; keep going


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Claude Status Buddy")
        self.preview = DashboardPreview()

        self.status_panel = StatusPanel()

        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(12, 12, 12, 12)
        v.addWidget(self.preview, alignment=Qt.AlignCenter)
        v.addWidget(self.status_panel)
        v.addStretch()
        self.setCentralWidget(central)

        # State menu: manual override or follow hooks
        m = self.menuBar().addMenu("&State")
        for label, state in (("Follow &hooks", None),
                             ("Force &idle", "idle"),
                             ("Force &working", "working"),
                             ("Force &attention", "attention")):
            a = QAction(label, self)
            a.triggered.connect(lambda _checked=False, s=state: self.preview.set_override_state(s))
            m.addAction(a)

        # Toolbar: Save / Undo / Redo / panel toggle
        self.panel = PanelController()
        tb = QToolBar("Main")
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        style = self.style()
        self._act_save = QAction(style.standardIcon(QStyle.SP_DialogSaveButton), "Save", self)
        self._act_save.setShortcut(QKeySequence.Save)                       # Ctrl+S
        self._act_save.triggered.connect(self._save)
        self._act_save.setEnabled(False)                                     # nothing to save yet
        tb.addAction(self._act_save)
        self._act_undo = QAction(style.standardIcon(QStyle.SP_ArrowBack), "Undo", self)
        self._act_undo.setShortcut(QKeySequence.Undo)                       # Ctrl+Z
        self._act_undo.triggered.connect(self._undo)
        self._act_undo.setEnabled(False)
        tb.addAction(self._act_undo)
        self._act_redo = QAction(style.standardIcon(QStyle.SP_ArrowForward), "Redo", self)
        self._act_redo.setShortcut(QKeySequence.Redo)                       # Ctrl+Y / Ctrl+Shift+Z
        self._act_redo.triggered.connect(self._redo)
        self._act_redo.setEnabled(False)
        tb.addAction(self._act_redo)
        tb.addSeparator()
        self._panel_action = QAction("Connect to panel", self, checkable=True)
        self._panel_action.toggled.connect(self._toggle_panel)
        tb.addAction(self._panel_action)
        self.panel.connectionChanged.connect(self._on_panel_changed)
        # Preview's frames feed the panel controller (it drops if not connected)
        self.preview.frameReady.connect(self.panel.on_frame_ready)

        # Settings dock (right side) - palette / weekly anchor / budget / brightness
        self.settings = SettingsPanel()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.settings)
        dock = QDockWidget("Settings", self)
        dock.setWidget(scroll)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)
        # When a weekly setting changes, force the preview to recompute usage on the next tick
        self.settings.weeklyChanged.connect(self.preview.force_usage_refresh)
        # When the brightness slider moves, push it to the panel if connected
        self.settings._bright.valueChanged.connect(self.panel.set_brightness)

        # Theme menu: Save As / Open / Reset to defaults. Auto-save/load uses
        # DEFAULT_THEME_PATH so power-user named themes don't clobber the daily one.
        tm = self.menuBar().addMenu("&Theme")
        for label, slot in (("&Open…", self._theme_open),
                            ("&Save As…", self._theme_save_as),
                            ("&Reset to defaults", self._theme_reset)):
            a = QAction(label, self)
            a.triggered.connect(slot)
            tm.addAction(a)

        # Auto-load the last-saved theme on startup (settings panel is the source of truth
        # after this point; nothing is written back to disk until Save is hit)
        if DEFAULT_THEME_PATH.exists():
            try:
                load_theme(DEFAULT_THEME_PATH)
                self.settings.reload_from_globals()
                self.preview.force_usage_refresh()
            except Exception as e:
                print(f"warning: could not load theme {DEFAULT_THEME_PATH}: {e}", file=sys.stderr)

        # Undo/redo + dirty tracking. _baseline is the on-disk-saved state; _last is the
        # state we'd revert to if the next change happened (== current state right now).
        self._baseline = self._snapshot()
        self._last = self._snapshot()
        self._undo_stack = []
        self._redo_stack = []
        self._update_title()
        # Wire AFTER initial sync so we don't snapshot a no-op
        self.settings.changed.connect(self._on_settings_changed)

        self.statusBar().showMessage("Ready - state follows ~/.claude/claude-screen-state.json")
        self.resize(860, 460)

        # Background stats refresh (same daemon-style loop the widget uses)
        threading.Thread(target=cs._stats_loop, args=(cs.STATS_REFRESH_SEC,), daemon=True).start()

    def _toggle_panel(self, on):
        if on:
            self._panel_action.setText("Disconnecting…")
            self._panel_action.setEnabled(False)
            QApplication.processEvents()
            self.panel.connect_panel()
            self._panel_action.setEnabled(True)
            if not self.panel.connected:                      # init failed, revert toggle
                self._panel_action.blockSignals(True)
                self._panel_action.setChecked(False)
                self._panel_action.blockSignals(False)
        else:
            self.panel.disconnect_panel()

    def _on_panel_changed(self, connected, msg):
        self.statusBar().showMessage(msg, 5000)
        self._panel_action.setText("Disconnect panel" if connected else "Connect to panel")

    def _theme_open(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open theme", str(cs.CLAUDE_DIR),
                                                "Theme files (*.json);;All files (*.*)")
        if not path:
            return
        try:
            load_theme(Path(path))
        except Exception as e:
            QMessageBox.warning(self, "Open theme", f"Could not load theme:\n{e}")
            return
        self.settings.reload_from_globals()
        self.preview.force_usage_refresh()
        save_theme(DEFAULT_THEME_PATH)            # promote to current
        self.statusBar().showMessage(f"Loaded theme: {path}", 5000)

    def _theme_save_as(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save theme as", str(cs.CLAUDE_DIR / "my-theme.json"),
                                                "Theme files (*.json)")
        if not path:
            return
        save_theme(Path(path))
        self.statusBar().showMessage(f"Saved theme: {path}", 5000)

    def _theme_reset(self):
        reset_theme()
        self.settings.reload_from_globals()
        self.preview.force_usage_refresh()
        # Reset behaves like any other change: undoable, dirty, NOT auto-saved
        self._on_settings_changed()
        self.statusBar().showMessage("Theme reset (Ctrl+S to keep, Ctrl+Z to undo)", 5000)

    # ---- undo / redo / save plumbing ------------------------------------------------

    def _snapshot(self):
        """Capture the current claude_screen.* globals that the theme owns."""
        return {k: getattr(cs, k) for k in THEME_KEYS}

    def _apply_snapshot(self, snap):
        """Reverse direction: push the snapshot back into globals + refresh UI WITHOUT
        firing the settings.changed signal (reload_from_globals uses blockSignals)."""
        for k, v in snap.items():
            setattr(cs, k, tuple(v) if isinstance(v, list) else v)
        self.settings.reload_from_globals()
        self.preview.force_usage_refresh()

    def _on_settings_changed(self):
        """Wired to SettingsPanel.changed. Records the previous state for undo."""
        self._undo_stack.append(self._last)
        if len(self._undo_stack) > 100:
            self._undo_stack.pop(0)
        self._redo_stack.clear()
        self._last = self._snapshot()
        self._update_title()

    def _undo(self):
        if not self._undo_stack:
            return
        self._redo_stack.append(self._snapshot())
        snap = self._undo_stack.pop()
        self._apply_snapshot(snap)
        self._last = snap
        self._update_title()
        self.statusBar().showMessage("Undone", 2000)

    def _redo(self):
        if not self._redo_stack:
            return
        self._undo_stack.append(self._snapshot())
        snap = self._redo_stack.pop()
        self._apply_snapshot(snap)
        self._last = snap
        self._update_title()
        self.statusBar().showMessage("Redone", 2000)

    def _save(self):
        save_theme(DEFAULT_THEME_PATH)
        self._baseline = self._snapshot()
        self._update_title()
        self.statusBar().showMessage(f"Saved -> {DEFAULT_THEME_PATH}", 5000)

    @property
    def _dirty(self):
        return self._snapshot() != self._baseline

    def _update_title(self):
        base = "Claude Status Buddy"
        self.setWindowTitle(f"• {base}" if self._dirty else base)
        self._act_save.setEnabled(self._dirty)
        self._act_undo.setEnabled(bool(self._undo_stack))
        self._act_redo.setEnabled(bool(self._redo_stack))

    def closeEvent(self, event):
        # Prompt if there are unsaved theme changes
        if self._dirty:
            choice = QMessageBox.question(
                self, "Unsaved changes",
                "You have unsaved theme changes. Save before closing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if choice == QMessageBox.Cancel:
                event.ignore()
                return
            if choice == QMessageBox.Save:
                self._save()
        # Release the panel cleanly so it doesn't freeze on the last frame
        if self.panel.connected:
            self.panel.disconnect_panel()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
