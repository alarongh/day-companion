$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONIOENCODING='utf-8'
$pythonPath=Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12+ is required. Install Python, then retry.' }
    & $pythonPath -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
$configured = Select-String -LiteralPath '.env' -Pattern '^BOT_TOKEN=.+$' -Quiet
if (-not $configured -and -not $env:BOT_TOKEN) {
    Write-Host 'Open .env and fill BOT_TOKEN locally, then run this script again.'
    exit 1
}
& $pythonPath -m companion.app
exit $LASTEXITCODE
