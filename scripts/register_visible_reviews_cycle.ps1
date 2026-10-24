param(
    [ValidateRange(1,4)][int]$Workers = 4,
    [ValidateRange(1,24)][int]$IntervalHours = 6,
    [switch]$StartNow
)
$ErrorActionPreference = 'Stop'
$Workspace = Split-Path -Parent $PSScriptRoot
$Backend = (Resolve-Path (Join-Path $Workspace 'backend')).Path
(Get-Command python -ErrorAction Stop) | Out-Null
$TaskName = 'KBC Visible Reviews 6h'
$Audit = Join-Path $Workspace 'debug\visible-review-cycles'
New-Item -ItemType Directory -Path $Audit -Force | Out-Null
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Export-ScheduledTask -TaskName $TaskName | Set-Content -LiteralPath (Join-Path $Audit ('task-before-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.xml'))
}
$Runner = Join-Path $PSScriptRoot 'run_visible_reviews_cycle.ps1'
$PowerShellPath = (Get-Command powershell.exe -ErrorAction Stop).Source
$Action = New-ScheduledTaskAction -Execute $PowerShellPath `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Runner`" -Workers $Workers" `
    -WorkingDirectory $Backend
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours($IntervalHours) `
    -RepetitionInterval (New-TimeSpan -Hours $IntervalHours)
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$Principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Principal $Principal -Description "Visible Aptem reviews and signatures every $IntervalHours hours; additive, $Workers local workers, no pruning." -Force | Out-Null
Export-ScheduledTask -TaskName $TaskName | Set-Content -LiteralPath (Join-Path $Audit 'task.xml')
if ($StartNow) { Start-ScheduledTask -TaskName $TaskName }
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
Get-ScheduledTaskInfo -TaskName $TaskName | Select-Object LastRunTime,NextRunTime,LastTaskResult
