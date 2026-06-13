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
    project_path = str(task.projectPath)
    working_dir_setup = f"$WorkingDir = '{escape_ps(project_path)}'"
    process_helpers = ""
    codex_setup = ""
    codex_output_args = ""
    codex_missing_output_check = ""
    if kind == "codex":
        codex_home_prefix = f"codex-home-round-{task.round}-"
        working_dir_setup = "$WorkingDir = $TaskDir"
        project_key = toml_basic_string(project_path.lower())
        task_dir_key = toml_basic_string(str(task_dir).lower())
        process_helpers = r"""function ConvertTo-NativeArgument {
  param([AllowEmptyString()][string]$Argument)
  if ($null -eq $Argument) { return '""' }
  if ($Argument.Length -eq 0) { return '""' }
  if ($Argument -notmatch '[\s"]') { return $Argument }
  $Result = '"'
  $Backslashes = 0
  foreach ($Char in $Argument.ToCharArray()) {
    if ($Char -eq [char]0x5c) {
      $Backslashes += 1
      continue
    }
    if ($Char -eq [char]0x22) {
      $Result += ('\' * ($Backslashes * 2 + 1))
      $Result += '"'
      $Backslashes = 0
      continue
    }
    if ($Backslashes -gt 0) {
      $Result += ('\' * $Backslashes)
      $Backslashes = 0
    }
    $Result += $Char
  }
  if ($Backslashes -gt 0) {
    $Result += ('\' * ($Backslashes * 2))
  }
  $Result += '"'
  return $Result
}

function Join-NativeArguments {
  param([string[]]$Arguments)
  return (($Arguments | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ')
}
"""
        codex_setup = f"""$CodexHome = Join-Path $TaskDir ('{escape_ps(codex_home_prefix)}' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $CodexHome | Out-Null
$DefaultCodexHome = Join-Path $env:USERPROFILE '.codex'
$TemporaryCodexFiles = @()
foreach ($SeedFile in @('auth.json')) {{
  $SourceFile = Join-Path $DefaultCodexHome $SeedFile
  if (Test-Path -LiteralPath $SourceFile) {{
    $DestinationFile = Join-Path $CodexHome $SeedFile
    Copy-Item -LiteralPath $SourceFile -Destination $DestinationFile -Force
    $TemporaryCodexFiles += $DestinationFile
  }}
}}
$MinimalConfigLines = @()
$DefaultConfigFile = Join-Path $DefaultCodexHome 'config.toml'
if (Test-Path -LiteralPath $DefaultConfigFile) {{
  $DefaultConfigText = Get-Content -Raw -LiteralPath $DefaultConfigFile -Encoding UTF8
  foreach ($ConfigKey in @('model', 'model_reasoning_effort')) {{
    $Match = [regex]::Match($DefaultConfigText, "(?m)^\\s*$ConfigKey\\s*=\\s*.+$")
    if ($Match.Success) {{
      $MinimalConfigLines += $Match.Value.Trim()
    }}
  }}
}}
$MinimalConfigLines += 'sandbox_mode = "workspace-write"'
$MinimalConfigLines += ''
$MinimalConfigLines += '[projects.{project_key}]'
$MinimalConfigLines += 'trust_level = "trusted"'
$MinimalConfigLines += ''
$MinimalConfigLines += '[projects.{task_dir_key}]'
$MinimalConfigLines += 'trust_level = "trusted"'
$MinimalConfigFile = Join-Path $CodexHome 'config.toml'
Set-Content -LiteralPath $MinimalConfigFile -Value $MinimalConfigLines -Encoding UTF8
$TemporaryCodexFiles += $MinimalConfigFile
$env:CODEX_HOME = $CodexHome
Add-Content -LiteralPath $LogFile -Value "CODEX_HOME=$CodexHome" -Encoding UTF8
Add-Content -LiteralPath $LogFile -Value "Codex minimal config: $MinimalConfigFile" -Encoding UTF8
try {{
  $CodexCommandInfo = Get-Command $CommandName -ErrorAction Stop
  Add-Content -LiteralPath $LogFile -Value "Codex command: $($CodexCommandInfo.Source)" -Encoding UTF8
}} catch {{
  Add-Content -LiteralPath $LogFile -Value ("Codex command lookup failed: {{0}}" -f $_.Exception.Message) -Encoding UTF8
}}"""
        if output_path is not None:
            codex_output_args = f"$CodexArgs += @('--output-last-message', '{escape_ps(str(output_path))}')"
            codex_missing_output_check = f"""  if (-not (Test-Path -LiteralPath '{escape_ps(str(output_path))}')) {{
    Add-Content -LiteralPath $LogFile -Value "WARNING: Codex completed without creating the expected output file." -Encoding UTF8
    if ($Code -eq 0) {{ $Code = 1 }}
  }}"""
    script = f"""$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$TaskId = '{escape_ps(task.id)}'
$TaskDir = '{escape_ps(str(task_dir))}'
$PromptFile = '{escape_ps(str(prompt_path))}'
$LogFile = '{escape_ps(str(log_path))}'
$LauncherKind = '{escape_ps(kind)}'
$TemporaryCodexFiles = @()
$CommandJson = @'
{command_json}
'@
$Command = @(ConvertFrom-Json $CommandJson)
{working_dir_setup}
Set-Location -LiteralPath $WorkingDir
Set-Content -LiteralPath $LogFile -Value "Started {kind} window for task $TaskId" -Encoding UTF8
$CommandName = [string]$Command[0]
{process_helpers}
{codex_setup}
if ($LauncherKind -eq 'claude') {{
  foreach ($ProxyVariable in @('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')) {{
    $ProxyValue = [Environment]::GetEnvironmentVariable($ProxyVariable, 'Process')
    if ($ProxyValue -match '^(?i)socks[45]h?://') {{
      [Environment]::SetEnvironmentVariable($ProxyVariable, $null, 'Process')
      Add-Content -LiteralPath $LogFile -Value "Removed unsupported SOCKS proxy env for Claude: $ProxyVariable" -Encoding UTF8
    }}
  }}
}}
Write-Host ''
Write-Host 'Task:' $TaskId
Write-Host 'Working directory:' (Get-Location).Path
Write-Host 'Task directory:' $TaskDir
Write-Host 'Prompt file:' $PromptFile
{output_line}
Write-Host ''
Write-Host 'Open the prompt file above and provide its contents to the CLI if needed.'
Write-Host ''
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
    $CodexArgs = @('exec', '--ephemeral', '--skip-git-repo-check', '--sandbox', 'workspace-write', '-C', $TaskDir)
    {codex_output_args}
    $CodexArgs += @('--color', 'never')
    $CodexArgs += '-'
    Add-Content -LiteralPath $LogFile -Value ("Codex args: {{0}}" -f ($CodexArgs -join ' ')) -Encoding UTF8
    $CodexCommandInfo = Get-Command $CommandName -ErrorAction Stop
    $CodexExecutable = $CodexCommandInfo.Source
    if (-not $CodexExecutable) {{ $CodexExecutable = $CodexCommandInfo.Path }}
    $CodexProcessInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $CodexProcessInfo.FileName = $CodexExecutable
    $CodexProcessInfo.Arguments = Join-NativeArguments $CodexArgs
    $CodexProcessInfo.WorkingDirectory = $WorkingDir
    $CodexProcessInfo.UseShellExecute = $false
    $CodexProcessInfo.RedirectStandardInput = $true
    $CodexProcessInfo.RedirectStandardOutput = $true
    $CodexProcessInfo.RedirectStandardError = $true
    $CodexProcessInfo.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $CodexProcessInfo.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $CodexProcessInfo.CreateNoWindow = $true
    $CodexProcess = [System.Diagnostics.Process]::new()
    $CodexProcess.StartInfo = $CodexProcessInfo
    Write-Host 'Codex is running. Captured output will appear when it finishes.'
    [void]$CodexProcess.Start()
    $StdoutTask = $CodexProcess.StandardOutput.ReadToEndAsync()
    $StderrTask = $CodexProcess.StandardError.ReadToEndAsync()
    $PromptBytes = [System.Text.Encoding]::UTF8.GetBytes($PromptText)
    $CodexProcess.StandardInput.BaseStream.Write($PromptBytes, 0, $PromptBytes.Length)
    $CodexProcess.StandardInput.Close()
    $CodexProcess.WaitForExit()
    $Stdout = $StdoutTask.Result
    $Stderr = $StderrTask.Result
    $Code = $CodexProcess.ExitCode
    if ($Stdout) {{
      Add-Content -LiteralPath $LogFile -Value $Stdout -Encoding UTF8
      Write-Host $Stdout
    }}
    if ($Stderr) {{
      Add-Content -LiteralPath $LogFile -Value "Codex stderr:" -Encoding UTF8
      Add-Content -LiteralPath $LogFile -Value $Stderr -Encoding UTF8
      if ($Code -ne 0) {{
        Write-Host $Stderr -ForegroundColor Yellow
      }} else {{
        Write-Host 'Codex emitted non-fatal stderr warnings; they were saved to the log.' -ForegroundColor DarkYellow
      }}
    }}
{codex_missing_output_check}
  }} else {{
    & $CommandName $PromptText
    $Code = $LASTEXITCODE
  }}
  Add-Content -LiteralPath $LogFile -Value ("CLI exit code: {{0}}" -f $Code) -Encoding UTF8
}} catch {{
  Write-Host $_.Exception.Message -ForegroundColor Red
  Add-Content -LiteralPath $LogFile -Value $_.Exception.Message -Encoding UTF8
}}
if ($TemporaryCodexFiles.Count -gt 0) {{
  foreach ($TemporaryFile in $TemporaryCodexFiles) {{
    if (Test-Path -LiteralPath $TemporaryFile) {{
      Remove-Item -LiteralPath $TemporaryFile -Force
    }}
  }}
  Add-Content -LiteralPath $LogFile -Value "Removed temporary Codex auth/config files from task CODEX_HOME." -Encoding UTF8
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
        "-NoExit",
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


def toml_basic_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
