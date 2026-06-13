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
    if kind in {"claude", "codex"}:
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

function Write-NativeChunk {
  param(
    [AllowEmptyString()][string]$Text,
    [string]$LogFile,
    [bool]$IsError
  )
  if ($Text.Length -eq 0) { return }
  [System.IO.File]::AppendAllText($LogFile, $Text, [System.Text.Encoding]::UTF8)
  if ($IsError) {
    Write-Host $Text -NoNewline -ForegroundColor Yellow
  } else {
    Write-Host $Text -NoNewline
  }
}

function Receive-NativeStreamChunk {
  param(
    [System.Threading.Tasks.Task[int]]$ReadTask,
    [byte[]]$Buffer,
    [System.Text.Decoder]$Decoder,
    [char[]]$Chars,
    [string]$LogFile,
    [bool]$IsError,
    [ref]$Done,
    [ref]$NextTask,
    [System.IO.Stream]$Stream
  )
  if ($Done.Value -or -not $ReadTask.IsCompleted) { return }
  $Count = $ReadTask.Result
  if ($Count -le 0) {
    $CharCount = $Decoder.GetChars($Buffer, 0, 0, $Chars, 0, $true)
    if ($CharCount -gt 0) {
      $Text = -join $Chars[0..($CharCount - 1)]
      Write-NativeChunk -Text $Text -LogFile $LogFile -IsError $IsError
    }
    $Done.Value = $true
    return
  }
  $CharCount = $Decoder.GetChars($Buffer, 0, $Count, $Chars, 0, $false)
  if ($CharCount -gt 0) {
    $Text = -join $Chars[0..($CharCount - 1)]
    Write-NativeChunk -Text $Text -LogFile $LogFile -IsError $IsError
  }
  $NextTask.Value = $Stream.ReadAsync($Buffer, 0, $Buffer.Length)
}

function Invoke-StreamingNativeProcess {
  param(
    [string]$CommandName,
    [string[]]$Arguments,
    [string]$WorkingDirectory,
    [string]$PromptText,
    [string]$LogFile,
    [string]$Label
  )
  $CommandInfo = Get-Command $CommandName -ErrorAction Stop
  $Executable = $CommandInfo.Source
  if (-not $Executable) { $Executable = $CommandInfo.Path }
  $ProcessInfo = [System.Diagnostics.ProcessStartInfo]::new()
  $ProcessInfo.FileName = $Executable
  $ProcessInfo.Arguments = Join-NativeArguments $Arguments
  $ProcessInfo.WorkingDirectory = $WorkingDirectory
  $ProcessInfo.UseShellExecute = $false
  $ProcessInfo.RedirectStandardInput = $true
  $ProcessInfo.RedirectStandardOutput = $true
  $ProcessInfo.RedirectStandardError = $true
  $ProcessInfo.StandardOutputEncoding = [System.Text.Encoding]::UTF8
  $ProcessInfo.StandardErrorEncoding = [System.Text.Encoding]::UTF8
  $ProcessInfo.CreateNoWindow = $true
  $Process = [System.Diagnostics.Process]::new()
  $Process.StartInfo = $ProcessInfo
  Add-Content -LiteralPath $LogFile -Value ("{0} command: {1}" -f $Label, $Executable) -Encoding UTF8
  [void]$Process.Start()

  $StdoutBuffer = [byte[]]::new(4096)
  $StderrBuffer = [byte[]]::new(4096)
  $StdoutChars = [char[]]::new(4096)
  $StderrChars = [char[]]::new(4096)
  $StdoutDecoder = [System.Text.Encoding]::UTF8.GetDecoder()
  $StderrDecoder = [System.Text.Encoding]::UTF8.GetDecoder()
  $StdoutTask = $Process.StandardOutput.BaseStream.ReadAsync($StdoutBuffer, 0, $StdoutBuffer.Length)
  $StderrTask = $Process.StandardError.BaseStream.ReadAsync($StderrBuffer, 0, $StderrBuffer.Length)
  $StdoutDone = $false
  $StderrDone = $false

  $PromptBytes = [System.Text.Encoding]::UTF8.GetBytes($PromptText)
  $Process.StandardInput.BaseStream.Write($PromptBytes, 0, $PromptBytes.Length)
  $Process.StandardInput.Close()

  while (-not ($StdoutDone -and $StderrDone)) {
    Receive-NativeStreamChunk -ReadTask $StdoutTask -Buffer $StdoutBuffer -Decoder $StdoutDecoder -Chars $StdoutChars -LogFile $LogFile -IsError $false -Done ([ref]$StdoutDone) -NextTask ([ref]$StdoutTask) -Stream $Process.StandardOutput.BaseStream
    Receive-NativeStreamChunk -ReadTask $StderrTask -Buffer $StderrBuffer -Decoder $StderrDecoder -Chars $StderrChars -LogFile $LogFile -IsError $true -Done ([ref]$StderrDone) -NextTask ([ref]$StderrTask) -Stream $Process.StandardError.BaseStream
    if (-not ($StdoutDone -and $StderrDone)) {
      Start-Sleep -Milliseconds 50
    }
  }
  $Process.WaitForExit()
  return $Process.ExitCode
}
"""
    if kind == "codex":
        codex_home_prefix = f"codex-home-round-{task.round}-"
        working_dir_setup = "$WorkingDir = $TaskDir"
        project_key = toml_basic_string(project_path.lower())
        task_dir_key = toml_basic_string(str(task_dir).lower())
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
  if ($LauncherKind -eq 'claude') {{
    $ClaudeArgs = @()
    if ($Command.Count -gt 1) {{
      $ClaudeArgs += @($Command[1..($Command.Count - 1)])
    }}
    $HasPrintMode = $false
    $HasPermissionMode = $false
    foreach ($ClaudeArg in $ClaudeArgs) {{
      if ($ClaudeArg -eq '-p' -or $ClaudeArg -eq '--print') {{
        $HasPrintMode = $true
      }}
      if ($ClaudeArg -eq '--permission-mode' -or $ClaudeArg -like '--permission-mode=*') {{
        $HasPermissionMode = $true
      }}
    }}
    if (-not $HasPrintMode) {{
      $ClaudeArgs += '-p'
    }}
    if (-not $HasPermissionMode) {{
      $ClaudeArgs += @('--permission-mode', 'bypassPermissions')
    }}
    Add-Content -LiteralPath $LogFile -Value ("Claude args: {{0}}" -f ($ClaudeArgs -join ' ')) -Encoding UTF8
    Write-Host 'Claude is running. Output will appear below.'
    $Code = Invoke-StreamingNativeProcess -CommandName $CommandName -Arguments $ClaudeArgs -WorkingDirectory $WorkingDir -PromptText $PromptText -LogFile $LogFile -Label 'Claude'
  }} elseif ($LauncherKind -eq 'codex') {{
    $CodexArgs = @('exec', '--ephemeral', '--skip-git-repo-check', '--sandbox', 'workspace-write', '-C', $TaskDir)
    {codex_output_args}
    $CodexArgs += @('--color', 'never')
    $CodexArgs += '-'
    Add-Content -LiteralPath $LogFile -Value ("Codex args: {{0}}" -f ($CodexArgs -join ' ')) -Encoding UTF8
    Write-Host 'Codex is running. Output will appear below.'
    $Code = Invoke-StreamingNativeProcess -CommandName $CommandName -Arguments $CodexArgs -WorkingDirectory $WorkingDir -PromptText $PromptText -LogFile $LogFile -Label 'Codex'
{codex_missing_output_check}
  }} else {{
    $CliArgs = @()
    if ($Command.Count -gt 1) {{
      $CliArgs = @($Command[1..($Command.Count - 1)])
    }}
    & $CommandName @CliArgs
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
