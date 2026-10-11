# Registers two Task Scheduler tasks for the current user:
#   OKXBot           - starts at logon, restarts on failure (run_forever.py also restarts main.py)
#   OKXBot-Watchdog  - every 5 min, Telegram alert if the bot is silent > 5 min
# Run:  powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pyw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) { throw "Run setup.bat first ($pyw not found)" }

$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$root\run_forever.py`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "OKXBot" -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "OKX monitoring/trading bot" -Force | Out-Null

$wAction = New-ScheduledTaskAction -Execute $pyw -Argument "`"$root\scripts\watchdog.py`"" -WorkingDirectory $root
$wTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
$wSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "OKXBot-Watchdog" -Action $wAction -Trigger $wTrigger -Settings $wSettings `
    -Principal $principal -Description "Alerts to Telegram if OKXBot is silent" -Force | Out-Null

Write-Host "Tasks OKXBot and OKXBot-Watchdog registered. Start now: Start-ScheduledTask -TaskName OKXBot"
