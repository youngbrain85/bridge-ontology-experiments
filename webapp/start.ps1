param([string]$Package = '', [string]$Python = '')
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($Package)) { $Package = Split-Path -Parent $PSScriptRoot }
$taskPackagePath = (Resolve-Path -LiteralPath $Package).Path
if (-not (Test-Path -LiteralPath (Join-Path $taskPackagePath 'experiment.py'))) { throw 'Experiment package not found.' }
if ([string]::IsNullOrWhiteSpace($Python)) {
    $taskLocalPython = Join-Path $taskPackagePath '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $taskLocalPython) { $Python = $taskLocalPython }
    elseif ($env:VIRTUAL_ENV -and (Test-Path -LiteralPath (Join-Path $env:VIRTUAL_ENV 'Scripts\python.exe'))) {
        $Python = Join-Path $env:VIRTUAL_ENV 'Scripts\python.exe'
    }
    else { $Python = (Get-Command python -ErrorAction Stop).Source }
}
$taskRuntime = Join-Path $PSScriptRoot 'runtime'
New-Item -ItemType Directory -Path $taskRuntime -Force | Out-Null
$taskInfoPath = Join-Path $taskRuntime 'server.json'
function Get-LocalApp {
    if (-not (Test-Path -LiteralPath $taskInfoPath)) { return $null }
    try {
        $taskInfo = Get-Content -LiteralPath $taskInfoPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($taskInfo.url -notmatch '^http://127\.0\.0\.1:\d{1,5}$') { return $null }
        $taskHealth = Invoke-RestMethod -Uri ($taskInfo.url + '/api/health') -TimeoutSec 2
        if ($taskHealth.app -eq 'bridge-ontology-multi-model-app' -and $taskHealth.package_root -eq $taskPackagePath) { return $taskInfo }
    } catch { return $null }
    return $null
}
$taskExisting = Get-LocalApp
if ($null -ne $taskExisting) { Start-Process $taskExisting.url; exit 0 }
$taskServer = Join-Path $PSScriptRoot 'server.py'
$taskArguments = @('-B', '-X', 'utf8', ('"' + $taskServer + '"'), '--package', ('"' + $taskPackagePath + '"'))
$taskProcess = Start-Process -FilePath $Python -ArgumentList $taskArguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskRuntime 'server.stdout.txt') -RedirectStandardError (Join-Path $taskRuntime 'server.stderr.txt')
for ($taskAttempt = 0; $taskAttempt -lt 50; $taskAttempt++) {
    Start-Sleep -Milliseconds 200
    $taskExisting = Get-LocalApp
    if ($null -ne $taskExisting -and $taskExisting.pid -eq $taskProcess.Id) { Start-Process $taskExisting.url; exit 0 }
    if ($taskProcess.HasExited) { throw 'Local server did not start. Check webapp/runtime/server.stderr.txt.' }
}
throw 'The local server is still starting. Double-click Start again in a few seconds.'
