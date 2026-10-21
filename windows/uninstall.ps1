# Removes the scheduled task, the Start menu shortcut and the CLI. The config
# and the captured state (%LOCALAPPDATA%\promptloom) are left in place.

$ErrorActionPreference = "SilentlyContinue"
Stop-ScheduledTask -TaskName "Promptloom"
Unregister-ScheduledTask -TaskName "Promptloom" -Confirm:$false
Remove-Item (Join-Path ([Environment]::GetFolderPath("Programs")) "Promptloom.lnk")
if (Get-Command uv) { uv tool uninstall promptloom } else { pipx uninstall promptloom }
Write-Host "Removed. Left in place: your config and captured state."
