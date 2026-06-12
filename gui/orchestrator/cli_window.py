from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .models import Task
from .path_safety import ensure_child_path


DEFAULT_SETTINGS = {
    "claudeCommand": ["claude"],
    "codexCommand": ["codex"],
}


class CliWindowError(RuntimeError):
    pass


def load_settings(settings_path: Path) -> dict[str, list[str]]:
    if not settings_path.exists():
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(DEFAULT_SETTINGS, indent=2), encoding="utf-8")
        return dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CliWindowError(f"Invalid settings JSON: {settings_path}") from exc
    settings: dict[str, list[str]] = {}
    for key, fallback in DEFAULT_SETTINGS.items():
        value = data.get(key, fallback) if isinstance(data, dict) else fallback
        if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
            settings[key] = list(fallback)
        else:
            settings[key] = value
    return settings


def generate_launcher_script(
    *,
    task: Task,
    task_dir: Path,
    kind: str,
    command: list[str],
    prompt_path: Path,
    output_path: Path | None = None,
) -> Path:
    ensure_child_path(task_dir, prompt_path)
    if output_path is not None:
        ensure_child_path(task_dir, output_path)
    script_path = task_dir / f"{kind}_window_round_{task.round}.ps1"
    ensure_child_path(task_dir, script_path)
    log_path = task_dir / f"{kind}_window_round_{task.round}.log"
    command_json = json.dumps(command, ensure_ascii=False)
    output_line = f"Write-Host 'Output file: {escape_ps(str(output_path))}'" if output_path else ""
    codex_home = task_dir.parent.parent / "codex-home"
    codex_setup = ""
    codex_output_args = ""
    codex_missing_output_check = ""
    if kind == "codex":
        codex_setup = f"""$CodexHome = '{escape_ps(str(codex_home))}'
New-Item -ItemType Directory -Force -Path $CodexHome | Out-Null
$env:CODEX_HOME = $CodexHome
Add-Content -LiteralPath $LogFile -Value "CODEX_HOME=$CodexHome" -Encoding UTF8"""
        if output_path is not None:
            codex_output_args = f"$CodexArgs += @('--output-last-message', '{escape_ps(str(output_path))}')"
            codex_missing_output_check = f"""  if (-not (Test-Path -LiteralPath '{escape_ps(str(output_path))}')) {{
    Add-Content -LiteralPath $LogFile -Value "WARNING: Codex completed without creating the expected output file." -Encoding UTF8
  }}"""
    script = f"""$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$TaskId = '{escape_ps(task.id)}'
$TaskDir = '{escape_ps(str(task_dir))}'
$PromptFile = '{escape_ps(str(prompt_path))}'
$LogFile = '{escape_ps(str(log_path))}'
$LauncherKind = '{escape_ps(kind)}'
$CommandJson = @'
{command_json}
'@
$Command = @(ConvertFrom-Json $CommandJson)
Set-Location -LiteralPath '{escape_ps(task.projectPath)}'
Set-Content -LiteralPath $LogFile -Value "Started {kind} window for task $TaskId" -Encoding UTF8
{codex_setup}
Write-Host ''
Write-Host 'Task:' $TaskId
Write-Host 'Working directory:' (Get-Location).Path
Write-Host 'Task directory:' $TaskDir
Write-Host 'Prompt file:' $PromptFile
{output_line}
Write-Host ''
Write-Host 'Open the prompt file above and provide its contents to the CLI if needed.'
Write-Host ''
$CommandName = [string]$Command[0]
if (-not (Get-Command $CommandName -ErrorAction SilentlyContinue)) {{
  Write-Host "ERROR: CLI command not found: $CommandName" -ForegroundColor Red
  Add-Content -LiteralPath $LogFile -Value "CLI command not found: $CommandName" -Encoding UTF8
  Read-Host 'Press Enter to close this window'
  exit 127
}}
try {{
  $PromptText = Get-Content -Raw -LiteralPath $PromptFile -Encoding UTF8
  if ($Command.Count -gt 1) {{
    $CliArgs = @($Command[1..($Command.Count - 1)])
    & $CommandName @CliArgs
  }} elseif ($LauncherKind -eq 'codex') {{
    $CodexArgs = @('exec', '--skip-git-repo-check', '--sandbox', 'read-only', '--ask-for-approval', 'never', '--add-dir', $TaskDir)
    {codex_output_args}
    $CodexArgs += '-'
    $PromptText | & $CommandName @CodexArgs 2>&1 | Tee-Object -FilePath $LogFile -Append
{codex_missing_output_check}
  }} else {{
    & $CommandName $PromptText
  }}
  $Code = $LASTEXITCODE
  Add-Content -LiteralPath $LogFile -Value ("CLI exit code: {{0}}" -f $Code) -Encoding UTF8
}} catch {{
  Write-Host $_.Exception.Message -ForegroundColor Red
  Add-Content -LiteralPath $LogFile -Value $_.Exception.Message -Encoding UTF8
}}
Write-Host ''
Write-Host 'When complete, return to the web page and click the matching completed button.'
Read-Host 'Press Enter to close this window'
"""
    # Windows PowerShell 5.1 reads UTF-8 scripts without a BOM as the local
    # ANSI code page, which corrupts non-ASCII paths such as Chinese project
    # names. Use a UTF-8 BOM for generated launcher scripts.
    script_path.write_text(script, encoding="utf-8-sig")
    return script_path


def launch_powershell_window(script_path: Path) -> subprocess.Popen[Any]:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell") or "powershell"
    argv = [
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
    ]
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    return subprocess.Popen(argv, shell=False, creationflags=creationflags)


def launch_cli_window(
    *,
    task: Task,
    task_dir: Path,
    kind: str,
    command: list[str],
    prompt_path: Path,
    output_path: Path | None = None,
) -> dict[str, Any]:
    script_path = generate_launcher_script(
        task=task,
        task_dir=task_dir,
        kind=kind,
        command=command,
        prompt_path=prompt_path,
        output_path=output_path,
    )
    available = shutil.which(command[0]) is not None
    process = launch_powershell_window(script_path)
    return {
        "script": str(script_path),
        "command": command,
        "pid": process.pid,
        "cliAvailable": available,
    }


def escape_ps(value: str) -> str:
    return value.replace("'", "''")
