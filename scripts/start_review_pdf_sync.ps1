param([ValidateSet('all','active')][string]$Scope='all',
      [ValidateRange(1,5)][int]$Workers=5,
      [ValidateRange(0,24)][double]$RunHours=8)
$ErrorActionPreference='Stop'
$workspace=Split-Path -Parent $PSScriptRoot
$backend=(Resolve-Path (Join-Path $workspace 'backend')).Path
$debug=(Resolve-Path (Join-Path $workspace 'debug')).Path
$stopFile=Join-Path $debug 'review-pdf-sync.stop'
if(Test-Path -LiteralPath $stopFile){throw 'Stop file exists; remove it deliberately before resuming.'}
$python=(Get-Command python -ErrorAction Stop).Source
$stamp=Get-Date -Format 'yyyyMMdd-HHmmss'
$stdout=Join-Path $debug "review-pdf-sync-$stamp.stdout.log"
$stderr=Join-Path $debug "review-pdf-sync-$stamp.stderr.log"
$hours=$RunHours.ToString([Globalization.CultureInfo]::InvariantCulture)
$arguments="-u manage.py sync_review_pdfs --apply --scope $Scope --workers $Workers --run-hours $hours"
$process=Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $backend -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
[pscustomobject]@{Pid=$process.Id;Scope=$Scope;Workers=$Workers;Stdout=$stdout;Stderr=$stderr}
