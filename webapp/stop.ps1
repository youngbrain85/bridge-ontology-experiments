param()
$ErrorActionPreference = 'Stop'
$taskInfoPath = Join-Path $PSScriptRoot 'runtime\server.json'
if (-not (Test-Path -LiteralPath $taskInfoPath)) { Write-Host 'The local app is not running.'; exit 0 }
$taskInfo = Get-Content -LiteralPath $taskInfoPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($taskInfo.url -notmatch '^http://127\.0\.0\.1:\d{1,5}$') { throw 'Invalid local server address.' }
try { $taskHealth = Invoke-RestMethod -Uri ($taskInfo.url + '/api/health') -TimeoutSec 3 }
catch { Write-Host 'The local app is not running.'; exit 0 }
if ($taskHealth.app -ne 'bridge-ontology-multi-model-app' -or $taskHealth.package_root -ne $taskInfo.package) { throw 'Local app identity does not match.' }
$taskBootstrap = Invoke-RestMethod -Uri ($taskInfo.url + '/api/bootstrap') -TimeoutSec 3
$taskHeaders = @{ 'Origin' = $taskInfo.url; 'X-CSRF-Token' = $taskBootstrap.csrf_token }
try {
    Invoke-RestMethod -Method Post -Uri ($taskInfo.url + '/api/shutdown') -Headers $taskHeaders -ContentType 'application/json' -Body '{}' -TimeoutSec 5 | Out-Null
    Write-Host 'The local app has stopped. Experiment files are preserved.'
} catch { throw 'Finish the current job before closing the app. For API generation, use Stop in the browser and wait for the current response.' }
