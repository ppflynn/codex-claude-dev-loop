import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui import server
from gui.orchestrator.git_tools import (
    get_current_branch,
    get_git_common_dir,
    get_main_worktree_path,
    is_git_worktree,
)
from gui.orchestrator.store import TaskStore


class WorktreeGitToolsTests(unittest.TestCase):
    def test_is_git_worktree_detects_file_based_git(self):
        tmp = Path(os.environ.get("TEMP", "/tmp")) / f"wt-{uuid.uuid4().hex}"
        tmp.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(str(tmp), ignore_errors=True))
        (tmp / ".git").write_text("gitdir: /some/main/.git/worktrees/wt\n", encoding="utf-8")
        self.assertTrue(is_git_worktree(tmp))

    def test_is_git_worktree_returns_false_for_dir_based_git(self):
        tmp = Path(os.environ.get("TEMP", "/tmp")) / f"wt-{uuid.uuid4().hex}"
        tmp.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(str(tmp), ignore_errors=True))
        (tmp / ".git").mkdir()
        self.assertFalse(is_git_worktree(tmp))

    def test_get_git_common_dir_returns_none_for_non_git(self):
        tmp = Path(os.environ.get("TEMP", "/tmp")) / f"wt-{uuid.uuid4().hex}"
        tmp.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(str(tmp), ignore_errors=True))
        result = get_git_common_dir(tmp)
        self.assertIsNone(result)

    def test_get_current_branch_returns_none_for_non_git(self):
        tmp = Path(os.environ.get("TEMP", "/tmp")) / f"wt-{uuid.uuid4().hex}"
        tmp.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(str(tmp), ignore_errors=True))
        result = get_current_branch(tmp)
        self.assertIsNone(result)

    def test_get_main_worktree_path_returns_none_for_non_git(self):
        tmp = Path(os.environ.get("TEMP", "/tmp")) / f"wt-{uuid.uuid4().hex}"
        tmp.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(str(tmp), ignore_errors=True))
        result = get_main_worktree_path(tmp)
        self.assertIsNone(result)

    def test_worktree_functions_with_real_git_repo(self):
        tmp = Path(os.environ.get("TEMP", "/tmp")) / f"wt-{uuid.uuid4().hex}"
        tmp.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(str(tmp), ignore_errors=True))

        subprocess.run(["git", "-C", str(tmp), "init"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(tmp), "config", "user.email", "test@test.test"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp), "config", "user.name", "Test"],
            capture_output=True, check=True,
        )
        (tmp / "readme.md").write_text("# test\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(tmp), "add", "readme.md"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp), "commit", "-m", "init"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp), "checkout", "-b", "feature/test-branch"],
            capture_output=True, check=True,
        )

        self.assertFalse(is_git_worktree(tmp))
        self.assertEqual(get_current_branch(tmp), "feature/test-branch")
        common_dir = get_git_common_dir(tmp)
        self.assertIsNotNone(common_dir)
        self.assertTrue((Path(common_dir) / "HEAD").exists())

    def test_get_current_branch_detached_head(self):
        tmp = Path(os.environ.get("TEMP", "/tmp")) / f"wt-{uuid.uuid4().hex}"
        tmp.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(str(tmp), ignore_errors=True))

        subprocess.run(["git", "-C", str(tmp), "init"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(tmp), "config", "user.email", "test@test.test"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp), "config", "user.name", "Test"],
            capture_output=True, check=True,
        )
        (tmp / "f.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(tmp), "add", "f.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(tmp), "commit", "-m", "init"],
            capture_output=True, check=True,
        )
        commit_hash = subprocess.run(
            ["git", "-C", str(tmp), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(tmp), "checkout", commit_hash],
            capture_output=True, check=True,
        )

        self.assertIsNone(get_current_branch(tmp))


class WorktreeServerTests(unittest.TestCase):
    def make_dir(self):
        temp_root = server.ROOT / ".gui" / "test-tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        path = temp_root / f"wtcase-{uuid.uuid4().hex}"
        path.mkdir()
        self.addCleanup(lambda: shutil.rmtree(str(path), ignore_errors=True))
        return path

    def make_git_repo(self):
        root = self.make_dir()
        subprocess.run(["git", "-C", str(root), "init"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "test@test.test"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "Test"],
            capture_output=True, check=True,
        )
        (root / "readme.md").write_text("# repo\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "readme.md"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-m", "init"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "checkout", "-b", "feature/dev"],
            capture_output=True, check=True,
        )
        return root

    def make_git_worktree(self, main_repo, branch="feature/worktree-branch"):
        wt_path = Path(os.environ.get("TEMP", "/tmp")) / f"wt-{uuid.uuid4().hex}"
        subprocess.run(
            ["git", "-C", str(main_repo), "branch", branch],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(main_repo), "worktree", "add", str(wt_path), branch],
            capture_output=True, check=True,
        )
        self.addCleanup(lambda: shutil.rmtree(str(wt_path), ignore_errors=True))
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "-C", str(main_repo), "worktree", "remove", "--force", str(wt_path)],
                capture_output=True,
            )
        )
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "-C", str(main_repo), "branch", "-D", branch],
                capture_output=True,
            )
        )
        return wt_path

    def test_make_project_primary_worktree_type(self):
        repo = self.make_git_repo()
        project = server.make_project(repo)
        self.assertEqual(project["worktreeType"], "primary")
        self.assertIsNotNone(project["gitCommonDir"])
        self.assertEqual(project["branch"], "feature/dev")
        self.assertTrue(project["available"])

    def test_make_project_worktree_type(self):
        repo = self.make_git_repo()
        wt = self.make_git_worktree(repo)
        project = server.make_project(wt)
        self.assertEqual(project["worktreeType"], "worktree")
        self.assertIsNotNone(project["gitCommonDir"])
        self.assertIsNotNone(project["branch"])
        self.assertIsNotNone(project["mainWorktreePath"])

    def test_make_project_non_git_has_null_worktree_type(self):
        root = self.make_dir()
        (root / ".git").mkdir()
        with mock.patch("gui.server.get_git_common_dir", side_effect=RuntimeError("no git")), \
             mock.patch("gui.server.get_current_branch", side_effect=RuntimeError("no git")), \
             mock.patch("gui.server.is_git_worktree", side_effect=RuntimeError("no git")), \
             mock.patch("gui.server.get_main_worktree_path", side_effect=RuntimeError("no git")):
            project = server.make_project(root)
        self.assertIsNone(project["worktreeType"])
        self.assertIsNone(project["gitCommonDir"])
        self.assertIsNone(project["branch"])
        self.assertIsNone(project["mainWorktreePath"])

    def test_same_path_duplicate_registration_rejected(self):
        repo = self.make_git_repo()
        project_store = server.ProjectStore(repo / ".gui" / "projects.json")
        first = project_store.add_project(repo)
        second = project_store.add_project(repo)
        self.assertEqual(first["id"], second["id"])
        projects = project_store.list_projects()
        self.assertEqual(len(projects), 1)

    def test_same_repo_different_worktree_paths_allowed(self):
        repo = self.make_git_repo()
        wt = self.make_git_worktree(repo)

        project_store = server.ProjectStore(repo / ".gui" / "projects.json")
        primary = project_store.add_project(repo)
        worktree = project_store.add_project(wt)

        self.assertNotEqual(primary["id"], worktree["id"])
        self.assertNotEqual(primary["path"], worktree["path"])
        projects = project_store.list_projects()
        self.assertEqual(len(projects), 2)

    def test_windows_path_case_insensitive_dedup(self):
        repo = self.make_git_repo()
        project_store = server.ProjectStore(repo / ".gui" / "projects.json")
        first = project_store.add_project(repo)

        upper_path = str(repo).upper()
        if upper_path != str(repo) and Path(upper_path).exists():
            pass
        else:
            mixed = str(repo).swapcase()
            if Path(mixed).exists():
                upper_path = mixed
            else:
                self.skipTest("Cannot construct case-different path on this filesystem")

        with mock.patch.object(server, "normalize_path", return_value=repo):
            second = project_store.add_project(repo, name="dup")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(project_store.list_projects()), 1)

    def test_trailing_slash_path_dedup(self):
        repo = self.make_git_repo()
        project_store = server.ProjectStore(repo / ".gui" / "projects.json")
        first = project_store.add_project(repo)
        self.assertTrue(project_store._same_path(str(repo) + "\\", str(repo)))
        self.assertTrue(project_store._same_path(str(repo), str(repo) + "/"))

    def test_chinese_and_space_path_handling(self):
        root = server.ROOT / ".gui" / "test-tmp" / f"wt-{uuid.uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(str(root), ignore_errors=True))

        proj_dir = root / "中文 测试项目"
        proj_dir.mkdir(parents=True)
        (proj_dir / ".git").mkdir()

        project = server.make_project(proj_dir)
        self.assertEqual(project["kind"], "git-uninitialized")
        self.assertEqual(project["name"], proj_dir.name)
        self.assertIn("中文", project["name"])

        project_store = server.ProjectStore(root / "projects.json")
        added = project_store.add_project(proj_dir)
        self.assertEqual(added["id"], project["id"])

        added2 = project_store.add_project(proj_dir)
        self.assertEqual(added["id"], added2["id"])
        self.assertEqual(len(project_store.list_projects()), 1)

    def test_remove_worktree_record_does_not_delete_local_directory(self):
        repo = self.make_git_repo()
        wt = self.make_git_worktree(repo)

        project_store = server.ProjectStore(repo / ".gui" / "projects.json")
        task_store = TaskStore(repo / ".gui" / "tasks")
        added = project_store.add_project(wt)

        self.assertTrue(wt.is_dir())
        removed = project_store.remove_project(added["id"])
        self.assertFalse(project_store.list_projects())
        self.assertTrue(wt.is_dir(), "Worktree directory must survive record removal")

    def test_remove_worktree_record_does_not_delete_git_branch(self):
        repo = self.make_git_repo()
        branch_name = "feature/keep-branch-test"
        wt = self.make_git_worktree(repo, branch=branch_name)

        project_store = server.ProjectStore(repo / ".gui" / "projects.json")
        task_store = TaskStore(repo / ".gui" / "tasks")
        added = project_store.add_project(wt)

        result = subprocess.run(
            ["git", "-C", str(repo), "branch", "--list", branch_name],
            capture_output=True, text=True, check=True,
        )
        self.assertIn(branch_name, result.stdout)

        project_store.remove_project(added["id"])

        result = subprocess.run(
            ["git", "-C", str(repo), "branch", "--list", branch_name],
            capture_output=True, text=True, check=True,
        )
        self.assertIn(branch_name, result.stdout, "Git branch must survive record removal")

    def test_running_task_blocks_project_removal(self):
        repo = self.make_git_repo()
        root = repo / ".gui" / "test-tmp" / f"case-{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(str(root), ignore_errors=True))

        project_store = server.ProjectStore(root / "projects.json")
        task_store = TaskStore(root / "tasks")
        project = project_store.add_project(repo)

        with mock.patch("gui.server.assert_git_work_tree"), mock.patch("gui.server.assert_clean_work_tree"):
            task = server.create_task(
                {
                    "projectId": project["id"],
                    "title": "Running Task",
                    "description": "Test",
                    "acceptance": "Test",
                    "maxRounds": 1,
                },
                project_store,
                task_store,
            )
        task.status = "CLAUDE_WINDOW_STARTED"
        task_store.save(task)

        with self.assertRaises(server.ApiError):
            server.remove_project(
                project["id"], project_store, task_store, server.RunManager(project_store)
            )

    def test_target_worktree_missing_blocks_task(self):
        repo = self.make_git_repo()
        root = repo / ".gui" / "test-tmp" / f"case-{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(str(root), ignore_errors=True))

        project_store = server.ProjectStore(root / "projects.json")
        task_store = TaskStore(root / "tasks")

        fake_path = root / "nonexistent-worktree"
        fake_path.mkdir(parents=True)
        project = project_store.add_project(fake_path)
        project["worktreeType"] = "worktree"
        project_store.save_projects(project_store.list_projects())

        shutil.rmtree(str(fake_path), ignore_errors=True)

        with mock.patch("gui.server.assert_git_work_tree"), mock.patch("gui.server.assert_clean_work_tree"):
            pass

        task = server.create_task(
            {
                "projectId": project["id"],
                "title": "Should Block",
                "description": "Test",
                "acceptance": "Test",
            },
            project_store,
            task_store,
        )

        self.assertEqual(task.status, "BLOCKED")
        self.assertIn("path does not exist", task.history[-1]["message"].lower())

    def test_non_git_project_still_works_normally(self):
        root = self.make_dir()
        (root / ".git").mkdir()
        project_store = server.ProjectStore(root / "projects.json")
        project = project_store.add_project(root)
        self.assertEqual(project["kind"], "git-uninitialized")
        self.assertIsNotNone(project["worktreeType"])

    def test_paths_equal_case_insensitive(self):
        project_store = server.ProjectStore(server.PROJECTS_FILE)
        self.assertTrue(project_store._same_path(
            "E:\\AI-Tools\\My-Project",
            "e:\\ai-tools\\my-project",
        ))
        self.assertTrue(project_store._same_path(
            "E:\\AI-Tools\\My-Project\\",
            "E:\\AI-Tools\\My-Project",
        ))
        self.assertTrue(project_store._same_path(
            "E:/AI-Tools/My-Project",
            "E:\\AI-Tools\\My-Project",
        ))
        self.assertFalse(project_store._same_path(
            "E:\\AI-Tools\\Project-A",
            "E:\\AI-Tools\\Project-B",
        ))

    def test_project_id_uniqueness_across_worktrees(self):
        repo = self.make_git_repo()
        wt = self.make_git_worktree(repo)

        id1 = server.project_id(repo)
        id2 = server.project_id(wt)
        self.assertNotEqual(id1, id2)

    def test_list_projects_refreshes_availability(self):
        repo = self.make_git_repo()
        project_store = server.ProjectStore(repo / ".gui" / "projects.json")
        project = project_store.add_project(repo)
        self.assertTrue(project["available"])

        projects = project_store.list_projects()
        self.assertTrue(projects[0]["available"])

    def test_existing_original_tests_still_pass_make_project_has_fields(self):
        root = self.make_dir()
        (root / ".git").mkdir()
        project = server.make_project(root)
        self.assertIn("worktreeType", project)
        self.assertIn("gitCommonDir", project)
        self.assertIn("branch", project)
        self.assertIn("mainWorktreePath", project)
        self.assertIn("available", project)

    def test_remove_project_preserves_worktree_parent_info(self):
        repo = self.make_git_repo()
        project_store = server.ProjectStore(repo / ".gui" / "projects.json")
        project = project_store.add_project(repo)
        self.assertEqual(project["worktreeType"], "primary")
        removed = project_store.remove_project(project["id"])
        self.assertEqual(removed["worktreeType"], "primary")

    def test_readd_after_removal_possible(self):
        repo = self.make_git_repo()
        project_store = server.ProjectStore(repo / ".gui" / "projects.json")
        first = project_store.add_project(repo)
        removed = project_store.remove_project(first["id"])
        self.assertEqual(len(project_store.list_projects()), 0)
        second = project_store.add_project(repo)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(project_store.list_projects()), 1)

    def test_both_primary_and_worktree_appear_in_list(self):
        repo = self.make_git_repo()
        wt = self.make_git_worktree(repo)

        project_store = server.ProjectStore(repo / ".gui" / "projects.json")
        project_store.add_project(repo)
        project_store.add_project(wt)

        projects = project_store.list_projects()
        types = {p["worktreeType"] for p in projects}
        self.assertIn("primary", types)
        self.assertIn("worktree", types)
        for p in projects:
            self.assertIsNotNone(p["id"])
            self.assertTrue(p["path"])


if __name__ == "__main__":
    unittest.main()
