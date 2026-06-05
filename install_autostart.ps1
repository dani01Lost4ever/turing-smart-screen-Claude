# Register a Windows Scheduled Task that auto-starts the widget at login.
# Runs hidden, in the user's interactive session, restarts on failure.
# To remove: run uninstall_autostart.ps1 (or via Task Scheduler -> "ClaudeStatusBuddy").

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyw  = 'C:\Users\dbusetto\AppData\Local\Programs\Python\Launcher\pyw.exe'

if (-not (Test-Path $pyw)) {
    throw "pyw.exe not found at $pyw - install the Python Launcher (it ships with python.org installers)."
}

$action    = New-ScheduledTaskAction -Execute $pyw -Argument '-3.13 claude_screen.py' -WorkingDirectory $here
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings  = New-ScheduledTaskSettingsSet `
                -StartWhenAvailable `
                -DontStopIfGoingOnBatteries `
                -AllowStartIfOnBatteries `
                -RestartCount 3 `
                -RestartInterval (New-TimeSpan -Minutes 1) `
                -ExecutionTimeLimit ([TimeSpan]::Zero) `
                -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
            -Description 'Drives the Turing 3.5 USB panel with Claude Code usage gauges + buddy.'

Register-ScheduledTask -TaskName 'ClaudeStatusBuddy' -InputObject $task -Force | Out-Null
Write-Host 'Installed scheduled task: ClaudeStatusBuddy'
Write-Host "  command : $pyw -3.13 claude_screen.py"
Write-Host "  cwd     : $here"
Write-Host "  trigger : At log on (current user), hidden, auto-restart x3 on crash"
Write-Host ''
Write-Host 'It will start on next login. To run it now without rebooting:'
Write-Host '  Start-ScheduledTask -TaskName ClaudeStatusBuddy'
