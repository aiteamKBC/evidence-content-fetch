param(
    [int]$IntervalMinutes = 120
)
# Registers (or re-registers) the scheduled reviews-sync cycle. Run once.
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\scripts\register_cycle.ps1                  # every 2 hours
#   powershell -ExecutionPolicy Bypass -File .\scripts\register_cycle.ps1 -IntervalMinutes 60
#   Disable-ScheduledTask    -TaskName "KBC Reviews Cycle"   # pause
#   Unregister-ScheduledTask -TaskName "KBC Reviews Cycle"   # remove
$ErrorActionPreference = "Stop"

$Workspace  = Split-Path -Parent $PSScriptRoot
$Backend    = (Resolve-Path (Join-Path $Workspace 'backend')).Path
$PythonPath = (Get-Command python -ErrorAction Stop).Source
$TaskName   = "KBC Reviews Cycle"

$Action = New-ScheduledTaskAction -Execute $PythonPath `
    -Argument "`"$Backend\manage.py`" run_cycle" `
    -WorkingDirectory $Backend

$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes($IntervalMinutes) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

# IgnoreNew: if the previous cycle is still running, do not start a second one.
# ExecutionTimeLimit: kill a cycle that hangs past 6 hours.
# StartWhenAvailable: if the machine was off/asleep at the scheduled time, run on wake.
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Description "Reviews sync every $IntervalMinutes minutes" -Force | Out-Null

Write-Output "Registered '$TaskName': runs every $IntervalMinutes minutes (python $Backend\manage.py run_cycle)."
