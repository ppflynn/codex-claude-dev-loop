<#
.SYNOPSIS
    Start the local web GUI for the Codex Claude Dev Loop orchestrator.

.DESCRIPTION
    Launches gui/server.py on http://127.0.0.1:8765 by default.
    Detects Python (prefers `py -3`, falls back to `python`) and reports
    clear errors when Python is missing, gui/server.py is missing, or the
    port is already in use.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/start-gui.ps1
    powershell -ExecutionPolicy Bypass -File scripts/start-gui.ps1 -Port 8787
    powershell -ExecutionPolicy Bypass -File scripts/start-gui.ps1 -HostName 0.0.0.0 -Port 9000
#>

param(
    [string]$HostName = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = (Resolve-Path (Join-Path $ScriptDir "..")).Path
$ServerPath = Join-Path $ProjectDir "gui\server.py"

function Write-InfoLine($message, $color = "Cyan") {
    Write-Host $message -ForegroundColor $color
}

if (-not (Test-Path $ServerPath)) {
    Write-Host ""
    Write-Host "ERROR: gui\server.py was not found." -ForegroundColor Red
    Write-Host "  Looked at: $ServerPath" -ForegroundColor DarkGray
    Write-Host "  This usually means the repository layout changed or the file was removed." -ForegroundColor Yellow
    Write-Host "  Restore the file from git, or re-clone the repository." -ForegroundColor Yellow
    exit 1
}

$pythonCommand = Get-Command py -ErrorAction SilentlyContinue
if ($pythonCommand) {
    $pythonExe = "py"
    $pythonArgs = @("-3", $ServerPath, "--host", $HostName, "--port", "$Port")
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        Write-Host ""
        Write-Host "ERROR: Python was not found on PATH." -ForegroundColor Red
        Write-Host "  Install Python 3.9+ from https://www.python.org/downloads/ and ensure 'py' or 'python' is on PATH." -ForegroundColor Yellow
        Write-Host "  On Windows, the official installer adds py.exe to PATH by default." -ForegroundColor Yellow
        exit 1
    }
    $pythonExe = "python"
    $pythonArgs = @($ServerPath, "--host", $HostName, "--port", "$Port")
}

# Pre-check that the requested port is free. Python will fail otherwise, but
# surfacing the issue before launching gives the user a clear next step.
try {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
    $listener.Start()
    $listener.Stop()
} catch {
    Write-Host ""
    Write-Host "ERROR: Port $Port is already in use or unavailable." -ForegroundColor Red
    Write-Host "  Close the process using that port, or start the GUI on another port:" -ForegroundColor Yellow
    Write-Host "    powershell -ExecutionPolicy Bypass -File scripts\start-gui.ps1 -Port 8787" -ForegroundColor Yellow
    Write-Host "    start.bat 8787" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-InfoLine "Codex Claude Dev Loop - GUI launcher" "Cyan"
Write-InfoLine "  Project: $ProjectDir" "DarkGray"
Write-InfoLine "  Server : $ServerPath" "DarkGray"
Write-InfoLine "  Python : $pythonExe" "DarkGray"
Write-Host ""
Write-Host "GUI running at " -NoNewline
Write-Host "http://${HostName}:$Port/" -NoNewline -ForegroundColor Green
Write-Host ""
Write-Host "Press Ctrl+C to stop." -ForegroundColor Yellow
Write-Host ""

Set-Location $ProjectDir
try {
    & $pythonExe @pythonArgs
} catch {
    Write-Host ""
    Write-Host "ERROR: GUI process exited unexpectedly: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
