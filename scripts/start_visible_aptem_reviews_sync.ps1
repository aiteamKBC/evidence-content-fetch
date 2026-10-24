param(
    [ValidateRange(1,5)][int]$Workers = 2,
    [string]$Report = '..\debug\visible-aptem-reviews.json'
)

$ErrorActionPreference = 'Stop'
$backend = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\backend')).Path
$debugPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\debug')).Path
$python = (Get-Command python -ErrorAction Stop).Source
$env:APTEM_BROWSER_HEADLESS = 'false'

for ($index = 0; $index -lt $Workers; $index++) {
    $stdout = Join-Path $debugPath "visible-aptem-worker-$index.stdout.log"
    $stderr = Join-Path $debugPath "visible-aptem-worker-$index.stderr.log"
    $arguments = @(
        'manage.py', 'sync_visible_aptem_reviews', '--apply',
        '--worker-count', "$Workers", '--worker-index', "$index",
        '--report', $Report, '--resume'
    )
    $worker = Start-Process -FilePath $python -WorkingDirectory $backend `
        -ArgumentList $arguments -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    Write-Output "Started visible review worker $index/$Workers PID=$($worker.Id) report=$Report"
    if ($index -lt $Workers - 1) { Start-Sleep -Seconds 10 }
}
