<#
.SYNOPSIS
    Build the Codex Claude Dev Loop Windows desktop EXE.

.DESCRIPTION
    Builds a Windows onedir EXE bundle via PyInstaller.

    Output:
      dist\CodexClaudeDevLoop\CodexClaudeDevLoop.exe  (main executable)
      dist\CodexClaudeDevLoop\_internal\              (Python runtime + data)

    Requirements:
      py -3 -m pip install pyinstaller pywebview

.PARAMETER Clean
    Remove the build\ and dist\ folders before building.

.PARAMETER NoConfirm
    Pass --noconfirm to PyInstaller so the build does not prompt when
    overwriting an existing dist directory.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build-exe.ps1
    powershell -ExecutionPolicy Bypass -File packaging\build-exe.ps1 -Clean
#>

param(
    [switch]$Clean,
    [switch]$NoConfirm = $true
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = (Resolve-Path (Join-Path $ScriptDir "..")).Path
Set-Location $ProjectDir

Write-Host "=== Codex Claude Dev Loop - desktop EXE build ===" -ForegroundColor Cyan
Write-Host "Project: $ProjectDir"

# 1) Dependency checks -------------------------------------------------------
function Test-Command {
    param([string]$Name, [string]$DisplayName)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $cmd) {
        Write-Host "ERROR: $DisplayName not found on PATH ($Name)." -ForegroundColor Red
        Write-Host "  py:     install Python 3.10+ from https://www.python.org/" -ForegroundColor Yellow
        Write-Host "  pyinstaller / pywebview: py -3 -m pip install pyinstaller pywebview" -ForegroundColor Yellow
        exit 1
    }
    return $cmd.Source
}

$python = Test-Command "py" "Python launcher (py)"
Write-Host "Python launcher: $python"

# Sanity check: ensure required Python packages are importable.
$checkScript = @'
import importlib
missing = []
for name in ("PyInstaller", "webview"):
    try:
        importlib.import_module(name)
    except ImportError:
        missing.append(name)
if missing:
    raise SystemExit(",".join(missing))
raise SystemExit(0)
'@
$probe = & py -3 -c $checkScript
$probeCode = $LASTEXITCODE
if ($probeCode -ne 0) {
    Write-Host "ERROR: missing Python packages: $probe" -ForegroundColor Red
    Write-Host "  py -3 -m pip install pyinstaller pywebview" -ForegroundColor Yellow
    exit 1
}

# 2) Optional clean ----------------------------------------------------------
if ($Clean) {
    foreach ($dir in @("build", "dist")) {
        $target = Join-Path $ProjectDir $dir
        if (Test-Path $target) {
            Write-Host "Cleaning $target" -ForegroundColor Yellow
            Remove-Item -Recurse -Force $target
        }
    }
}

# 3) Build -------------------------------------------------------------------
$specPath = Join-Path $ScriptDir "CodexClaudeDevLoop.spec"
if (-not (Test-Path $specPath)) {
    Write-Host "ERROR: spec file missing: $specPath" -ForegroundColor Red
    exit 1
}

$args = @("-3", "-m", "PyInstaller")
if ($NoConfirm) { $args += "--noconfirm" }
$args += $specPath

Write-Host "Running: py $($args -join ' ')" -ForegroundColor Cyan
& py @args
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    Write-Host "PyInstaller failed with exit code $exitCode" -ForegroundColor Red
    exit $exitCode
}

# 4) Report ------------------------------------------------------------------
$exe = Join-Path $ProjectDir "dist\CodexClaudeDevLoop\CodexClaudeDevLoop.exe"
if (Test-Path $exe) {
    Write-Host ""
    Write-Host "Build succeeded:" -ForegroundColor Green
    Write-Host "  $exe" -ForegroundColor Green
} else {
    Write-Host "Build reported success but expected exe was not found: $exe" -ForegroundColor Red
    exit 1
}
