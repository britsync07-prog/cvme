param(
    [string]$Time = "09:00",
    [string]$TaskName = "TailorMail Daily Outreach",
    [string]$Python = (Join-Path $PSScriptRoot "..\.venv312\Scripts\python.exe")
)

$runner = (Resolve-Path (Join-Path $PSScriptRoot "run-daily-outreach.ps1")).Path
$argument = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -Python `"$Python`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Description "TailorMail daily outreach batch" -Force | Out-Null
Write-Output "Scheduled '$TaskName' for $Time."
