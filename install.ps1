[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$venv = Join-Path $PSScriptRoot '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    & py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or newer is required.' }
    & py -3 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11+ and the Windows Python launcher first.' }
}
& $python -m pip install $PSScriptRoot
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $python -m rxyy_mcp setup
if ($LASTEXITCODE -ne 0) { throw 'Configuration setup failed.' }
Write-Output ('Ready. Start: & "' + (Join-Path $venv 'Scripts\rxyy-tools.exe') + '" start')
