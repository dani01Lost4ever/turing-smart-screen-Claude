"""Launch claude_app's MainWindow, let it render + fetch live status, then grab a PNG.

Usage: python tools/capture_app_ui.py [out.png] [state]
  state = working | idle | attention   (forces the buddy state for a deterministic shot)
Run with the same interpreter as the app (py -3.13).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import claude_app

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/app-ui.png")
STATE = sys.argv[2] if len(sys.argv) > 2 else "working"


def main():
    app = QApplication(sys.argv)
    win = claude_app.MainWindow()
    win.preview.set_override_state(STATE)   # deterministic buddy state for the shot
    win.show()

    def grab_and_quit():
        OUT.parent.mkdir(parents=True, exist_ok=True)
        win.grab().save(str(OUT))
        print(f"saved {OUT.resolve()}")
        app.quit()

    # ~8s lets the buddy animate, the gauges compute, and status.claude.com load
    QTimer.singleShot(8000, grab_and_quit)
    app.exec()


if __name__ == "__main__":
    main()
