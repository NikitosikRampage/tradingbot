Unregister-ScheduledTask -TaskName "OKXBot" -Confirm:$false -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "OKXBot-Watchdog" -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Removed."
