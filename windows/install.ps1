# Installs promptloom for the current user on Windows: the CLI, a scheduled
# task that runs the daemon at sign-in, and a Start menu shortcut for the app.
# Nothing here needs an administrator. Safe to run again.
#
#   powershell -ExecutionPolicy Bypass -File windows\install.ps1

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

Write-Host "== CLI"
if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv tool install --force --with anthropic --with pywebview --editable $here
} elseif (Get-Command pipx -ErrorAction SilentlyContinue) {
    pipx install --force --editable $here
    pipx inject promptloom anthropic
    pipx inject promptloom pywebview
} else {
    Write-Error "Install uv (https://docs.astral.sh/uv/) or pipx first."
}
$promptloom = (Get-Command promptloom -ErrorAction SilentlyContinue).Source
if (-not $promptloom) {
    $promptloom = Join-Path $env:USERPROFILE ".local\bin\promptloom.exe"
}
& $promptloom init

Write-Host "== daemon (a scheduled task at sign-in)"
# pythonw keeps the daemon off the taskbar; the uv shim is a console program.
$pythonw = Join-Path (Split-Path -Parent $promptloom) "..\tools\promptloom\Scripts\pythonw.exe"
$action = if (Test-Path $pythonw) {
    New-ScheduledTaskAction -Execute (Resolve-Path $pythonw) -Argument "-m promptloom watch"
} else {
    New-ScheduledTaskAction -Execute $promptloom -Argument "watch"
}
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -Hidden
Register-ScheduledTask -TaskName "Promptloom" -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "Promptloom"

Write-Host "== Start menu shortcut"
$programs = [Environment]::GetFolderPath("Programs")
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path $programs "Promptloom.lnk"))
$shortcut.TargetPath = $promptloom
$shortcut.Arguments = "app"
$shortcut.WorkingDirectory = $env:USERPROFILE
$shortcut.Description = "Every prompt you have typed into your AI coding tools"
$icon = Join-Path $here "windows\promptloom.ico"
if (Test-Path $icon) { $shortcut.IconLocation = $icon }
$shortcut.Save()

Write-Host ""
Write-Host "Done. 'promptloom app' opens the app in a window; pin it to the taskbar from there."
Write-Host "The daemon runs as the scheduled task 'Promptloom' (Task Scheduler) at every sign-in."
