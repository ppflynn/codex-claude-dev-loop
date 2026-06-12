import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui.orchestrator import git_tools


def completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


class GitToolsTests(unittest.TestCase):
    def make_dir(self):
        root = Path(__file__).resolve().parents[1] / ".gui" / "test-tmp" / uuid.uuid4().hex
        root.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        return root

    def test_non_git_repo_is_rejected(self):
        with mock.patch("gui.orchestrator.git_tools._run_git", return_value=completed([], 128, "", "not a git repo")):
            with self.assertRaises(git_tools.GitError):
                git_tools.assert_git_work_tree(self.make_dir())

    def test_clean_repo_passes_and_dirty_repo_fails(self):
        root = self.make_dir()
        with mock.patch("gui.orchestrator.git_tools._run_git", return_value=completed([], 0, "")):
            git_tools.assert_clean_work_tree(root)
        with mock.patch("gui.orchestrator.git_tools._run_git", return_value=completed([], 0, "?? new.txt\n")):
            with self.assertRaises(git_tools.DirtyWorkTreeError):
                git_tools.assert_clean_work_tree(root)

    def test_collect_status_and_diff_artifacts(self):
        root = self.make_dir()
        task_dir = self.make_dir() / "task"
        responses = [
            completed([], 0, "true\n"),
            completed([], 0, " M src/app.py\n"),
            completed([], 0, " src/app.py | 2 +-\n"),
            completed([], 0, "diff --git a/src/app.py b/src/app.py\n"),
        ]
        with mock.patch("gui.orchestrator.git_tools._run_git", side_effect=responses):
            artifacts = git_tools.collect_git_artifacts(root, task_dir, 1)
        self.assertTrue(artifacts.status_path.exists())
        self.assertTrue(artifacts.diff_stat_path.exists())
        self.assertTrue(artifacts.diff_path.exists())

    def test_env_change_blocks_diff_content(self):
        root = self.make_dir()
        task_dir = self.make_dir() / "task"
        responses = [
            completed([], 0, "true\n"),
            completed([], 0, "?? .env\n"),
        ]
        with mock.patch("gui.orchestrator.git_tools._run_git", side_effect=responses):
            with self.assertRaises(git_tools.EnvFileChangedError):
                git_tools.collect_git_artifacts(root, task_dir, 1)
        self.assertNotIn("SECRET=1", (task_dir / "git_diff_round_1.diff").read_text(encoding="utf-8"))

    def test_source_does_not_execute_forbidden_git_commands(self):
        source = (Path(__file__).resolve().parents[1] / "gui" / "orchestrator" / "git_tools.py").read_text(encoding="utf-8")
        for forbidden in ('"commit"', '"push"', '"reset"', '"clean"', '"checkout"', '"switch"', '"restore"'):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
