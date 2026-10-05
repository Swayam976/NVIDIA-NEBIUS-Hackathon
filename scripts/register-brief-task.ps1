# Registers a daily Windows Task Scheduler entry (current user, no admin) that
# runs scripts/run-daily-brief.sh in WSL to launch the Nebius brief job.
# Trigger log: .brief-trigger.log in the repo root. Remove with:
#   Unregister-ScheduledTask -TaskName hw-copilot-daily-brief -Confirm:$false
param(
    [string]$Time = "08:00",
    [string]$Distro = "Ubuntu"
)
$ErrorActionPreference = "Stop"

$repo = (Resolve-Path "$PSScriptRoot\..").Path
$wslRepo = (wsl.exe -d $Distro -- wslpath -a "$repo").Trim()
if (-not $wslRepo) { throw "Could not map $repo into WSL distro '$Distro'." }

$action = New-ScheduledTaskAction -Execute "wsl.exe" `
    -Argument "-d $Distro -- env BRIEF_LOG=`"$wslRepo/.brief-trigger.log`" bash `"$wslRepo/scripts/run-daily-brief.sh`""
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
# Missed runs (PC asleep/off at $Time) start as soon as the machine is available.
# Laptop: the defaults would skip the run on battery or kill it when unplugged.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName "hw-copilot-daily-brief" -Action $action -Trigger $trigger -Settings $settings `
    -Description "hw-copilot: launch the daily brief as a Nebius Serverless Job" -Force | Out-Null
Write-Host "Registered 'hw-copilot-daily-brief' daily at $Time (distro: $Distro)."
