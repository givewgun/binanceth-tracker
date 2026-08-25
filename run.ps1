<#
.SYNOPSIS
    Convenience launcher for Windows: sets up a virtualenv on first run,
    then serves the app. Mirrors run.sh.

.EXAMPLE
    .\run.ps1                  # serve the dashboard
    .\run.ps1 sync --full      # re-fetch everything from scratch
#>
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

# Prefer the py launcher; fall back to whatever `python` resolves to.
function Get-Bootstrapper {
    if (Get-Command py -ErrorAction SilentlyContinue) { return @('py', @('-3')) }
    if (Get-Command python -ErrorAction SilentlyContinue) { return @('python', @()) }
    throw "Python 3 not found on PATH. Install it from https://python.org or the Microsoft Store."
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host 'Creating virtualenv...'
    $boot, $bootArgs = Get-Bootstrapper
    & $boot @bootArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Failed to create virtualenv (exit $LASTEXITCODE)." }
    & $venvPython -m pip install --quiet --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip (exit $LASTEXITCODE)." }
    & $venvPython -m pip install --quiet -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Failed to install requirements (exit $LASTEXITCODE)." }
}

if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.env'))) {
    Write-Host 'No .env found - copying .env.example. Add your API keys, then re-run.'
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot '.env.example') `
              -Destination (Join-Path $PSScriptRoot '.env')
    exit 1
}

# $args reaches the app verbatim, so `.\run.ps1 sync --full` behaves like ./run.sh.
& $venvPython -m app.main @args
exit $LASTEXITCODE
