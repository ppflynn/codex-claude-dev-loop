<#
.SYNOPSIS
    Start the local web GUI for the AI coding collaboration orchestrator.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/start-gui.ps1
    powershell -ExecutionPolicy Bypass -File scripts/start-gui.ps1 -Port 8787
#>

param(
    [string]$HostName = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = (Resolve-Path (Join-Path $ScriptDir "..")).Path
$ServerPath = Join-Path $ProjectDir "gui/server.py"

if (-not (Test-Path $ServerPath)) {
    Write-Host "ERROR: gui/server.py not found." -ForegroundColor Red
    exit 1
}

$pythonCommand = Get-Command py -ErrorAction SilentlyContinue
if ($pythonCommand) {
    $pythonExe = "py"
    $pythonArgs = @("-3", $ServerPath, "--host", $HostName, "--port", "$Port")
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        Write-Host "ERROR: Python was not found. Install Python 3 or ensure 'py'/'python' is on PATH." -ForegroundColor Red
        exit 1
    }
    $pythonExe = "python"
    $pythonArgs = @($ServerPath, "--host", $HostName, "--port", "$Port")
}

Write-Host "Starting Agent GUI..." -ForegroundColor Cyan
Write-Host "URL: http://${HostName}:$Port" -ForegroundColor Green
Write-Host "Press Ctrl+C to stop." -ForegroundColor Yellow

Set-Location $ProjectDir
& $pythonExe @pythonArgs
