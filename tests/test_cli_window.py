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
        self.assertIn("Prompt file:", script.read_text(encoding="utf-8"))

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
        self.assertIn("--output-last-message", content)
        self.assertIn("--sandbox', 'read-only", content)
        self.assertIn("--skip-git-repo-check", content)

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
        self.assertFalse(kwargs.get("shell"))
        self.assertFalse(result["cliAvailable"])
        self.assertEqual(result["pid"], 123)


if __name__ == "__main__":
    unittest.main()
