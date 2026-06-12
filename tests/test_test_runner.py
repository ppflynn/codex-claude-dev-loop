import shutil
import sys
import unittest
import uuid
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui.orchestrator.test_runner import parse_command, run_tests


class TestRunnerTests(unittest.TestCase):
    def make_dir(self):
        root = Path(__file__).resolve().parents[1] / ".gui" / "test-tmp" / uuid.uuid4().hex
        root.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        return root

    def test_argv_command_success_records_exit_zero(self):
        root = self.make_dir()
        result = run_tests(root, root / "task", 1, f'"{sys.executable}" -c "raise SystemExit(0)"')
        self.assertEqual(result.exit_code, 0)
        self.assertIn("EXIT_CODE: 0", result.output)

    def test_argv_command_failure_records_nonzero(self):
        root = self.make_dir()
        result = run_tests(root, root / "task", 1, f'"{sys.executable}" -c "raise SystemExit(5)"')
        self.assertEqual(result.exit_code, 5)
        self.assertIn("EXIT_CODE: 5", result.output)

    def test_shell_control_syntax_is_rejected(self):
        for command in ("pytest && echo ok", "pytest | tee out", "pytest > out", "pytest; echo ok", "echo $(whoami)"):
            with self.assertRaises(ValueError):
                parse_command(command)

    def test_no_test_command_is_recorded(self):
        root = self.make_dir()
        result = run_tests(root, root / "task", 1, "")
        self.assertIsNone(result.command)
        self.assertIn("NO_TEST_COMMAND", result.output)


if __name__ == "__main__":
    unittest.main()
