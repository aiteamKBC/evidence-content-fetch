param([ValidateRange(1,5)][int]$Workers=5)
$ErrorActionPreference='Stop'
$workspace=Split-Path -Parent $PSScriptRoot
$backend=(Resolve-Path (Join-Path $workspace 'backend')).Path
$debug=(Resolve-Path (Join-Path $workspace 'debug')).Path
$report=Join-Path $debug 'all-aptem-reviews-sync.json'
$lock=$report+'.lock'
if(Test-Path -LiteralPath $lock){throw 'Aptem review sync lock exists; inspect the running process first.'}
$python=(Get-Command python -ErrorAction Stop).Source
$stamp=Get-Date -Format 'yyyyMMdd-HHmmss'
$stdout=Join-Path $debug "all-aptem-reviews-sync-$stamp.stdout.log"
$stderr=Join-Path $debug "all-aptem-reviews-sync-$stamp.stderr.log"
$arguments="-u manage.py sync_all_aptem_reviews --apply --workers $Workers --report ..\debug\all-aptem-reviews-sync.json"
$process=Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $backend `
    -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
[pscustomobject]@{Pid=$process.Id;Workers=$Workers;Report=$report;Stdout=$stdout;Stderr=$stderr}
