@echo off
REM Claude Code Status Buddy - one-click launcher.
REM Double-click to start the widget. Uses pythonw (no console window) and
REM pins Python 3.13 because 3.14 doesn't have the required packages.
REM cwd is set to this file's folder so library\... imports resolve.

cd /d "%~dp0"
start "" pyw -3.13 claude_screen.py
