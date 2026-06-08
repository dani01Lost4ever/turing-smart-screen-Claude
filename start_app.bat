@echo off
REM Claude Code Status Buddy - PySide6 desktop app (Phase 1).
REM Double-click to launch. Uses pythonw (no console window) and pins Python 3.13.
REM cwd is set to this file's folder so library\... and claude_screen imports resolve.

cd /d "%~dp0"
start "" pyw -3.13 claude_app.py
