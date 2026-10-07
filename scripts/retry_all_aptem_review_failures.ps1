param(
    [Parameter(Mandatory = $true)][int]$MainPid,
    [string]$MainReport = "debug\all-aptem-reviews-sync.json"
)

$ErrorActionPreference = "Stop"
$workspace = Split-Path -Parent $PSScriptRoot
$reportPath = Join-Path $workspace $MainReport
$retryReport = Join-Path $workspace "debug\all-aptem-reviews-retry.json"
$logPath = Join-Path $workspace "debug\all-aptem-reviews-retry.log"

while (Get-Process -Id $MainPid -ErrorAction SilentlyContinue) {
    Start-Sleep -Seconds 30
}

if (-not (Test-Path -LiteralPath $reportPath)) {
    "Main report missing: $reportPath" | Out-File -LiteralPath $logPath -Encoding utf8
    exit 1
}

$report = Get-Content -LiteralPath $reportPath -Raw | ConvertFrom-Json
$ids = @($report.errors | ForEach-Object { [string]$_.review_id } | Sort-Object -Unique)
if ($ids.Count -eq 0) {
    "No failed reviews to retry." | Out-File -LiteralPath $logPath -Encoding utf8
    exit 0
}

Push-Location (Join-Path $workspace "backend")
try {
    & python manage.py sync_all_aptem_reviews --apply --workers 5 `
        --review-ids ($ids -join ',') --report $retryReport *>&1 |
        Out-File -LiteralPath $logPath -Encoding utf8
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
