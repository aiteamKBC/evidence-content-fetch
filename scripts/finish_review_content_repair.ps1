param(
    [Parameter(Mandatory = $true)][int]$RepairProcessId,
    [string]$RepairReport = 'debug\review-content-v6-repair.json'
)

$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$reportPath = Join-Path $taskRoot $RepairReport
$backendPath = Join-Path $taskRoot 'backend'
$pythonPath = (Get-Command python.exe -ErrorAction Stop).Source

function Read-RepairState {
    for ($attempt = 0; $attempt -lt 5; $attempt++) {
        try {
            return Get-Content -LiteralPath $reportPath -Raw -ErrorAction Stop | ConvertFrom-Json
        } catch {
            if ($attempt -eq 4) { throw }
            Start-Sleep -Milliseconds 200
        }
    }
}

$state = Read-RepairState
if ([int]$state.pid -ne $RepairProcessId) {
    throw 'Checkpoint belongs to a different repair process; no follow-up was started.'
}
$repairProcess = Get-Process -Id $RepairProcessId -ErrorAction SilentlyContinue
if ($repairProcess) {
    if ($repairProcess.ProcessName -notmatch '^python') {
        throw 'Repair PID does not belong to Python; no follow-up was started.'
    }
    $originalStart = $repairProcess.StartTime.ToUniversalTime()
    $reportStart = [datetime]::Parse($state.started_at).ToUniversalTime()
    if ([math]::Abs(($originalStart - $reportStart).TotalSeconds) -ge 120) {
        throw 'Repair process start time differs from its checkpoint.'
    }
    Write-Output ('{0} Waiting for repair PID {1}.' -f [datetime]::UtcNow.ToString('o'), $RepairProcessId)
    while ($repairProcess) {
        Start-Sleep -Seconds 5
        $repairProcess = Get-Process -Id $RepairProcessId -ErrorAction SilentlyContinue
        if ($repairProcess -and $repairProcess.StartTime.ToUniversalTime() -ne $originalStart) {
            throw 'Repair PID was reused; stopped the follow-up safely.'
        }
    }
}

$state = Read-RepairState
if ([int]$state.pid -ne $RepairProcessId -or $state.run_status -notin @('finished', 'finished_with_gaps')) {
    throw 'Repair did not finish normally. No automatic restart or follow-up writes were performed.'
}

Set-Location -LiteralPath $backendPath
Write-Output ('{0} Repair finished; reconciling only proven preserved duplicates.' -f [datetime]::UtcNow.ToString('o'))
& $pythonPath manage.py reconcile_preserved_review_sections --apply --workers 5 --report (Join-Path $taskRoot 'debug\review-content-preserved-reconciliation.json')
if ($LASTEXITCODE -ne 0) { throw 'Preserved-section reconciliation failed; inspect its report.' }

# Missing/changed source timestamps cannot justify a local-only reclassification.
# Fetch those selected reviews afresh once, together with unexpected source
# failures. Known persistent Archived manifest failures and reconstructed 14980
# stay explicit; never loop retries or restart a repair the user stopped.
$reconciliation = Get-Content -LiteralPath (Join-Path $taskRoot 'debug\review-content-preserved-reconciliation.json') -Raw | ConvertFrom-Json
$refreshIds = @($reconciliation.results.PSObject.Properties | Where-Object {
    $_.Value.outcome -eq 'skipped' -and $_.Value.reason -eq 'source_changed_or_freshness_unproven'
} | ForEach-Object {
    if ($_.Name -notmatch '^\d+$') { throw 'Invalid review identity in reconciliation report.' }
    $_.Name
})
$sourceRetryIds = @($state.results.PSObject.Properties | Where-Object {
    ($_.Value.outcome -eq 'unverified' -and $_.Name -ne '14980' -and $_.Value.issues.Count -gt 0) -or
    ($_.Value.outcome -eq 'error' -and -not (
        $_.Value.source_status -eq 'Archived' -and $_.Value.reason -eq 'Aptem temporary HTTP 500'))
} | ForEach-Object {
    if ($_.Name -notmatch '^\d+$') { throw 'Invalid review identity in source retry plan.' }
    $_.Name
})
$refreshIds = @(@($refreshIds) + @($sourceRetryIds) | Sort-Object -Unique)
for ($offset = 0; $offset -lt $refreshIds.Count; $offset += 200) {
    $last = [math]::Min($offset + 199, $refreshIds.Count - 1)
    $selectedIds = $refreshIds[$offset..$last] -join ','
    $batchReport = Join-Path $taskRoot ('debug\review-content-freshness-retry-{0}.json' -f $offset)
    Write-Output ('{0} Fresh source verification for {1} timestamp-unproven or failed-source reviews.' -f [datetime]::UtcNow.ToString('o'), ($last-$offset+1))
    & $pythonPath manage.py repair_review_content --apply --workers 5 --scope content --review-ids $selectedIds --report $batchReport
    if ($LASTEXITCODE -ne 0) { throw 'Freshness verification failed; inspect its report.' }
}

Write-Output ('{0} Checking fresh source inventory and a read-only Neon snapshot.' -f [datetime]::UtcNow.ToString('o'))
& $pythonPath manage.py audit_review_content_capture --report (Join-Path $taskRoot 'debug\review-content-independent-audit.json')
if ($LASTEXITCODE -ne 0) { throw 'Independent final audit failed; completeness is not confirmed.' }

# Earlier captures used the PriorLearning endpoint for PLRContent. Use the
# independent audit's post-run inventory, including manifests discovered late
# in the main pass, to recapture every affected review with the corrected source.
$audit = Get-Content -LiteralPath (Join-Path $taskRoot 'debug\review-content-independent-audit.json') -Raw | ConvertFrom-Json
$plrIds = @($audit.issues.plr_source_contract_invalid | Where-Object { $null -ne $_ } | ForEach-Object {
    if ($_ -notmatch '^\d+$') { throw 'Invalid review identity in PLR contract audit.' }
    [string]$_
} | Sort-Object -Unique)
for ($offset = 0; $offset -lt $plrIds.Count; $offset += 200) {
    $last = [math]::Min($offset + 199, $plrIds.Count - 1)
    $selectedIds = $plrIds[$offset..$last] -join ','
    $batchReport = Join-Path $taskRoot ('debug\review-content-plr-repair-{0}.json' -f $offset)
    Write-Output ('{0} Re-fetching {1} reviews with the corrected PLR source contract.' -f [datetime]::UtcNow.ToString('o'), ($last-$offset+1))
    & $pythonPath manage.py repair_review_content --apply --workers 5 --scope content --review-ids $selectedIds --report $batchReport
    if ($LASTEXITCODE -ne 0) { throw 'PLR source repair failed; inspect its report.' }
}
if ($plrIds.Count -gt 0) {
    Write-Output ('{0} Repeating the independent audit after PLR source repair.' -f [datetime]::UtcNow.ToString('o'))
    & $pythonPath manage.py audit_review_content_capture --report (Join-Path $taskRoot 'debug\review-content-independent-audit.json')
    if ($LASTEXITCODE -ne 0) { throw 'Post-PLR independent audit failed; completeness is not confirmed.' }
}
Write-Output ('{0} Follow-up finished. Audit reports source errors and unresolved content explicitly.' -f [datetime]::UtcNow.ToString('o'))
