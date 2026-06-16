@echo off
setlocal enableextensions enabledelayedexpansion

rem ---------------------------------------------------------------------------
rem Codex Claude Dev Loop - one-click GUI launcher.
rem
rem Usage:
rem   start.bat              Start GUI on the default port (8765).
rem   start.bat 8787         Start GUI on the specified port.
rem   start.bat -Port 8787   Same as above, mirror of the PowerShell flag.
rem
rem Notes:
rem   This batch forwards arguments to scripts\start-gui.ps1.
rem   A bare numeric first argument is rewritten to "-Port <n>" so callers
rem   can simply double-click the file or type "start.bat 8787".
rem ---------------------------------------------------------------------------

set "SCRIPT_DIR=%~dp0"
set "PS1=%SCRIPT_DIR%scripts\start-gui.ps1"

if not exist "%PS1%" (
  echo [start.bat] ERROR: scripts\start-gui.ps1 not found next to %~nx0.
  echo Expected path: %PS1%
  echo This file should sit at the repository root. Re-clone or restore the file.
  endlocal & exit /b 1
)

set "PS_EXE="
where /q powershell.exe && set "PS_EXE=powershell.exe"
if not defined PS_EXE (
  where /q pwsh.exe && set "PS_EXE=pwsh.exe"
)
if not defined PS_EXE (
  echo [start.bat] ERROR: Neither powershell.exe nor pwsh.exe was found on PATH.
  echo Install Windows PowerShell 5.1 ^(built into Windows 10+^) or PowerShell 7+.
  endlocal & exit /b 1
)

rem Compose forwarded args. If the first user arg is a pure number, prefix it
rem with -Port so "start.bat 8787" works as documented.
set "FORWARDED="
if "%~1"=="" goto :run
set "FIRST=%~1"
rem strip leading dash, then check digits-only.
rem IMPORTANT: do not prefix the FOR /F input with any sentinel char.
rem FOR /F treats every non-delimiter character as a token, so a leading '#'
rem would always be matched and force IS_NUM=0, breaking "start.bat 8787".
set "NUMERIC=%FIRST:-=%"
set "IS_NUM=1"
if "%NUMERIC%"=="" set "IS_NUM=0"
for /f "delims=0123456789" %%A in ("%NUMERIC%") do set "IS_NUM=0"
if "%IS_NUM%"=="1" (
  set "FORWARDED=-Port %FIRST%"
  shift
) else (
  set "FORWARDED=%FIRST%"
  shift
)
:argloop
if "%~1"=="" goto :run
set "FORWARDED=%FORWARDED% %~1"
shift
goto :argloop

:run
echo [start.bat] Launching GUI via %PS_EXE%...
"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %FORWARDED%
set "EXITCODE=%ERRORLEVEL%"

if "%EXITCODE%"=="0" (
  endlocal & exit /b 0
)

echo.
echo [start.bat] GUI exited with code %EXITCODE%.
echo Common causes:
echo   - Python 3 is not installed or 'py'/'python' is not on PATH.
echo   - gui\server.py is missing or unreadable.
echo   - Port 8765 ^(or the port you passed^) is already in use; pass another, e.g. start.bat 8787.
endlocal & exit /b %EXITCODE%
