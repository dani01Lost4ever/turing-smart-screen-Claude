# Remove the auto-start scheduled task. Does not touch the widget files.
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName 'ClaudeStatusBuddy' -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host 'ClaudeStatusBuddy task is not installed - nothing to remove.'
    return
}
Unregister-ScheduledTask -TaskName 'ClaudeStatusBuddy' -Confirm:$false
Write-Host 'Removed scheduled task: ClaudeStatusBuddy'
