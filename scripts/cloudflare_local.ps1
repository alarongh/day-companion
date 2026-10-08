param(
    [ValidateSet('stop','start')][string]$Action,
    [Parameter(Mandatory=$true)][string]$Workspace
)
$ErrorActionPreference = 'Stop'
$botWorkspace = (Resolve-Path -LiteralPath $Workspace).Path
$botPython = Join-Path $botWorkspace '.venv\Scripts\python.exe'
$botPidFile = Join-Path $botWorkspace 'data\bot.pid'
$botDirectory = $botWorkspace
if ($Action -eq 'stop') {
    if (-not (Test-Path -LiteralPath $botPidFile)) { exit 0 }
    $botLauncher = [int](Get-Content -LiteralPath $botPidFile)
    $botRoot = Get-CimInstance Win32_Process -Filter "ProcessId = $botLauncher"
    if (-not $botRoot) { exit 0 }
    if ($botRoot.CommandLine -notlike ('*' + $botPython + '* -m companion.app')) { throw 'Saved process is not this bot' }
    $botChildren = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $botLauncher" | Where-Object { $_.CommandLine -eq $botRoot.CommandLine })
    foreach ($botChild in $botChildren) { Stop-Process -Id $botChild.ProcessId -Force }
    if (Get-Process -Id $botLauncher -ErrorAction SilentlyContinue) { Stop-Process -Id $botLauncher -Force }
} else {
    New-Item -ItemType Directory -Path (Join-Path $botWorkspace 'data') -Force | Out-Null
    $botOutput = Join-Path $botWorkspace 'data\local-stdout.log'
    $botErrors = Join-Path $botWorkspace 'data\local-stderr.log'
    $botProcess = Start-Process -FilePath $botPython -ArgumentList '-m','companion.app' -WorkingDirectory $botDirectory -WindowStyle Hidden -RedirectStandardOutput $botOutput -RedirectStandardError $botErrors -PassThru
    $botProcess.Id | Set-Content -LiteralPath $botPidFile
}
