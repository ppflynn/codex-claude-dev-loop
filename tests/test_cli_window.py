import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui.orchestrator.cli_window import generate_launcher_script, launch_cli_window
from gui.orchestrator.models import Task


class CliWindowTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / ".gui" / "test-tmp" / uuid.uuid4().hex
        self.project = self.root / "project"
        self.task_dir = self.root / "tasks" / "task_test"
        self.project.mkdir(parents=True)
        self.task_dir.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.task = Task.create(
            task_id="task_test",
            project_id="project1",
            project_path=str(self.project),
            title="Title",
            description="Description",
            acceptance="Acceptance",
        )
        self.prompt = self.task_dir / "CLAUDE_IMPLEMENT_PROMPT.md"
        self.prompt.write_text("prompt", encoding="utf-8")

    def test_generate_claude_launcher_inside_task_dir(self):
        script = generate_launcher_script(
            task=self.task,
            task_dir=self.task_dir,
            kind="claude",
            command=["claude"],
            prompt_path=self.prompt,
        )
        self.assertTrue(script.exists())
        self.assertEqual(script.parent, self.task_dir)
        content = script.read_text(encoding="utf-8")
        self.assertIn("Prompt file:", content)
        self.assertIn("Removed unsupported SOCKS proxy env for Claude", content)
        self.assertIn("[Environment]::SetEnvironmentVariable($ProxyVariable, $null, 'Process')", content)
        self.assertIn("socks[45]", content)
        self.assertIn("Claude args:", content)
        self.assertIn("$ClaudeArgs += '-p'", content)
        self.assertIn("$ClaudeArgs += @('--permission-mode', 'bypassPermissions')", content)
        self.assertIn("Write-NativeChunk", content)
        self.assertIn("Receive-NativeStreamChunk", content)
        self.assertIn("Invoke-StreamingNativeProcess", content)
        self.assertIn("Write-Host $Text -NoNewline", content)
        self.assertIn("Claude is running. Output will appear below.", content)
        self.assertIn("-Arguments $ClaudeArgs", content)
        self.assertIn("[System.Text.Encoding]::UTF8.GetBytes($PromptText)", content)
        self.assertIn("StandardInput.BaseStream.Write", content)
        self.assertIn("BaseStream.ReadAsync", content)
        self.assertNotIn("ReadToEndAsync()", content)
        self.assertNotIn("& $CommandName $PromptText", content)

    def test_generate_claude_launcher_preserves_configured_args(self):
        script = generate_launcher_script(
            task=self.task,
            task_dir=self.task_dir,
            kind="claude",
            command=["claude", "--model", "sonnet"],
            prompt_path=self.prompt,
        )
        content = script.read_text(encoding="utf-8")
        self.assertIn('"--model", "sonnet"', content)
        self.assertIn("$ClaudeArgs += @($Command[1..($Command.Count - 1)])", content)
        self.assertIn("if (-not $HasPrintMode)", content)
        self.assertIn("if (-not $HasPermissionMode)", content)
        self.assertIn("$ProcessInfo.Arguments = Join-NativeArguments $Arguments", content)
        self.assertIn("-Arguments $ClaudeArgs", content)

    def test_generate_codex_launcher_mentions_output_file(self):
        output = self.task_dir / "CODEX_REVIEW.json"
        script = generate_launcher_script(
            task=self.task,
            task_dir=self.task_dir,
            kind="codex",
            command=["codex"],
            prompt_path=self.prompt,
            output_path=output,
        )
        content = script.read_text(encoding="utf-8")
        self.assertIn("CODEX_REVIEW.json", content)
        self.assertIn("CODEX_HOME", content)
        self.assertIn("$env:CODEX_HOME = $CodexHome", content)
        self.assertNotIn("CodexHomeForLog", content)
        self.assertNotIn("default user Codex home", content)
        self.assertIn(str(self.task_dir), content)
        self.assertIn("codex-home-round-1-", content)
        self.assertIn("[Guid]::NewGuid()", content)
        self.assertIn("auth.json", content)
        self.assertIn("config.toml", content)
        self.assertIn("Copy-Item -LiteralPath $SourceFile", content)
        self.assertIn("Codex minimal config", content)
        self.assertIn('sandbox_mode = "workspace-write"', content)
        self.assertIn("Removed temporary Codex auth/config files", content)
        self.assertIn("$WorkingDir = $TaskDir", content)
        self.assertIn(str(self.project).lower().replace("\\", "\\\\"), content.lower())
        self.assertIn(str(self.task_dir).lower().replace("\\", "\\\\"), content.lower())
        self.assertNotIn("[windows]", content)
        self.assertNotIn("[mcp_servers", content)
        self.assertIn("--ephemeral", content)
        self.assertIn("--output-last-message", content)
        self.assertIn("--sandbox', 'workspace-write", content)
        self.assertIn("--color', 'never", content)
        self.assertIn("'-C', $TaskDir", content)
        self.assertIn("--skip-git-repo-check", content)
        self.assertIn('("Codex args: {0}" -f ($CodexArgs -join', content)
        self.assertNotIn("--version", content)
        self.assertIn("System.Diagnostics.ProcessStartInfo", content)
        self.assertIn("RedirectStandardError = $true", content)
        self.assertIn("Invoke-StreamingNativeProcess", content)
        self.assertIn("Codex is running. Output will appear below.", content)
        self.assertIn("-Arguments $CodexArgs", content)
        self.assertIn("BaseStream.ReadAsync", content)
        self.assertNotIn("ReadToEndAsync()", content)
        self.assertIn("[System.Text.Encoding]::UTF8.GetBytes($PromptText)", content)
        self.assertIn("StandardInput.BaseStream.Write", content)
        self.assertNotIn("StandardInput.Write($PromptText)", content)
        self.assertIn("$ProcessInfo.Arguments = Join-NativeArguments $Arguments", content)
        self.assertNotIn("Tee-Object", content)
        self.assertNotIn("2>&1", content)
        self.assertIn("Codex completed without creating the expected output file", content)
        self.assertIn("if ($Code -eq 0) { $Code = 1 }", content)

    def test_launch_uses_argv_and_not_shell_true(self):
        fake_process = mock.Mock(spec=subprocess.Popen)
        fake_process.pid = 123
        with mock.patch("gui.orchestrator.cli_window.subprocess.Popen", return_value=fake_process) as popen:
            result = launch_cli_window(
                task=self.task,
                task_dir=self.task_dir,
                kind="claude",
                command=["definitely_missing_claude_for_test"],
                prompt_path=self.prompt,
            )
        args, kwargs = popen.call_args
        self.assertIsInstance(args[0], list)
        self.assertIn("-NoExit", args[0])
        self.assertFalse(kwargs.get("shell"))
        self.assertFalse(result["cliAvailable"])
        self.assertEqual(result["pid"], 123)


    def test_launch_cli_window_returns_log_metadata(self):
        fake_process = mock.Mock(spec=subprocess.Popen)
        fake_process.pid = 456
        with mock.patch("gui.orchestrator.cli_window.subprocess.Popen", return_value=fake_process):
            result = launch_cli_window(
                task=self.task,
                task_dir=self.task_dir,
                kind="claude",
                command=["claude"],
                prompt_path=self.prompt,
            )
        self.assertIn("logPath", result)
        self.assertIn("logName", result)
        self.assertEqual(result["logName"], "claude_window_round_1.log")
        self.assertIn("claude_window_round_1.log", result["logPath"])
        self.assertIn(str(self.task_dir), result["logPath"])

    def test_launch_codex_window_returns_log_metadata(self):
        fake_process = mock.Mock(spec=subprocess.Popen)
        fake_process.pid = 789
        output = self.task_dir / "CODEX_REVIEW.json"
        with mock.patch("gui.orchestrator.cli_window.subprocess.Popen", return_value=fake_process):
            result = launch_cli_window(
                task=self.task,
                task_dir=self.task_dir,
                kind="codex",
                command=["codex"],
                prompt_path=self.prompt,
                output_path=output,
            )
        self.assertIn("logPath", result)
        self.assertIn("logName", result)
        self.assertEqual(result["logName"], "codex_window_round_1.log")
        self.assertIn("codex_window_round_1.log", result["logPath"])


if __name__ == "__main__":
    unittest.main()
