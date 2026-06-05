@echo off
REM Stop the widget by reading its PID file. The daemon writes its PID to
REM %USERPROFILE%\.claude\claude-screen.pid on start, so we can kill the
REM exact process even when Windows hides command lines from cross-process queries.
REM Trade-off: forced kill leaves the last frame on the panel until you restart.

set "PIDFILE=%USERPROFILE%\.claude\claude-screen.pid"
if not exist "%PIDFILE%" (
  echo Widget not running ^(no PID file at %PIDFILE%^).
  exit /b 0
)

set /p PID=<"%PIDFILE%"
echo Stopping widget PID %PID%
taskkill /PID %PID% /F >nul 2>&1
del "%PIDFILE%" >nul 2>&1
