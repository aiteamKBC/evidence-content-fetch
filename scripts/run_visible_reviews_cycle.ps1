param([ValidateRange(1,4)][int]$Workers = 4, [string]$RetryFrom = '')
$ErrorActionPreference = 'Stop'
$Workspace = Split-Path -Parent $PSScriptRoot
$Backend = (Resolve-Path (Join-Path $Workspace 'backend')).Path
$PythonPath = (Get-Command python -ErrorAction Stop).Source
$Audit = Join-Path $Workspace 'debug\visible-review-cycles'
New-Item -ItemType Directory -Path $Audit -Force | Out-Null
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$Arguments = @('-u', "`"$Backend\manage.py`"", 'refresh_visible_reviews', '--workers', $Workers)
if ($RetryFrom) {
    $Prior = (Resolve-Path -LiteralPath $RetryFrom).Path
    $Arguments += @('--retry-from', "`"$Prior`"")
}
$Process = Start-Process -FilePath $PythonPath -WorkingDirectory $Backend -WindowStyle Hidden `
    -ArgumentList $Arguments `
    -RedirectStandardOutput (Join-Path $Audit "$Stamp-coordinator.log") `
    -RedirectStandardError (Join-Path $Audit "$Stamp-coordinator-error.log") -Wait -PassThru
exit $Process.ExitCode
