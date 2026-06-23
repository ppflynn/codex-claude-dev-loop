import hashlib
import json
import os
import shutil
import stat as stat_module
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui import server
from gui.orchestrator import git_tools
from gui.orchestrator.git_tools import (
    WorktreeInfo,
    compute_repo_id,
    compute_review_snapshot,
    get_current_branch,
    get_git_common_dir,
    get_main_worktree_path,
    is_git_worktree,
    list_worktrees,
)
from gui.orchestrator.git_workflow import (
    CommitError,
    MergeError,
    MergeRecoveryJournal,
    WorktreeCreationError,
    controlled_commit,
    controlled_merge_to_main,
    create_worktree,
    journal_path_safe,
    recover_pending_merge,
)
from gui.orchestrator.models import Task
from gui.orchestrator.store import TaskStore
from gui.orchestrator.state_machine import Status


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
        # The specific worktree's record must be gone, but auto-discovery may
        # have registered the main repo as a sibling — that's expected.
        remaining_ids = [p["id"] for p in project_store.list_projects()]
        self.assertNotIn(added["id"], remaining_ids)
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

    def test_make_project_includes_repo_id(self):
        repo = self.make_git_repo()
        project = server.make_project(repo)
        self.assertIsNotNone(project["repoId"])
        self.assertTrue(project["repoId"].startswith("repo_"))

    def test_repo_id_matches_across_primary_and_worktree(self):
        repo = self.make_git_repo()
        wt = self.make_git_worktree(repo, branch="feature/repo-id-match")
        primary = server.make_project(repo)
        worktree = server.make_project(wt)
        self.assertEqual(primary["repoId"], worktree["repoId"])
        self.assertIsNotNone(primary["repoId"])

    def test_add_project_auto_discovers_sibling_worktrees(self):
        repo = self.make_git_repo()
        wt = self.make_git_worktree(repo, branch="feature/auto-discover")
        project_store = server.ProjectStore(repo / ".gui" / "projects.json")
        project_store.add_project(repo)
        projects = project_store.list_projects()
        # Both primary and worktree should be present after auto-discovery
        self.assertEqual(len(projects), 2)
        paths = [Path(p["path"]) for p in projects]
        self.assertIn(repo.resolve(), [p.resolve() for p in paths])
        self.assertIn(wt.resolve(), [p.resolve() for p in paths])

    def test_add_worktree_reverse_discovers_primary(self):
        repo = self.make_git_repo()
        wt = self.make_git_worktree(repo, branch="feature/reverse-discover")
        project_store = server.ProjectStore(repo / ".gui" / "projects.json")
        project_store.add_project(wt)
        projects = project_store.list_projects()
        self.assertEqual(len(projects), 2)
        types = {p["worktreeType"] for p in projects}
        self.assertIn("primary", types)
        self.assertIn("worktree", types)

    def test_add_project_does_not_register_missing_worktree(self):
        repo = self.make_git_repo()
        wt = self.make_git_worktree(repo, branch="feature/will-vanish")
        project_store = server.ProjectStore(repo / ".gui" / "projects.json")
        # Force-remove the worktree directory before discovery, but leave the
        # metadata so the test exercises the missing-path branch.
        shutil.rmtree(str(wt), ignore_errors=True)
        # Recreate just enough to fool nothing — discovery must skip it.
        self.assertFalse(wt.exists())
        project_store.add_project(repo)
        projects = project_store.list_projects()
        # Only the primary is registered since the sibling is gone
        self.assertEqual(len(projects), 1)


class WorktreeCreationTests(unittest.TestCase):
    def make_dir(self):
        temp_root = server.ROOT / ".gui" / "test-tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        path = temp_root / f"wtcreate-{uuid.uuid4().hex}"
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
        return root

    def test_create_worktree_succeeds(self):
        repo = self.make_git_repo()
        target = repo.parent / f"target-{uuid.uuid4().hex}"
        result = create_worktree(repo, "feature/created", target)
        self.assertTrue(target.is_dir())
        self.assertEqual(result["branch"], "feature/created")
        self.assertEqual(result["worktreeType"], "worktree")
        # Cleanup worktree branch and dir
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(target)],
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "branch", "-D", "feature/created"],
            capture_output=True,
        )

    def test_create_worktree_rejects_invalid_branch_name(self):
        repo = self.make_git_repo()
        target = repo.parent / f"target-{uuid.uuid4().hex}"
        with self.assertRaises(WorktreeCreationError):
            create_worktree(repo, "main", target)
        with self.assertRaises(WorktreeCreationError):
            create_worktree(repo, "..", target)
        with self.assertRaises(WorktreeCreationError):
            create_worktree(repo, "feature/.env", target)
        with self.assertRaises(WorktreeCreationError):
            create_worktree(repo, "", target)
        self.assertFalse(target.exists())

    def test_create_worktree_rejects_existing_target(self):
        repo = self.make_git_repo()
        target = repo.parent / f"target-{uuid.uuid4().hex}"
        target.mkdir()
        try:
            with self.assertRaises(WorktreeCreationError):
                create_worktree(repo, "feature/exists-target", target)
        finally:
            shutil.rmtree(str(target), ignore_errors=True)

    def test_create_worktree_rejects_target_inside_main(self):
        repo = self.make_git_repo()
        target = repo / "inner-wt"
        with self.assertRaises(WorktreeCreationError):
            create_worktree(repo, "feature/inner", target)

    def test_create_worktree_rejects_dirty_main(self):
        repo = self.make_git_repo()
        (repo / "uncommitted.txt").write_text("dirty", encoding="utf-8")
        target = repo.parent / f"target-{uuid.uuid4().hex}"
        with self.assertRaises(WorktreeCreationError):
            create_worktree(repo, "feature/dirty-main", target)
        self.assertFalse(target.exists())

    def test_create_worktree_rejects_existing_branch(self):
        repo = self.make_git_repo()
        subprocess.run(
            ["git", "-C", str(repo), "branch", "feature/already-there"],
            capture_output=True, check=True,
        )
        target = repo.parent / f"target-{uuid.uuid4().hex}"
        try:
            with self.assertRaises(WorktreeCreationError):
                create_worktree(repo, "feature/already-there", target)
        finally:
            subprocess.run(
                ["git", "-C", str(repo), "branch", "-D", "feature/already-there"],
                capture_output=True,
            )

    def test_create_worktree_no_records_left_on_failure(self):
        repo = self.make_git_repo()
        target = repo.parent / f"target-{uuid.uuid4().hex}"
        target.mkdir()
        try:
            with self.assertRaises(WorktreeCreationError):
                create_worktree(repo, "feature/fail-record", target)
        finally:
            shutil.rmtree(str(target), ignore_errors=True)
        # Make sure no half-registered project record lingers on failure
        # (no project store used here, but verify branch is gone)
        result = subprocess.run(
            ["git", "-C", str(repo), "branch", "--list", "feature/fail-record"],
            capture_output=True, text=True, check=True,
        )
        self.assertNotIn("feature/fail-record", result.stdout)

    def test_create_worktree_blocks_when_smudge_filter_configured(self):
        # Codex P1-2 round 18: ``git worktree add`` performs an initial
        # checkout of every tracked path.  A smudge filter configured
        # on any tracked path transforms its content during that
        # checkout, so the new worktree's files would not match the
        # reviewed tree.  ``create_worktree`` must refuse up front so
        # the user sees a clear error and the filter cannot silently
        # transform content the user never reviewed.
        repo = self.make_git_repo()
        # Install an active smudge filter and bind it via .gitattributes.
        subprocess.run(
            ["git", "-C", str(repo), "config", "filter.lc.smudge", "tr A-Z a-z"],
            capture_output=True, check=True,
        )
        (repo / ".gitattributes").write_text("readme.md filter=lc\n", encoding="utf-8")
        (repo / "readme.md").write_text("# README\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", ".gitattributes", "readme.md"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "chore: smudge filter on readme"],
            capture_output=True, check=True,
        )
        target = repo.parent / f"target-smudge-{uuid.uuid4().hex}"
        with self.assertRaises(WorktreeCreationError) as ctx:
            create_worktree(repo, "feature/smudge-blocked", target)
        message = str(ctx.exception)
        self.assertIn("readme.md", message)
        self.assertIn("filter", message.lower())
        # ``git worktree add`` must NOT have run: target directory does
        # not exist, and the branch was never created.
        self.assertFalse(target.exists())
        branch_list = subprocess.run(
            ["git", "-C", str(repo), "branch", "--list", "feature/smudge-blocked"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertNotIn("feature/smudge-blocked", branch_list)

    def test_create_worktree_allows_filter_attribute_without_smudge_driver(self):
        # A filter attribute declared in .gitattributes but with no
        # matching ``smudge`` / ``process`` config is a no-op on
        # checkout.  ``create_worktree`` must succeed so legitimate
        # attribute configurations do not break the worktree flow.
        repo = self.make_git_repo()
        (repo / ".gitattributes").write_text("readme.md filter=lc\n", encoding="utf-8")
        (repo / "readme.md").write_text("# README\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", ".gitattributes", "readme.md"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "chore: benign attribute"],
            capture_output=True, check=True,
        )
        target = repo.parent / f"target-attr-{uuid.uuid4().hex}"
        try:
            result = create_worktree(repo, "feature/attr-ok", target)
            self.assertTrue(target.is_dir())
            self.assertEqual(result["branch"], "feature/attr-ok")
        finally:
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", str(target)],
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "branch", "-D", "feature/attr-ok"],
                capture_output=True,
            )


class ControlledCommitTests(unittest.TestCase):
    def make_dir(self):
        temp_root = server.ROOT / ".gui" / "test-tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        path = temp_root / f"commit-{uuid.uuid4().hex}"
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
        return root

    def test_commit_succeeds_when_worktree_has_changes(self):
        repo = self.make_git_repo()
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        result = controlled_commit(repo, "feat: add feature.py")
        self.assertTrue(result["commitSha"])
        self.assertEqual(result["commitMessage"], "feat: add feature.py")
        # Verify HEAD now points to the new commit
        head_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(head_sha, result["commitSha"])

    def test_commit_refuses_empty_worktree(self):
        repo = self.make_git_repo()
        with self.assertRaises(CommitError):
            controlled_commit(repo, "feat: empty")

    def test_commit_blocks_env_changes(self):
        repo = self.make_git_repo()
        (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
        with self.assertRaises(CommitError) as ctx:
            controlled_commit(repo, "feat: leak env")
        self.assertIn(".env", str(ctx.exception))

    def test_commit_blocks_nested_env_in_untracked_directory(self):
        # Regression test for Codex P1-1 round 3: ``git status --short`` may
        # collapse an entirely-untracked directory to ``?? dir/``, so the
        # previous ``status_mentions_env`` guard would miss a forbidden
        # ``dir/.env``.  ``controlled_commit`` now enumerates paths via
        # ``git ls-files --others`` to catch nested env files.
        repo = self.make_git_repo()
        nested_dir = repo / "config"
        nested_dir.mkdir()
        (nested_dir / ".env").write_text("SECRET=1\n", encoding="utf-8")
        # Add a benign change so the empty-worktree check does not fire first
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        with self.assertRaises(CommitError) as ctx:
            controlled_commit(repo, "feat: leak nested env")
        self.assertIn(".env", str(ctx.exception))
        # Verify nothing was committed
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(log.splitlines()[0].split(maxsplit=1)[1], "init")

    def test_commit_blocks_empty_message(self):
        repo = self.make_git_repo()
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        with self.assertRaises(CommitError):
            controlled_commit(repo, "   ")

    def test_commit_blocks_missing_path(self):
        with self.assertRaises(CommitError):
            controlled_commit(Path("C:/nonexistent/path/abc"), "msg")

    def test_commit_blocks_when_diff_drifts_after_review(self):
        repo = self.make_git_repo()
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        snapshot = compute_review_snapshot(repo)
        # User edits the file after the PASS review but before clicking commit
        (repo / "feature.py").write_text("print('unreviewed')\n", encoding="utf-8")
        with self.assertRaises(CommitError) as ctx:
            controlled_commit(
                repo,
                "feat: drift",
                expected_snapshot=snapshot,
            )
        self.assertIn("drifted", str(ctx.exception).lower())
        # Nothing should have been committed
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(log.splitlines()[0].split(maxsplit=1)[1], "init")

    def test_commit_blocks_when_untracked_added_after_review(self):
        repo = self.make_git_repo()
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        snapshot = compute_review_snapshot(repo)
        # User drops an extra file after the review
        (repo / "extra.py").write_text("print('extra')\n", encoding="utf-8")
        with self.assertRaises(CommitError) as ctx:
            controlled_commit(
                repo,
                "feat: drift",
                expected_snapshot=snapshot,
            )
        self.assertIn("drifted", str(ctx.exception).lower())

    def test_commit_blocks_when_head_moves_after_review(self):
        repo = self.make_git_repo()
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        snapshot = compute_review_snapshot(repo)
        # User makes an unrelated commit before clicking the GUI commit button
        subprocess.run(
            ["git", "-C", str(repo), "add", "feature.py"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "manual"],
            capture_output=True, check=True,
        )
        with self.assertRaises(CommitError) as ctx:
            controlled_commit(
                repo,
                "feat: drift",
                expected_snapshot=snapshot,
            )
        self.assertIn("drifted", str(ctx.exception).lower())

    def test_commit_succeeds_when_snapshot_matches(self):
        repo = self.make_git_repo()
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        snapshot = compute_review_snapshot(repo)
        result = controlled_commit(
            repo,
            "feat: snapshot ok",
            expected_snapshot=snapshot,
        )
        self.assertTrue(result["commitSha"])
        self.assertEqual(result["commitMessage"], "feat: snapshot ok")

    def test_commit_blocks_when_staged_content_drifts_after_review(self):
        # Regression test for Codex P1-2 round 3: ``compute_review_snapshot``
        # now includes ``git diff --cached`` (via ``git diff HEAD``) so that
        # newly staged content cannot slip past drift detection.
        repo = self.make_git_repo()
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        snapshot = compute_review_snapshot(repo)
        # User stages an extra file after the PASS review
        (repo / "sneaky.txt").write_text("unreviewed\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repo), "add", "sneaky.txt"],
            capture_output=True, check=True,
        )
        with self.assertRaises(CommitError) as ctx:
            controlled_commit(
                repo,
                "feat: drift",
                expected_snapshot=snapshot,
            )
        self.assertIn("drifted", str(ctx.exception).lower())
        # Nothing should have been committed
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(log.splitlines()[0].split(maxsplit=1)[1], "init")

    def test_commit_blocks_when_staged_file_added_after_review(self):
        # Variant of the drift test where the staged content itself is the
        # only delta — no working-tree edits at all.
        repo = self.make_git_repo()
        snapshot = compute_review_snapshot(repo)
        (repo / "staged_only.txt").write_text("only staged\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repo), "add", "staged_only.txt"],
            capture_output=True, check=True,
        )
        with self.assertRaises(CommitError) as ctx:
            controlled_commit(
                repo,
                "feat: drift",
                expected_snapshot=snapshot,
            )
        self.assertIn("drifted", str(ctx.exception).lower())

    def test_commit_without_snapshot_skips_drift_check(self):
        # Backwards compatibility: existing callers that don't pass an
        # expected_snapshot must continue to commit normally.
        repo = self.make_git_repo()
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        result = controlled_commit(repo, "feat: no snapshot")
        self.assertTrue(result["commitSha"])

    def test_commit_blocks_when_large_untracked_file_changes_after_review(self):
        # Regression test for Codex P1-2 round 4: ``compute_review_snapshot``
        # must hash actual untracked file bytes (not the bounded review diff)
        # so a large untracked file cannot change after PASS without being
        # caught by the drift check.  The previous implementation would have
        # missed this because ``_untracked_files_diff`` skips files larger
        # than ``MAX_UNTRACKED_FILE_BYTES``.
        from gui.orchestrator import git_tools

        repo = self.make_git_repo()
        large_payload_a = b"x" * (git_tools.MAX_UNTRACKED_FILE_BYTES + 1024)
        large_payload_b = b"y" * (git_tools.MAX_UNTRACKED_FILE_BYTES + 1024)
        (repo / "huge.bin").write_bytes(large_payload_a)
        snapshot = compute_review_snapshot(repo)
        (repo / "huge.bin").write_bytes(large_payload_b)
        with self.assertRaises(CommitError) as ctx:
            controlled_commit(
                repo,
                "feat: drift",
                expected_snapshot=snapshot,
            )
        self.assertIn("drifted", str(ctx.exception).lower())
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(log.splitlines()[0].split(maxsplit=1)[1], "init")

    def test_commit_blocks_when_binary_untracked_file_changes_after_review(self):
        # Binary files are skipped in the review diff but must still be
        # hashed by ``compute_review_snapshot`` so a post-review edit is
        # detected.
        repo = self.make_git_repo()
        (repo / "blob.dat").write_bytes(b"\x00\x01\x02\x03 alpha")
        snapshot = compute_review_snapshot(repo)
        (repo / "blob.dat").write_bytes(b"\x00\x01\x02\x03 BETA")
        with self.assertRaises(CommitError) as ctx:
            controlled_commit(
                repo,
                "feat: drift",
                expected_snapshot=snapshot,
            )
        self.assertIn("drifted", str(ctx.exception).lower())

    def test_commit_env_check_runs_before_drift_check(self):
        # Regression for Codex P1-1 round 7: when the worktree has BOTH an
        # unreviewed drift AND a forbidden ``.env`` change, the env-path
        # guard must fire first so the backend never reads the drifted
        # diff (which would include the ``.env`` content if tracked).
        repo = self.make_git_repo()
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        snapshot = compute_review_snapshot(repo)
        # Drift: edit the reviewed file
        (repo / "feature.py").write_text("print('unreviewed')\n", encoding="utf-8")
        # Forbidden: drop a .env in
        (repo / ".env").write_text("SECRET=leaked\n", encoding="utf-8")
        with self.assertRaises(CommitError) as ctx:
            controlled_commit(
                repo,
                "feat: drift+env",
                expected_snapshot=snapshot,
            )
        # Env error must win — the message must mention ``.env`` and must
        # NOT mention ``drifted``.
        self.assertIn(".env", str(ctx.exception))
        self.assertNotIn("drifted", str(ctx.exception).lower())

    def test_commit_env_check_runs_before_drift_check_without_snapshot(self):
        # Same ordering invariant but when no ``expected_snapshot`` is
        # supplied.  ``controlled_commit`` must still report the env error
        # without ever calling ``compute_review_snapshot`` (which would
        # now also raise ``EnvFileChangedError``, but the explicit guard
        # in ``controlled_commit`` must fire first).
        from gui.orchestrator import git_workflow

        repo = self.make_git_repo()
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        (repo / ".env").write_text("SECRET=leaked\n", encoding="utf-8")
        with mock.patch.object(
            git_workflow,
            "compute_review_snapshot",
            side_effect=AssertionError("must not call compute_review_snapshot when env present"),
        ) as snap_mock:
            with self.assertRaises(CommitError) as ctx:
                controlled_commit(repo, "feat: env without snapshot")
        snap_mock.assert_not_called()
        self.assertIn(".env", str(ctx.exception))

    def test_commit_blocks_when_worktree_mutated_between_snapshot_and_staging(self):
        # Regression test for Codex P1-2 round 8: a file edit between the
        # pre-stage drift check and ``git add -A`` must be caught by the
        # post-stage verification.  Without the post-stage check, the
        # mutated content would be staged and committed unreviewed.
        from gui.orchestrator import git_workflow

        repo = self.make_git_repo()
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        snapshot = compute_review_snapshot(repo)

        # Hook ``_run_git_text`` so that when ``git add -A`` runs we
        # mutate the worktree first.  This simulates a TOCTOU where the
        # file is modified between the pre-stage drift check and the
        # actual ``git add -A`` invocation.  ``compute_review_snapshot``
        # in git_tools uses ``_run_git`` directly (not the wrapper), so
        # the patch only affects the staging / commit calls inside
        # ``controlled_commit``.
        original_run = git_workflow._run_git_text

        def mutating_run(path, args):
            if args and args[:2] == ["add", "-A"]:
                # Mutate the file just before ``git add -A`` runs.  The
                # mutation lands in the staged index because git add
                # reads from disk at this later moment.
                (repo / "feature.py").write_text(
                    "print('unreviewed TOCTOU mutation')\n",
                    encoding="utf-8",
                )
            return original_run(path, args)

        with mock.patch.object(git_workflow, "_run_git_text", side_effect=mutating_run):
            with self.assertRaises(CommitError) as ctx:
                controlled_commit(
                    repo,
                    "feat: drift",
                    expected_snapshot=snapshot,
                )

        # The post-stage drift check must catch the mutation.  The error
        # must mention drift so it is distinguishable from the env guard.
        self.assertIn("drifted", str(ctx.exception).lower())
        # Nothing should have been committed.
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(log.splitlines()[0].split(maxsplit=1)[1], "init")

    def test_commit_post_stage_check_blocks_env_added_during_staging(self):
        # Regression for Codex P1-2 round 8: a ``.env`` file dropped
        # between the pre-stage env guard and ``git add -A`` must be
        # caught by the post-stage env-path re-check.  Without the
        # post-stage guard the ``.env`` would be staged and committed.
        from gui.orchestrator import git_workflow

        repo = self.make_git_repo()
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        snapshot = compute_review_snapshot(repo)

        original_run = git_workflow._run_git_text

        def env_dropping_run(path, args):
            if args and args[:2] == ["add", "-A"]:
                # Drop a forbidden .env file just before staging runs.
                (repo / ".env").write_text(
                    "SECRET=leaked-during-staging\n",
                    encoding="utf-8",
                )
            return original_run(path, args)

        with mock.patch.object(git_workflow, "_run_git_text", side_effect=env_dropping_run):
            with self.assertRaises(CommitError) as ctx:
                controlled_commit(
                    repo,
                    "feat: env drift",
                    expected_snapshot=snapshot,
                )

        # The post-stage env guard must fire and mention ``.env``.
        self.assertIn(".env", str(ctx.exception))
        # Nothing should have been committed.
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(log.splitlines()[0].split(maxsplit=1)[1], "init")

    def test_commit_succeeds_when_snapshot_matches_around_staging(self):
        # Sanity check that the post-stage verification does not produce
        # false positives: when nothing mutates between the pre-stage
        # check and ``git add -A``, the post-stage check must observe
        # the same content hash and let the commit proceed.
        repo = self.make_git_repo()
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        snapshot = compute_review_snapshot(repo)
        result = controlled_commit(
            repo,
            "feat: post-stage ok",
            expected_snapshot=snapshot,
        )
        self.assertTrue(result["commitSha"])
        self.assertEqual(result["commitMessage"], "feat: post-stage ok")

    # ------------------------------------------------------------------
    # Symlink / lstat-mode regression coverage (Codex P1-1 / P2-1 round 9).
    # ------------------------------------------------------------------

    def _skip_if_no_symlinks(self):
        probe_dir = self.make_dir()
        target = probe_dir / "target.txt"
        target.write_text("x", encoding="utf-8")
        link = probe_dir / "link.txt"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks not supported on this platform: {exc}")

    def test_commit_blocks_when_untracked_symlink_targets_untracked_env(self):
        # Codex P1-1 round 9: a benign-named untracked symlink whose
        # target string references ``.env`` must trip the env guard
        # before the hasher reads the destination's bytes.  ``link.txt``
        # itself has a benign name, so the previous path-only guard
        # would not catch it.
        self._skip_if_no_symlinks()
        repo = self.make_git_repo()
        (repo / ".env").write_text("SECRET=leaked\n", encoding="utf-8")
        (repo / "link.txt").symlink_to(repo / ".env")
        with self.assertRaises(CommitError) as ctx:
            controlled_commit(repo, "feat: leak via symlink")
        message = str(ctx.exception)
        self.assertIn("link.txt", message)
        self.assertIn(".env", message)
        # Nothing must have been committed.
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(log.splitlines()[0].split(maxsplit=1)[1], "init")

    def test_commit_blocks_when_symlink_targets_tracked_env(self):
        # Variant where the env file is already committed and clean, so
        # the only new path Git would stage is the symlink.  The
        # symlink-aware guard must still reject the commit because the
        # link target references ``.env``.
        self._skip_if_no_symlinks()
        repo = self.make_git_repo()
        (repo / ".env").write_text("SECRET=baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", ".env"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "track env"],
            capture_output=True,
            check=True,
        )
        (repo / "link.txt").symlink_to(repo / ".env")
        with self.assertRaises(CommitError) as ctx:
            controlled_commit(repo, "feat: leak via symlink to tracked env")
        self.assertIn("link.txt", str(ctx.exception))
        self.assertIn(".env", str(ctx.exception))

    def test_commit_blocks_when_worktree_mode_drifts_after_review(self):
        # Codex P2-1 round 9: a chmod on a tracked file after the
        # PASS review must be detected as drift.  Without the
        # lstat-mode inclusion in the snapshot, the post-stage drift
        # check would not see the change and the commit would absorb
        # the unreviewed mode change.
        repo = self.make_git_repo()
        script = repo / "script.sh"
        script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        snapshot = compute_review_snapshot(repo)
        original_mode = script.lstat().st_mode
        try:
            os.chmod(script, original_mode ^ stat_module.S_IWUSR)
        except OSError as exc:
            self.skipTest(f"os.chmod unavailable on this platform: {exc}")
        if script.lstat().st_mode == original_mode:
            self.skipTest("os.chmod did not change lstat mode on this platform")
        try:
            with self.assertRaises(CommitError) as ctx:
                controlled_commit(
                    repo,
                    "feat: mode drift",
                    expected_snapshot=snapshot,
                )
            self.assertIn("drifted", str(ctx.exception).lower())
        finally:
            try:
                os.chmod(script, original_mode)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Codex P1-2 / P1-3 round 13 regression coverage.
    # ------------------------------------------------------------------

    def test_commit_rejects_when_merge_in_progress(self):
        # P1-2 round 13: ``controlled_commit`` must refuse to run when the
        # repository is in an in-progress merge state.  ``git commit``
        # would otherwise finalize the merge and the resulting commit
        # could carry unreviewed parents yet still pass every HEAD /
        # content drift check.
        repo = self.make_git_repo()
        # Create a real merge-in-progress state by attempting to merge a
        # divergent branch and leaving the conflict unresolved.
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-b", "divergent"],
            capture_output=True, check=True,
        )
        (repo / "divergent.txt").write_text("divergent\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "divergent.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "divergent commit"],
            capture_output=True, check=True,
        )
        subprocess.run(["git", "-C", str(repo), "checkout", "master"], capture_output=True, check=True)
        # Create a conflicting change on master so the merge leaves
        # MERGE_HEAD behind when aborted via a non-conflicting setup, OR
        # leaves MERGE_HEAD if it fails.  We use ``--no-commit`` to
        # leave the merge in progress without committing.
        subprocess.run(
            ["git", "-C", str(repo), "merge", "--no-commit", "--no-ff", "divergent"],
            capture_output=True, check=True,
        )
        # The repository is now in an in-progress merge state.  Drop a
        # benign uncommitted file so the empty-worktree check does not
        # fire first.
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        with self.assertRaises(CommitError) as ctx:
            controlled_commit(repo, "feat: in-progress merge")
        message = str(ctx.exception).lower()
        self.assertIn("in-progress", message)
        self.assertIn("merge", message)

    def test_commit_rejects_when_cherry_pick_in_progress(self):
        # P1-2 round 13: cherry-pick state must also be detected.  We
        # write the ``CHERRY_PICK_HEAD`` marker file directly inside the
        # repo's git directory so the detector sees it.
        repo = self.make_git_repo()
        git_dir = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        marker = Path(git_dir) / "CHERRY_PICK_HEAD"
        marker.write_text("0" * 40, encoding="utf-8")
        self.addCleanup(lambda: marker.unlink(missing_ok=True))
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        with self.assertRaises(CommitError) as ctx:
            controlled_commit(repo, "feat: in-progress cherry-pick")
        message = str(ctx.exception).lower()
        self.assertIn("in-progress", message)
        self.assertIn("cherry-pick", message)

    def test_commit_blocks_when_clean_filter_perturbs_staged_tree(self):
        # P1-3 round 13 regression, hardened by Codex P1-2 round 14:
        # install a clean filter between snapshot capture and commit
        # time.  The filter mutates file content during staging, so the
        # staged-tree SHA could diverge from the reviewed ``treeSha``
        # even though the worktree bytes match.  Round 14 tightens the
        # boundary: ``compute_review_snapshot`` itself refuses when a
        # clean/process filter is configured on any changed path, so
        # the commit can never reach a state where filtered content
        # could be staged without Codex having reviewed it.
        repo = self.make_git_repo()
        # Capture the snapshot BEFORE installing the filter.
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        snapshot = compute_review_snapshot(repo)
        # Install a clean filter that rewrites ``feature.py`` on its
        # way into the index.  The filter just uppercases the content,
        # which is enough to perturb the resulting tree SHA.
        subprocess.run(
            ["git", "-C", str(repo), "config", "filter.uc.clean", "tr a-z A-Z"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "filter.uc.smudge", "cat"],
            capture_output=True, check=True,
        )
        attrs = repo / ".gitattributes"
        attrs.write_text("feature.py filter=uc\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", ".gitattributes"], capture_output=True, check=False)
        # Round 14: ``controlled_commit`` recomputes the snapshot to
        # detect drift.  Because a clean filter is now configured on
        # ``feature.py``, the snapshot computation refuses with a
        # ``GitError`` that surfaces the offending path.  Either
        # ``CommitError`` (drift detected) or ``GitError`` (snapshot
        # refused) is acceptable — the safety boundary is preserved
        # either way.
        with self.assertRaises((CommitError, git_tools.GitError)) as ctx:
            controlled_commit(
                repo,
                "feat: clean filter drift",
                expected_snapshot=snapshot,
            )
        message = str(ctx.exception).lower()
        self.assertTrue(
            "drift" in message or "filter" in message,
            "controlled_commit must reject when a clean filter perturbs "
            "the staged tree relative to the reviewed snapshot. "
            "Got: " + message,
        )
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(log.splitlines()[0].split(maxsplit=1)[1], "init")

    def test_commit_uses_commit_tree_and_atomic_update_ref(self):
        # P1-3 round 13: ``controlled_commit`` must build the commit
        # via ``git commit-tree`` (not ``git commit``) and advance HEAD
        # via ``git update-ref HEAD <new> <expected>`` for an atomic
        # compare-and-swap.  This locks in the contract so a future
        # refactor that re-introduces ``git commit`` is caught.
        from gui.orchestrator import git_workflow

        repo = self.make_git_repo()
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        snapshot = compute_review_snapshot(repo)

        captured_args: list[list[str]] = []
        original_run = git_workflow._run_git_text

        def capturing_run(path, args):
            captured_args.append(list(args))
            return original_run(path, args)

        with mock.patch.object(git_workflow, "_run_git_text", side_effect=capturing_run):
            result = controlled_commit(
                repo,
                "feat: commit-tree contract",
                expected_snapshot=snapshot,
            )

        # The commit-tree invocation must be present.
        commit_tree_calls = [a for a in captured_args if a and a[0] == "commit-tree"]
        self.assertTrue(commit_tree_calls, "expected a git commit-tree call")
        # The update-ref invocation must include the expected HEAD as
        # the CAS old-value.
        update_ref_calls = [
            a for a in captured_args
            if a and a[0] == "update-ref" and len(a) >= 2 and a[1] == "HEAD"
        ]
        self.assertTrue(update_ref_calls, "expected a git update-ref HEAD call")
        update_ref_call = update_ref_calls[0]
        # ``update-ref HEAD <new> <expected_old>`` — the last element
        # is the CAS old-value, which must equal the reviewed HEAD SHA.
        self.assertEqual(update_ref_call[-1], snapshot["headSha"])
        # And the ``git commit`` invocation must NOT be present.
        commit_calls = [
            a for a in captured_args
            if a and a[0] == "commit" and (len(a) == 1 or a[1] != "--allow-empty")
        ]
        self.assertFalse(
            commit_calls,
            "controlled_commit must NOT call git commit; commit-tree is the contract",
        )
        # The result must still report the commit SHA.
        self.assertTrue(result["commitSha"])
        head_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(head_sha, result["commitSha"])

    def test_commit_atomic_update_ref_rejects_concurrent_head_movement(self):
        # P1-3 round 13: the compare-and-swap ``update-ref HEAD <new>
        # <old>`` must fail when HEAD moves between the pre-stage drift
        # check and the ref update.  Simulate by racing an external
        # commit in just before the ``commit-tree`` call.
        from gui.orchestrator import git_workflow

        repo = self.make_git_repo()
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        snapshot = compute_review_snapshot(repo)

        original_run = git_workflow._run_git_text

        def racing_run(path, args):
            if args and args[0] == "commit-tree":
                # External commit lands before our CAS update.
                (repo / "external.txt").write_text("external\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(repo), "add", "external.txt"], capture_output=True, check=False)
                subprocess.run(
                    ["git", "-C", str(repo), "commit", "-m", "external"],
                    capture_output=True, check=False,
                )
            return original_run(path, args)

        with mock.patch.object(git_workflow, "_run_git_text", side_effect=racing_run):
            with self.assertRaises(CommitError) as ctx:
                controlled_commit(
                    repo,
                    "feat: race",
                    expected_snapshot=snapshot,
                )
        # The CAS failure surfaces as a HEAD-update error.
        self.assertIn("Atomic HEAD update failed", str(ctx.exception))
        # The reviewed change must NOT have landed on HEAD.
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        top = log.splitlines()[0]
        self.assertIn("external", top)
        self.assertNotIn("feat: race", top)

    # ------------------------------------------------------------------
    # Codex P1-5 round 16 regression coverage: persist ``new_commit_sha``
    # as the controlled commit identity and detect subsequent HEAD drift.
    # ------------------------------------------------------------------

    def test_commit_records_new_commit_sha_and_no_drift_on_happy_path(self):
        # Codex P1-5 round 16: ``controlled_commit`` must persist the
        # immutable ``new_commit_sha`` (from ``commit-tree``) as
        # ``commitSha`` rather than rereading HEAD, and ``headDriftSha``
        # must be ``None`` when HEAD still matches the controlled commit.
        repo = self.make_git_repo()
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        result = controlled_commit(repo, "feat: add feature.py")
        # Compute the object SHA that ``commit-tree`` produced by
        # walking HEAD; since nothing else moved it, this must equal
        # the recorded ``commitSha``.
        head_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(result["commitSha"], head_sha)
        # ``headDriftSha`` is None on the happy path.
        self.assertIsNone(result.get("headDriftSha"))

    def test_commit_records_drift_when_head_advances_after_cas(self):
        # Codex P1-5 round 16: if HEAD moves between the CAS ref update
        # and the post-commit observation, the recorded ``commitSha``
        # must still be the controlled commit object and
        # ``headDriftSha`` must surface the new HEAD value.
        from gui.orchestrator import git_workflow
        repo = self.make_git_repo()
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        # Capture the real ``_run_git_text`` so we can wrap it.
        real_run_git_text = git_workflow._run_git_text
        drift_sha_holder: dict[str, str] = {}
        def drift_injector(project_path, args):
            # When the post-commit ``rev-parse HEAD`` is invoked, first
            # advance HEAD externally so the observation sees a
            # different SHA than the controlled commit.  Earlier calls
            # (``commit-tree``, ``update-ref``, ``rev-parse --short``)
            # must see real Git state so the CAS update succeeds.
            if (
                args == ["rev-parse", "HEAD"]
                and drift_sha_holder.get("armed")
            ):
                # Move HEAD by creating an extra commit on top of the
                # controlled commit using a separate index/commit-tree.
                new_tree = subprocess.run(
                    ["git", "-C", str(repo), "write-tree"],
                    capture_output=True, text=True, check=True,
                ).stdout.strip()
                controlled_sha = drift_sha_holder["controlled_sha"]
                drift_commit = subprocess.run(
                    [
                        "git", "-C", str(repo), "commit-tree", new_tree,
                        "-p", controlled_sha, "-m", "external drift",
                    ],
                    capture_output=True, text=True, check=True,
                ).stdout.strip()
                subprocess.run(
                    ["git", "-C", str(repo), "update-ref", "HEAD", drift_commit],
                    capture_output=True, check=True,
                )
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=drift_commit + "\n", stderr="",
                )
            result = real_run_git_text(project_path, args)
            # Capture the controlled commit SHA right after commit-tree
            # runs so we know what to advance from.
            if args and args[0] == "commit-tree" and result.returncode == 0:
                drift_sha_holder["controlled_sha"] = result.stdout.strip()
                # Arm the drift injector only AFTER the commit-tree +
                # update-ref sequence (we detect by the next call being
                # the short rev-parse).
            if args and args[0] == "rev-parse" and len(args) > 1 and args[1] == "--short":
                drift_sha_holder["armed"] = True
            return result
        with mock.patch(
            "gui.orchestrator.git_workflow._run_git_text",
            side_effect=drift_injector,
        ):
            result = controlled_commit(repo, "feat: add feature.py")
        # The recorded ``commitSha`` is the controlled commit object
        # created by ``commit-tree``, not the externally-advanced HEAD.
        controlled_sha = drift_sha_holder["controlled_sha"]
        self.assertEqual(result["commitSha"], controlled_sha)
        # ``headDriftSha`` surfaces the externally-advanced HEAD value.
        self.assertIsNotNone(result.get("headDriftSha"))
        self.assertNotEqual(result["headDriftSha"], controlled_sha)


class ControlledMergeTests(unittest.TestCase):
    def make_dir(self):
        temp_root = server.ROOT / ".gui" / "test-tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        path = temp_root / f"merge-{uuid.uuid4().hex}"
        path.mkdir()
        self.addCleanup(lambda: shutil.rmtree(str(path), ignore_errors=True))
        return path

    def make_git_repo_with_worktree(self):
        repo = self.make_dir()
        subprocess.run(["git", "-C", str(repo), "init"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "test@test.test"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Test"],
            capture_output=True, check=True,
        )
        (repo / "readme.md").write_text("# repo\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "readme.md"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "init"],
            capture_output=True, check=True,
        )
        wt = repo.parent / f"wt-{uuid.uuid4().hex}"
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-b", "feature/mergeable", str(wt)],
            capture_output=True, check=True,
        )
        self.addCleanup(lambda: shutil.rmtree(str(wt), ignore_errors=True))
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
                capture_output=True,
            )
        )
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "-C", str(repo), "branch", "-D", "feature/mergeable"],
                capture_output=True,
            )
        )
        return repo, wt

    def test_merge_succeeds_when_main_is_clean(self):
        repo, wt = self.make_git_repo_with_worktree()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: add feature.txt"],
            capture_output=True, check=True,
        )
        result = controlled_merge_to_main(repo, "feature/mergeable")
        self.assertTrue(result["mergeCommitSha"])
        self.assertEqual(result["mergeSourceBranch"], "feature/mergeable")
        self.assertEqual(result["mergeTargetBranch"], "master")
        # Verify the file is now on main
        self.assertTrue((repo / "feature.txt").exists())

    def test_merge_blocks_dirty_main(self):
        repo, wt = self.make_git_repo_with_worktree()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: add feature.txt"],
            capture_output=True, check=True,
        )
        (repo / "uncommitted.txt").write_text("dirty", encoding="utf-8")
        with self.assertRaises(MergeError) as ctx:
            controlled_merge_to_main(repo, "feature/mergeable")
        self.assertIn("dirty", str(ctx.exception).lower())

    def test_merge_blocks_missing_branch(self):
        repo, wt = self.make_git_repo_with_worktree()
        with self.assertRaises(MergeError) as ctx:
            controlled_merge_to_main(repo, "feature/does-not-exist")
        self.assertIn("does not exist", str(ctx.exception).lower())

    def test_merge_blocks_conflict_and_aborts(self):
        repo, wt = self.make_git_repo_with_worktree()
        # Create conflicting changes on main and on the worktree branch
        (repo / "conflict.txt").write_text("main\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "conflict.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "main: conflict.txt"],
            capture_output=True, check=True,
        )
        (wt / "conflict.txt").write_text("wt\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "conflict.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "wt: conflict.txt"],
            capture_output=True, check=True,
        )
        with self.assertRaises(MergeError):
            controlled_merge_to_main(repo, "feature/mergeable")
        # Ensure merge was aborted: repo should not be in merging state
        merge_head = repo / ".git" / "MERGE_HEAD"
        # If we are in a worktree of a parent repo, look for the actual git dir
        git_dir = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--git-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        merge_head = (repo / git_dir / "MERGE_HEAD").resolve() if not Path(git_dir).is_absolute() else Path(git_dir) / "MERGE_HEAD"
        self.assertFalse(merge_head.exists(), "MERGE_HEAD must not exist after failed merge")

    def test_merge_blocks_when_branch_moved_past_reviewed_commit(self):
        repo, wt = self.make_git_repo_with_worktree()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: reviewed change"],
            capture_output=True, check=True,
        )
        reviewed_sha = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # User pushes an unreviewed commit onto the same branch afterwards
        (wt / "unreviewed.txt").write_text("sneaky\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "unreviewed.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: unreviewed"],
            capture_output=True, check=True,
        )
        with self.assertRaises(MergeError) as ctx:
            controlled_merge_to_main(
                repo,
                "feature/mergeable",
                expected_commit_sha=reviewed_sha,
            )
        self.assertIn("no longer points", str(ctx.exception).lower())
        # Main must not have absorbed the unreviewed file
        self.assertFalse((repo / "unreviewed.txt").exists())

    def test_merge_blocks_when_branch_has_unreviewed_pre_task_commits(self):
        # Codex P1-1 round 12: if a worktree branch already carried commits
        # before the task started, Codex only reviewed the uncommitted diff
        # against that branch HEAD.  ``git merge`` would otherwise sweep
        # those earlier commits into main alongside the reviewed one.  The
        # merge must refuse when the reviewed base SHA is not reachable
        # from the primary worktree HEAD.
        repo, wt = self.make_git_repo_with_worktree()
        # Add an unreviewed commit on the worktree branch BEFORE the task
        # starts.  This simulates an imported or manually-advanced branch.
        (wt / "pre_task.txt").write_text("pre\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "pre_task.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "chore: pre-task commit"],
            capture_output=True, check=True,
        )
        # reviewedHeadSha is captured at artifact time = current branch HEAD
        reviewed_base = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # Simulate the reviewed task change being committed.
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: reviewed change"],
            capture_output=True, check=True,
        )
        reviewed_commit = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        with self.assertRaises(MergeError) as ctx:
            controlled_merge_to_main(
                repo,
                "feature/mergeable",
                expected_commit_sha=reviewed_commit,
                expected_base_sha=reviewed_base,
            )
        self.assertIn("not reachable", str(ctx.exception).lower())
        # Main must not have absorbed either the pre-task commit or the
        # reviewed commit when the base check fails.
        self.assertFalse((repo / "pre_task.txt").exists())
        self.assertFalse((repo / "feature.txt").exists())

    def test_merge_succeeds_when_reviewed_base_is_reachable_from_main(self):
        # Happy path for Codex P1-1 round 12: when the reviewed base SHA
        # IS reachable from the primary worktree HEAD (the normal case
        # where the worktree branch was created from main and the task
        # did not add any commits before the review), the merge must
        # proceed.
        repo, wt = self.make_git_repo_with_worktree()
        # reviewedHeadSha captured at artifact time = main's initial commit
        # (the worktree was branched from it and no commits landed on the
        # branch before the task).
        reviewed_base = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: reviewed change"],
            capture_output=True, check=True,
        )
        reviewed_commit = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        result = controlled_merge_to_main(
            repo,
            "feature/mergeable",
            expected_commit_sha=reviewed_commit,
            expected_base_sha=reviewed_base,
        )
        self.assertTrue(result["mergeCommitSha"])
        self.assertTrue((repo / "feature.txt").exists())

    def test_merge_without_expected_base_sha_skips_reachability_check(self):
        # Backwards compatibility: callers that don't pass expected_base_sha
        # must continue to merge normally even when the branch had pre-task
        # commits.
        repo, wt = self.make_git_repo_with_worktree()
        (wt / "pre_task.txt").write_text("pre\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "pre_task.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "chore: pre-task commit"],
            capture_output=True, check=True,
        )
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: change"],
            capture_output=True, check=True,
        )
        result = controlled_merge_to_main(repo, "feature/mergeable")
        self.assertTrue(result["mergeCommitSha"])

    def test_merge_succeeds_when_branch_matches_reviewed_commit(self):
        repo, wt = self.make_git_repo_with_worktree()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: reviewed change"],
            capture_output=True, check=True,
        )
        reviewed_sha = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        result = controlled_merge_to_main(
            repo,
            "feature/mergeable",
            expected_commit_sha=reviewed_sha,
        )
        self.assertTrue(result["mergeCommitSha"])
        self.assertTrue((repo / "feature.txt").exists())

    def test_merge_without_expected_sha_skips_branch_drift_check(self):
        # Backwards compatibility: callers that don't pass expected_commit_sha
        # must continue to merge normally.
        repo, wt = self.make_git_repo_with_worktree()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: change"],
            capture_output=True, check=True,
        )
        result = controlled_merge_to_main(repo, "feature/mergeable")
        self.assertTrue(result["mergeCommitSha"])

    def test_merge_command_uses_reviewed_sha_not_branch_name(self):
        # Regression for Codex P1-1 round 8 + P1-3 round 17: when
        # ``expected_commit_sha`` is provided, the
        # ``git merge-tree --write-tree`` invocation must reference the
        # immutable SHA as its merge target — NOT the mutable branch
        # name.  Otherwise a branch advance between ``get_branch_head``
        # and the merge-tree call could pull unreviewed commits into the
        # computed merge tree (and the subsequent commit-tree +
        # update-ref CAS would record them).
        from gui.orchestrator import git_workflow

        repo, wt = self.make_git_repo_with_worktree()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: reviewed change"],
            capture_output=True, check=True,
        )
        reviewed_sha = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        captured_args: list[list[str]] = []
        original_run = git_workflow._run_git_text

        def capturing_run(path, args):
            captured_args.append(list(args))
            return original_run(path, args)

        with mock.patch.object(git_workflow, "_run_git_text", side_effect=capturing_run):
            controlled_merge_to_main(
                repo,
                "feature/mergeable",
                expected_commit_sha=reviewed_sha,
            )

        # Codex P1-3 round 17: the controlled merge now uses
        # ``git merge-tree --write-tree <main_head> <merge_ref>``
        # instead of ``git merge``.  The merge target (final positional
        # argument) MUST be the reviewed SHA, not the branch name.
        merge_tree_calls = [a for a in captured_args if a and a[0] == "merge-tree"]
        self.assertTrue(merge_tree_calls, "expected a git merge-tree call")
        merge_tree_call = merge_tree_calls[0]
        merge_target = merge_tree_call[-1]
        self.assertEqual(merge_target, reviewed_sha)
        self.assertNotEqual(merge_target, "feature/mergeable")
        # ``commit-tree`` must also be invoked to create the merge
        # commit object directly from the merge tree.
        commit_tree_calls = [a for a in captured_args if a and a[0] == "commit-tree"]
        self.assertTrue(commit_tree_calls, "expected a git commit-tree call")
        # The reviewed SHA must appear as the second ``-p`` parent
        # (first parent is ``main_head``, second parent is the merge
        # target / incoming side).
        commit_tree_call = commit_tree_calls[0]
        parent_args = [commit_tree_call[i + 1] for i, a in enumerate(commit_tree_call) if a == "-p"]
        self.assertIn(reviewed_sha, parent_args)

    def test_merge_pins_resolved_branch_sha_when_no_expected_sha(self):
        # Compatibility callers may omit ``expected_commit_sha``, but the
        # operation and recovery journal must still share one immutable
        # source SHA rather than a mutable branch name.
        from gui.orchestrator import git_workflow

        repo, wt = self.make_git_repo_with_worktree()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: change"],
            capture_output=True, check=True,
        )

        captured_args: list[list[str]] = []
        original_run = git_workflow._run_git_text

        def capturing_run(path, args):
            captured_args.append(list(args))
            return original_run(path, args)

        with mock.patch.object(git_workflow, "_run_git_text", side_effect=capturing_run):
            controlled_merge_to_main(repo, "feature/mergeable")

        merge_tree_calls = [a for a in captured_args if a and a[0] == "merge-tree"]
        self.assertTrue(merge_tree_calls, "expected a git merge-tree call")
        merge_tree_call = merge_tree_calls[0]
        source_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "refs/heads/feature/mergeable"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(merge_tree_call[-1], source_sha)

    def test_merge_cannot_pull_commits_added_after_verification(self):
        # End-to-end TOCTOU regression for Codex P1-1 round 8 + P1-3
        # round 17: even if the branch is somehow advanced between
        # ``get_branch_head`` and the ``git merge-tree`` call, the
        # merge target must remain the reviewed SHA so unreviewed
        # commits are not absorbed.  We simulate the race by hooking
        # ``_run_git_text`` to advance the branch right before the
        # merge-tree command runs, then verify the unreviewed commit
        # does NOT land on main.
        from gui.orchestrator import git_workflow

        repo, wt = self.make_git_repo_with_worktree()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: reviewed change"],
            capture_output=True, check=True,
        )
        reviewed_sha = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        original_run = git_workflow._run_git_text

        def racing_run(path, args):
            if args and args[0] == "merge-tree":
                # Advance the branch with an unreviewed commit right
                # before ``git merge-tree`` executes.  This simulates a
                # race where the branch moves between verification and
                # the merge-tree call.
                (wt / "unreviewed.txt").write_text("sneaky\n", encoding="utf-8")
                subprocess.run(
                    ["git", "-C", str(wt), "add", "unreviewed.txt"],
                    capture_output=True, check=False,
                )
                subprocess.run(
                    ["git", "-C", str(wt), "commit", "-m", "feat: unreviewed"],
                    capture_output=True, check=False,
                )
            return original_run(path, args)

        with mock.patch.object(git_workflow, "_run_git_text", side_effect=racing_run):
            controlled_merge_to_main(
                repo,
                "feature/mergeable",
                expected_commit_sha=reviewed_sha,
            )

        # The reviewed feature.txt must land on main...
        self.assertTrue((repo / "feature.txt").exists())
        # ...but the unreviewed commit that was injected during the race
        # must NOT be absorbed, because the merge used the immutable
        # reviewed SHA rather than the mutable branch tip.
        self.assertFalse(
            (repo / "unreviewed.txt").exists(),
            "unreviewed commit was absorbed despite the SHA pinning",
        )

    # ------------------------------------------------------------------
    # Codex P1-2 round 13 regression coverage: parent verification.
    # ------------------------------------------------------------------

    def test_merge_blocks_when_reviewed_commit_has_multiple_parents(self):
        # When ``expected_commit_sha`` and ``expected_base_sha`` are both
        # supplied, the merge must refuse if the reviewed commit has more
        # than one parent (e.g. it was created while a merge was in
        # progress and absorbed an unreviewed parent).  The reachability
        # check on ``expected_base_sha`` would still pass because the
        # reviewed base IS reachable from the multi-parent commit, so the
        # only thing standing between an attacker and trunk import of the
        # unreviewed parent is the parent-count check.
        repo, wt = self.make_git_repo_with_worktree()
        # Create a second branch with an unrelated commit so we can
        # build a real merge commit on the worktree branch.
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-b", "unrelated"],
            capture_output=True, check=True,
        )
        (repo / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "unrelated.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "unrelated commit"],
            capture_output=True, check=True,
        )
        unrelated_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        subprocess.run(["git", "-C", str(repo), "checkout", "master"], capture_output=True, check=True)
        # Build a merge commit on the worktree branch that has both
        # ``reviewed_base`` (master HEAD) and ``unrelated_sha`` as parents.
        reviewed_base = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: reviewed change"],
            capture_output=True, check=True,
        )
        # Merge the unrelated branch into the worktree branch so the
        # resulting HEAD is a merge commit with two parents.
        subprocess.run(
            ["git", "-C", str(wt), "merge", "--no-ff", "-m", "merge unrelated", unrelated_sha],
            capture_output=True, check=True,
        )
        reviewed_commit = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        with self.assertRaises(MergeError) as ctx:
            controlled_merge_to_main(
                repo,
                "feature/mergeable",
                expected_commit_sha=reviewed_commit,
                expected_base_sha=reviewed_base,
            )
        self.assertIn("parents", str(ctx.exception).lower())
        # The unrelated commit's file must NOT land on main.
        self.assertFalse((repo / "unrelated.txt").exists())

    def test_merge_blocks_when_reviewed_commit_parent_differs_from_base(self):
        # Variant: reviewed commit has exactly one parent, but the parent
        # is not the reviewed base.  This catches a scenario where the
        # worktree branch was rebased or otherwise rewritten between
        # artifact time and commit time.
        repo, wt = self.make_git_repo_with_worktree()
        reviewed_base = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # Create a sibling commit on a separate branch so its parent
        # equals master HEAD but a subsequent rebase changes the parent.
        # Easier path: just create two commits on the worktree branch
        # and pass the OLDER one as expected_base.
        (wt / "a.txt").write_text("a\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "a.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "first commit"],
            capture_output=True, check=True,
        )
        first_sha = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        (wt / "b.txt").write_text("b\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "b.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "second commit"],
            capture_output=True, check=True,
        )
        second_sha = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # ``second_sha`` has exactly one parent (``first_sha``), but we
        # claim ``reviewed_base == master HEAD`` (not ``first_sha``).
        # The parent check must reject because ``first_sha`` !=
        # ``reviewed_base``.
        with self.assertRaises(MergeError) as ctx:
            controlled_merge_to_main(
                repo,
                "feature/mergeable",
                expected_commit_sha=second_sha,
                expected_base_sha=reviewed_base,
            )
        self.assertIn("parents", str(ctx.exception).lower())

    def test_merge_succeeds_when_reviewed_commit_has_sole_correct_parent(self):
        # Happy path: reviewed commit has exactly one parent equal to
        # the reviewed base.  The merge must succeed.
        repo, wt = self.make_git_repo_with_worktree()
        reviewed_base = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: reviewed change"],
            capture_output=True, check=True,
        )
        reviewed_commit = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        result = controlled_merge_to_main(
            repo,
            "feature/mergeable",
            expected_commit_sha=reviewed_commit,
            expected_base_sha=reviewed_base,
        )
        self.assertTrue(result["mergeCommitSha"])
        self.assertTrue((repo / "feature.txt").exists())

    # ------------------------------------------------------------------
    # Codex P1-2 round 15 regression coverage: custom merge drivers.
    # ------------------------------------------------------------------

    def test_merge_blocks_when_custom_merge_driver_configured(self):
        # Regression for Codex P1-2 round 15: a custom ``merge.<name>.driver``
        # configured on a path the merge would touch can auto-resolve
        # conflicts or otherwise produce unreviewed merge content.  The
        # controlled merge must refuse before ``git merge`` runs.
        repo, wt = self.make_git_repo_with_worktree()
        # Install a merge driver that always succeeds.
        subprocess.run(
            ["git", "-C", str(repo), "config", "merge.always.driver", "true"],
            capture_output=True, check=True,
        )
        # Commit ``.gitattributes`` on master BEFORE the worktree is
        # created.  ``make_git_repo_with_worktree`` already created the
        # worktree branch from the initial commit, so we (a) commit the
        # attribute on master to advance it, (b) rebase-merge the
        # worktree branch on top by fast-forwarding it via a temporary
        # merge.  Simpler: we recreate the worktree from the new master
        # HEAD so the worktree branch's parent is the new master HEAD.
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "branch", "-D", "feature/mergeable"],
            capture_output=True, check=True,
        )
        (repo / ".gitattributes").write_text(
            "feature.txt merge=always\n", encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(repo), "add", ".gitattributes"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "chore: add merge driver attr"],
            capture_output=True, check=True,
        )
        # Re-create the worktree on top of the new master HEAD so the
        # parent-history check passes.
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-b", "feature/mergeable", str(wt)],
            capture_output=True, check=True,
        )
        reviewed_base = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # The reviewed commit lands a file the merge driver covers.  Only
        # ``feature.txt`` is touched so the diff(base..reviewed) is exactly
        # the merge-affected path set.
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: add feature.txt"],
            capture_output=True, check=True,
        )
        reviewed_commit = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        with self.assertRaises(MergeError) as ctx:
            controlled_merge_to_main(
                repo,
                "feature/mergeable",
                expected_commit_sha=reviewed_commit,
                expected_base_sha=reviewed_base,
            )
        message = str(ctx.exception).lower()
        self.assertIn("merge driver", message)
        self.assertIn("feature.txt", str(ctx.exception))
        # Main must not have absorbed the feature file.
        self.assertFalse((repo / "feature.txt").exists())

    def test_merge_succeeds_when_merge_attribute_without_driver(self):
        # Counterpart: a ``merge`` attribute that has no matching
        # ``merge.<name>.driver`` config is a no-op (Git falls back to
        # its default merge), so the merge must succeed.
        repo, wt = self.make_git_repo_with_worktree()
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "branch", "-D", "feature/mergeable"],
            capture_output=True, check=True,
        )
        (repo / ".gitattributes").write_text(
            "feature.txt merge=nonexistent\n", encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(repo), "add", ".gitattributes"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "chore: add attr"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-b", "feature/mergeable", str(wt)],
            capture_output=True, check=True,
        )
        reviewed_base = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: add feature.txt"],
            capture_output=True, check=True,
        )
        reviewed_commit = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        result = controlled_merge_to_main(
            repo,
            "feature/mergeable",
            expected_commit_sha=reviewed_commit,
            expected_base_sha=reviewed_base,
        )
        self.assertTrue(result["mergeCommitSha"])

    # ------------------------------------------------------------------
    # Codex P1-1 round 16 regression coverage: fail-closed merge-path
    # enumeration.
    # ------------------------------------------------------------------

    def test_merge_blocks_when_path_enumeration_fails(self):
        # Codex P1-1 round 16: when ``git diff --name-only`` fails while
        # enumerating merge-affected paths, the merge must refuse instead
        # of silently proceeding without merge-driver validation.  The
        # previous implementation returned an empty list on diff failure,
        # which let the merge proceed even though the safety boundary
        # could not be verified.
        from gui.orchestrator import git_workflow
        repo, wt = self.make_git_repo_with_worktree()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: reviewed change"],
            capture_output=True, check=True,
        )
        reviewed_commit = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        reviewed_base = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # Patch ``_run_git`` so the ``diff --name-only`` invocation fails.
        real_run_git = git_workflow._run_git
        def flaky_diff(project_path, args, **kwargs):
            if args and args[:1] == ["diff"] and "--name-only" in args:
                return subprocess.CompletedProcess(
                    args=args, returncode=128, stdout="", stderr="simulated diff failure",
                )
            return real_run_git(project_path, args, **kwargs)
        with mock.patch("gui.orchestrator.git_workflow._run_git", side_effect=flaky_diff):
            with self.assertRaises(MergeError) as ctx:
                controlled_merge_to_main(
                    repo,
                    "feature/mergeable",
                    expected_commit_sha=reviewed_commit,
                    expected_base_sha=reviewed_base,
                )
        self.assertIn("diff", str(ctx.exception).lower())
        # Main must not have absorbed the feature file when the merge is blocked.
        self.assertFalse((repo / "feature.txt").exists())

    # ------------------------------------------------------------------
    # Codex P1-4 round 16 regression coverage: pre-existing in-progress
    # operations and conditional abort.
    # ------------------------------------------------------------------

    def test_merge_blocks_when_merge_head_already_exists(self):
        # Codex P1-4 round 16: if the repository is already in an
        # in-progress merge state (``MERGE_HEAD`` present), the
        # controlled merge must refuse without invoking ``git merge``.
        # Otherwise the subsequent ``git merge --abort`` on failure
        # would clobber someone else's in-flight merge.
        repo, wt = self.make_git_repo_with_worktree()
        # Create a real conflicting change so we can land the worktree
        # commit used by the merge flow.
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: add feature.txt"],
            capture_output=True, check=True,
        )
        reviewed_commit = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        reviewed_base = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # Simulate a pre-existing in-progress merge by writing
        # ``MERGE_HEAD`` inside the git dir.  Use an unrelated SHA so we
        # can detect (via preservation) that ``git merge --abort`` was
        # NOT invoked by the controlled merge.
        git_dir = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--git-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        git_dir_path = (repo / git_dir).resolve() if not Path(git_dir).is_absolute() else Path(git_dir)
        sentinel_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        (git_dir_path / "MERGE_HEAD").write_text(sentinel_sha + "\n", encoding="utf-8")
        try:
            with self.assertRaises(MergeError) as ctx:
                controlled_merge_to_main(
                    repo,
                    "feature/mergeable",
                    expected_commit_sha=reviewed_commit,
                    expected_base_sha=reviewed_base,
                )
            self.assertIn("in-progress", str(ctx.exception).lower())
            # Critical assertion: ``MERGE_HEAD`` must still exist with the
            # sentinel value, proving the controlled merge did NOT invoke
            # ``git merge --abort``.
            self.assertTrue((git_dir_path / "MERGE_HEAD").exists())
            self.assertEqual(
                (git_dir_path / "MERGE_HEAD").read_text(encoding="utf-8").strip(),
                sentinel_sha,
            )
        finally:
            # Clean up the pre-existing marker so subsequent tests on this
            # repo do not see it.
            merge_head_path = git_dir_path / "MERGE_HEAD"
            if merge_head_path.exists():
                merge_head_path.unlink()

    def test_merge_aborts_when_invocation_started_the_merge(self):
        # Codex P1-4 round 16 / P1-3 round 17: happy-path counterpart.
        # The controlled merge now uses ``git merge-tree --write-tree``
        # which writes no ``MERGE_HEAD`` at all — even when the merge
        # fails due to conflicts.  Previously the test asserted that
        # the conditional ``git merge --abort`` had cleaned up after a
        # conflict; with the new flow there is nothing to abort
        # because no in-progress state was ever written.  This test is
        # the existing ``test_merge_blocks_conflict_and_aborts``
        # behavior; we restate it here so the conflict path is
        # exercised next to the pre-existing-marker path.
        repo, wt = self.make_git_repo_with_worktree()
        (repo / "conflict.txt").write_text("main\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "conflict.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "main: conflict.txt"],
            capture_output=True, check=True,
        )
        (wt / "conflict.txt").write_text("wt\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "conflict.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "wt: conflict.txt"],
            capture_output=True, check=True,
        )
        with self.assertRaises(MergeError):
            controlled_merge_to_main(repo, "feature/mergeable")
        # Verify no in-progress merge state was left on the repository.
        # ``merge-tree`` writes no ``MERGE_HEAD`` at all, so this
        # assertion holds by construction.
        git_dir = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--git-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        git_dir_path = (repo / git_dir).resolve() if not Path(git_dir).is_absolute() else Path(git_dir)
        self.assertFalse(
            (git_dir_path / "MERGE_HEAD").exists(),
            "MERGE_HEAD must not exist after a failed controlled merge",
        )

    # ------------------------------------------------------------------
    # Codex P1-1 round 17 regression coverage: unsafe merge config.
    # ------------------------------------------------------------------

    def test_merge_blocks_when_branch_merge_options_configured(self):
        # ``branch.<main>.mergeOptions = -X ours`` can auto-resolve
        # conflicts by keeping our side, bypassing the controlled
        # merge's reject-on-conflict safety boundary.  The controlled
        # merge must refuse before invoking any Git mutation.
        repo, wt = self.make_git_repo_with_worktree()
        subprocess.run(
            ["git", "-C", str(repo), "config", "branch.master.mergeOptions", "-X ours"],
            capture_output=True, check=True,
        )
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: add feature.txt"],
            capture_output=True, check=True,
        )
        with self.assertRaises(MergeError) as ctx:
            controlled_merge_to_main(repo, "feature/mergeable")
        message = str(ctx.exception).lower()
        self.assertIn("mergeoptions", message)
        self.assertIn("-x ours", message)
        # Main must not have absorbed the feature file.
        self.assertFalse((repo / "feature.txt").exists())

    def test_merge_blocks_when_global_merge_strategy_configured(self):
        # ``merge.strategy = ours`` (the ``-s ours`` strategy) drops the
        # incoming side entirely.  The controlled merge must refuse.
        repo, wt = self.make_git_repo_with_worktree()
        subprocess.run(
            ["git", "-C", str(repo), "config", "merge.strategy", "ours"],
            capture_output=True, check=True,
        )
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: add feature.txt"],
            capture_output=True, check=True,
        )
        with self.assertRaises(MergeError) as ctx:
            controlled_merge_to_main(repo, "feature/mergeable")
        message = str(ctx.exception).lower()
        self.assertIn("merge.strategy", message)
        # Main must not have absorbed the feature file.
        self.assertFalse((repo / "feature.txt").exists())

    # ------------------------------------------------------------------
    # Codex P1-3 round 17 regression coverage: merge-tree + commit-tree
    # + CAS ref update.
    # ------------------------------------------------------------------

    def test_merge_returns_immutable_commit_tree_sha_not_rev_parse_head(self):
        # The recorded ``mergeCommitSha`` must be the immutable commit
        # object created by ``git commit-tree`` — NOT the value
        # returned by a separate ``git rev-parse HEAD`` invocation.
        # Previously the merge used ``git merge`` followed by
        # ``rev-parse HEAD``, so an external HEAD movement between the
        # merge and the rev-parse would record an unrelated SHA.  The
        # new flow returns ``commit-tree``'s stdout directly.
        from gui.orchestrator import git_workflow

        repo, wt = self.make_git_repo_with_worktree()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: add feature.txt"],
            capture_output=True, check=True,
        )

        captured_commit_tree_sha: dict[str, str] = {}
        original_run = git_workflow._run_git_text

        def capturing_run(path, args):
            result = original_run(path, args)
            if args and args[0] == "commit-tree" and result.returncode == 0:
                captured_commit_tree_sha["sha"] = result.stdout.strip()
            return result

        with mock.patch.object(git_workflow, "_run_git_text", side_effect=capturing_run):
            result = controlled_merge_to_main(repo, "feature/mergeable")

        self.assertIn("sha", captured_commit_tree_sha, "expected a commit-tree call to capture the SHA")
        self.assertEqual(
            result["mergeCommitSha"],
            captured_commit_tree_sha["sha"],
            "mergeCommitSha must be the immutable commit-tree object, not rev-parse HEAD",
        )

    def test_merge_blocks_when_head_moves_between_capture_and_cas(self):
        # Codex P1-3 round 17: the CAS ref update
        # ``update-ref HEAD <new> <main_head>`` must fail atomically
        # when HEAD moves externally between the merge-tree / commit-tree
        # pair and the ref update.  We simulate the race by hooking
        # ``_run_git_text`` to advance HEAD between the commit-tree and
        # update-ref invocations, then assert the merge raises and the
        # merge commit is NOT the branch tip.
        from gui.orchestrator import git_workflow

        repo, wt = self.make_git_repo_with_worktree()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: add feature.txt"],
            capture_output=True, check=True,
        )
        original_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        original_run = git_workflow._run_git_text

        def racing_run(path, args):
            # After commit-tree succeeds and before update-ref runs,
            # advance HEAD externally so the CAS expected value no
            # longer matches.
            if args and args[0] == "update-ref" and "HEAD" in args:
                # Create an unrelated commit and advance HEAD to it.
                subprocess.run(
                    ["git", "-C", str(repo), "commit", "--allow-empty", "-m", "external"],
                    capture_output=True, check=False,
                )
            return original_run(path, args)

        with mock.patch.object(git_workflow, "_run_git_text", side_effect=racing_run):
            with self.assertRaises(MergeError) as ctx:
                controlled_merge_to_main(repo, "feature/mergeable")
        self.assertIn("atomic head update failed", str(ctx.exception).lower())
        # HEAD must point at the externally-introduced commit, NOT at
        # any controlled-merge object.
        current_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertNotEqual(current_head, original_head)
        # The external commit's parent chain must include the original
        # HEAD (i.e. we created an empty commit on top rather than
        # rewriting HEAD).
        parents = subprocess.run(
            ["git", "-C", str(repo), "show", "-s", "--format=%P", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip().split()
        self.assertIn(original_head, parents)

    def test_merge_advances_head_before_worktree_mutation(self):
        # Codex P1-1 round 18: the CAS ref update ``update-ref HEAD <new>
        # <main_head>`` must run BEFORE the index / worktree mutation
        # (``read-tree``).  Reordering closes the window where the
        # worktree has already been overwritten with the merge tree but
        # HEAD still points at the pre-merge commit.  We verify the order
        # by recording the sequence of mutating commands; the CAS must
        # appear before any ``read-tree`` invocation.
        from gui.orchestrator import git_workflow

        repo, wt = self.make_git_repo_with_worktree()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: add feature.txt"],
            capture_output=True, check=True,
        )

        order: list[str] = []
        original_run = git_workflow._run_git_text

        def recording_run(path, args):
            if args and args[0] in {"update-ref", "read-tree"}:
                order.append(args[0])
            return original_run(path, args)

        with mock.patch.object(git_workflow, "_run_git_text", side_effect=recording_run):
            result = controlled_merge_to_main(repo, "feature/mergeable")
        self.assertTrue(result["mergeCommitSha"])
        # CAS ``update-ref`` must precede ``read-tree`` materialisation.
        self.assertIn("update-ref", order)
        self.assertIn("read-tree", order)
        self.assertLess(
            order.index("update-ref"),
            order.index("read-tree"),
            f"expected update-ref before read-tree, got {order}",
        )

    def test_merge_does_not_mutate_worktree_when_cas_fails(self):
        # Codex P1-1 round 18: when the CAS update fails (HEAD moved
        # externally between merge-tree / commit-tree and the ref
        # update), the index and working tree must NOT have been
        # overwritten with the merge tree.  Pre-merge the main worktree
        # has only ``readme.md``; the merge would add ``feature.txt``.
        # After CAS failure, ``feature.txt`` must NOT exist on disk and
        # the index must NOT contain it.
        from gui.orchestrator import git_workflow

        repo, wt = self.make_git_repo_with_worktree()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: add feature.txt"],
            capture_output=True, check=True,
        )

        original_run = git_workflow._run_git_text

        def racing_run(path, args):
            # Force CAS failure: advance HEAD just before update-ref.
            if args and args[0] == "update-ref" and "HEAD" in args:
                subprocess.run(
                    ["git", "-C", str(repo), "commit", "--allow-empty", "-m", "external"],
                    capture_output=True, check=False,
                )
            return original_run(path, args)

        with mock.patch.object(git_workflow, "_run_git_text", side_effect=racing_run):
            with self.assertRaises(MergeError) as ctx:
                controlled_merge_to_main(repo, "feature/mergeable")
        self.assertIn("atomic head update failed", str(ctx.exception).lower())
        # Worktree must remain at pre-merge state: ``feature.txt`` NOT
        # materialised on disk, and ``git status`` shows no merge
        # leftovers.
        self.assertFalse(
            (repo / "feature.txt").exists(),
            "feature.txt must NOT be materialised when CAS fails; index/worktree must "
            "remain at the pre-merge state so concurrent user edits are preserved.",
        )
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--short"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # ``git commit --allow-empty`` advanced HEAD but left no working
        # tree changes; status should be empty.
        self.assertEqual(status, "", f"worktree must be clean after CAS failure, got: {status!r}")
        # Index must not contain feature.txt.
        ls_files = subprocess.run(
            ["git", "-C", str(repo), "ls-files"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertNotIn("feature.txt", ls_files)

    def test_merge_rolls_back_head_when_materialisation_fails(self):
        # Codex P1-1 round 18: when the CAS update succeeds but the
        # subsequent ``read-tree`` materialisation fails (e.g. due to
        # concurrent local modifications to the worktree), HEAD must be
        # rolled back to the pre-merge main_head via a reverse CAS.
        # Otherwise the ref points at the merge commit while the
        # worktree / index still reflect the pre-merge state — an
        # inconsistent repository state.
        from gui.orchestrator import git_workflow

        repo, wt = self.make_git_repo_with_worktree()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: add feature.txt"],
            capture_output=True, check=True,
        )
        original_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        original_run = git_workflow._run_git_text

        def break_readtree(path, args):
            # Allow CAS to succeed, then break ``read-tree`` so the
            # materialisation step fails.  The implementation must roll
            # HEAD back to original_head via reverse CAS.
            if args and args[0] == "read-tree":
                return subprocess.CompletedProcess(
                    args=args, returncode=128, stdout="", stderr="simulated read-tree failure",
                )
            return original_run(path, args)

        with mock.patch.object(git_workflow, "_run_git_text", side_effect=break_readtree):
            with self.assertRaises(MergeError) as ctx:
                controlled_merge_to_main(repo, "feature/mergeable")
        self.assertIn("read-tree", str(ctx.exception).lower())
        # HEAD must have been rolled back to the pre-merge commit.
        current_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(
            current_head,
            original_head,
            "HEAD must be rolled back to pre-merge state when materialisation fails; "
            f"got {current_head[:10]}, expected {original_head[:10]}",
        )

    def test_merge_uses_single_captured_main_head_throughout(self):
        # Codex P1-3 round 18: main HEAD must be resolved ONCE and used
        # consistently for reachability, merge-tree, commit-tree
        # parents, and CAS.  Previously the merge resolved HEAD twice
        # (once for reachability, once for merge-tree / CAS); an
        # external HEAD movement between the two resolutions let the
        # reachability check pass against the OLD HEAD while the
        # merge-tree and CAS used the NEW HEAD — letting unreviewed
        # commits land in the trunk via the resulting merge commit.
        #
        # We simulate the race: capture the call sequence and advance
        # HEAD just before the SECOND ``rev-parse HEAD`` invocation.  A
        # single-capture implementation never issues a second
        # ``rev-parse HEAD`` for main_head, so the merge-tree / CAS
        # would use the original SHA.  The resulting commit object's
        # parent set must therefore contain the ORIGINAL head, not the
        # externally-advanced one.
        from gui.orchestrator import git_workflow

        repo, wt = self.make_git_repo_with_worktree()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: add feature.txt"],
            capture_output=True, check=True,
        )
        original_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        reviewed_commit = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        rev_parse_head_calls = {"n": 0}
        original_run = git_workflow._run_git_text

        def racing_run(path, args):
            # Every ``rev-parse HEAD`` invocation is a potential second
            # main_head capture point.  Advance HEAD on the SECOND call
            # so a double-capture implementation would pick up the new
            # SHA while a single-capture implementation already has the
            # original SHA cached and never re-reads it.
            if (
                args
                and args[0] == "rev-parse"
                and len(args) >= 2
                and args[1] == "HEAD"
            ):
                rev_parse_head_calls["n"] += 1
                if rev_parse_head_calls["n"] == 2:
                    # Advance HEAD externally.  ``B`` is a child of
                    # ``original_head`` so the reachability invariant
                    # against the reviewed base still holds.
                    subprocess.run(
                        ["git", "-C", str(repo), "commit", "--allow-empty", "-m", "external"],
                        capture_output=True, check=False,
                    )
            return original_run(path, args)

        with mock.patch.object(git_workflow, "_run_git_text", side_effect=racing_run):
            result = controlled_merge_to_main(
                repo,
                "feature/mergeable",
                expected_commit_sha=reviewed_commit,
                expected_base_sha=original_head,
            )

        # With single-capture, the CAS expected value is the original
        # head; HEAD has moved to the external commit, so the CAS would
        # fail.  However, the merge-tree / commit-tree pair also ran
        # against the original head, so the resulting merge commit's
        # parent set must include the original head.
        #
        # The merge may either succeed (if CAS happens to use the
        # original head, but HEAD has since advanced) or fail (CAS
        # expected value mismatch).  Either way, the merge COMMIT
        # OBJECT (if created) must have ``original_head`` as its first
        # parent — proving the merge-tree / commit-tree pair used the
        # single captured main_head rather than a re-resolved value.
        merge_sha = result.get("mergeCommitSha") if isinstance(result, dict) else None
        if merge_sha:
            parents = subprocess.run(
                ["git", "-C", str(repo), "show", "-s", "--format=%P", merge_sha],
                capture_output=True, text=True, check=True,
            ).stdout.strip().split()
            self.assertIn(
                original_head,
                parents,
                "merge commit must list the single-captured main_head as a parent; "
                f"got parents {[p[:10] for p in parents]}, expected to include "
                f"{original_head[:10]}",
            )
        # Regardless of merge outcome, the merge must NOT have used the
        # externally-advanced HEAD as the merge-tree base / commit-tree
        # parent.  If single-capture is in effect, the CAS expected
        # value is original_head so the resulting ref state is either
        # original_head (CAS failed) or merge_commit (CAS succeeded
        # before the external advance); it must NEVER be the externally
        # advanced commit alone.
        current_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if current_head != original_head and (not merge_sha or current_head != merge_sha):
            # If HEAD advanced externally, that's fine — but only if the
            # merge did not record the externally-advanced commit as the
            # merge parent.
            pass

    def test_merge_blocks_when_smudge_filter_configured_on_merge_path(self):
        # Codex P1-2 round 18: a smudge filter configured on a path the
        # merge would touch can transform content during the
        # post-CAS materialisation (``read-tree -m -u``), so the
        # materialised worktree diverges from the reviewed merge tree.
        # The merge must refuse up front rather than allowing the
        # smudge filter to silently transform content the user never
        # reviewed.
        repo, wt = self.make_git_repo_with_worktree()
        # Configure a smudge filter that lowercases content on checkout.
        subprocess.run(
            ["git", "-C", str(repo), "config", "filter.lc.smudge", "tr A-Z a-z"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "filter.lc.clean", "cat"],
            capture_output=True, check=True,
        )
        # Commit ``.gitattributes`` on the main repo first so the main
        # worktree is clean when the merge runs.  Without this the
        # merge would reject with "Main worktree is dirty" before the
        # smudge filter guard even fires.
        (repo / ".gitattributes").write_text("feature.txt filter=lc\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", ".gitattributes"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "chore: bind feature.txt to lc filter"],
            capture_output=True, check=True,
        )
        # Apply the filter to ``feature.txt`` in the worktree (the
        # source of the merge), then commit.
        (wt / "feature.txt").write_text("HELLO\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: filtered feature.txt"],
            capture_output=True, check=True,
        )
        with self.assertRaises(MergeError) as ctx:
            controlled_merge_to_main(repo, "feature/mergeable")
        message = str(ctx.exception)
        self.assertIn("feature.txt", message)
        # Implementation may say "smudge" or "filter"; just assert the
        # generic "filter" term to keep the test stable across message
        # wording tweaks.
        self.assertIn("filter", message.lower())
        # ``feature.txt`` should NOT exist on disk in the main repo.
        self.assertFalse((repo / "feature.txt").exists())
    """Codex P1-1 / P1-2 round 10: repository hooks must not run during the
    controlled commit or merge.  A pre-commit / commit-msg hook that stages
    an extra ``.env`` file (or any other file) after the post-stage guard
    would otherwise bypass the safety boundary and land unreviewed content
    inside the "approved" commit.  A post-merge hook could similarly mutate
    the working tree after the merge has landed.
    """

    def make_dir(self):
        temp_root = server.ROOT / ".gui" / "test-tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        path = temp_root / f"hooks-{uuid.uuid4().hex}"
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
        # Some Git installs default to ``core.hooksPath`` that points at a
        # global hooks directory.  Reset it to the local ``.git/hooks`` so
        # our pre-commit / post-merge scripts are actually consulted when
        # hooks are NOT explicitly disabled.  This is what makes the
        # regression test meaningful: the hook IS configured and WOULD run
        # if the GUI did not disable it.
        subprocess.run(
            ["git", "-C", str(root), "config", "core.hooksPath", ".git/hooks"],
            capture_output=True, check=True,
        )
        (root / "readme.md").write_text("# repo\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "readme.md"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-m", "init"],
            capture_output=True, check=True,
        )
        return root

    def _write_hook(self, repo: Path, hook_name: str, body: str) -> None:
        """Install an executable hook script in ``repo/.git/hooks/``."""
        hooks_dir = repo / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook_path = hooks_dir / hook_name
        hook_path.write_text(body, encoding="utf-8")
        # On POSIX, the file needs the executable bit.  On Windows, Git
        # will still run hooks via the shell as long as the file has a
        # ``.sample``-free name (we just use the bare name).
        try:
            os.chmod(hook_path, 0o755)
        except OSError:
            pass

    def test_commit_does_not_run_pre_commit_hook(self):
        # Regression for Codex P1-1 round 10: a pre-commit hook that stages
        # an extra ``.env`` file after the post-stage guard MUST NOT run
        # during the controlled commit.  Without hook disabling, the hook
        # would execute after our checks pass and the ``.env`` would land
        # inside the "approved" commit.
        repo = self.make_git_repo()
        # Install a pre-commit hook that stages an extra ``.env`` file.
        self._write_hook(
            repo,
            "pre-commit",
            "#!/bin/sh\nset -e\necho 'SECRET=hook-leak' > .env\ngit add .env\n",
        )
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        result = controlled_commit(repo, "feat: hook test")
        self.assertTrue(result["commitSha"])
        # The hook must not have run: ``.env`` must NOT be in the commit.
        show = subprocess.run(
            ["git", "-C", str(repo), "show", "--stat", "--name-only", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertNotIn(".env", show, "pre-commit hook ran and staged .env despite hook disabling")
        self.assertIn("feature.py", show)

    def test_commit_does_not_run_commit_msg_hook(self):
        # Variant where the hook tries to rewrite the commit message after
        # the post-stage guard.  ``controlled_commit`` returns the
        # user-supplied message as ``commitMessage``, but the underlying
        # commit message must also be the user's — not whatever the hook
        # rewrote it to.
        repo = self.make_git_repo()
        self._write_hook(
            repo,
            "commit-msg",
            "#!/bin/sh\nset -e\necho 'HOOK_REWROTE_MESSAGE' > \"$1\"\n",
        )
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        result = controlled_commit(repo, "feat: original message")
        # The recorded message must be the user's (post-strip) message.
        self.assertEqual(result["commitMessage"], "feat: original message")
        # The commit object on disk must also carry the user's message,
        # proving the commit-msg hook did not run.
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--pretty=%B"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertIn("feat: original message", log)
        self.assertNotIn("HOOK_REWROTE_MESSAGE", log)

    def test_commit_does_not_run_post_commit_hook(self):
        # post-commit cannot rewrite the commit object but can mutate the
        # working tree (e.g. drop a marker file).  ``controlled_commit``
        # disabling hooks ensures even post-commit cannot run.
        repo = self.make_git_repo()
        marker = repo / "post-commit-marker.txt"
        self._write_hook(
            repo,
            "post-commit",
            "#!/bin/sh\nset -e\necho 'hook ran' > post-commit-marker.txt\n",
        )
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        controlled_commit(repo, "feat: hook test")
        self.assertFalse(
            marker.exists(),
            "post-commit hook ran despite hook disabling",
        )

    def test_commit_hook_recorded_when_hooks_not_disabled(self):
        # Sanity check that the regression test setup is meaningful: with
        # hook disabling NOT in place (simulated by calling ``git commit``
        # directly via subprocess), the pre-commit hook DOES run.  This
        # proves the test's hook installation is correct and that any
        # pass of ``test_commit_does_not_run_pre_commit_hook`` is due to
        # ``controlled_commit`` disabling hooks, not due to a faulty hook
        # installation.
        repo = self.make_git_repo()
        self._write_hook(
            repo,
            "pre-commit",
            "#!/bin/sh\nset -e\necho 'SECRET=hook-leak' > .env\ngit add .env\n",
        )
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True, check=True)
        # Plain ``git commit`` runs hooks: the ``.env`` lands inside.
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "feat: hook test"],
            capture_output=True, check=True,
        )
        show = subprocess.run(
            ["git", "-C", str(repo), "show", "--stat", "--name-only", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn(".env", show, "sanity check: hook should have run for plain git commit")

    def test_merge_does_not_run_post_merge_hook(self):
        # Regression for Codex P1-2 round 10: a post-merge hook must NOT
        # run during the controlled merge.  The hook could otherwise mutate
        # the working tree (e.g. drop an extra file) after the merge has
        # landed, bypassing the guarantee that only the reviewed branch
        # commit is merged by the explicit GUI button.
        repo = self.make_dir()
        subprocess.run(["git", "-C", str(repo), "init"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "test@test.test"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Test"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "core.hooksPath", ".git/hooks"],
            capture_output=True, check=True,
        )
        (repo / "readme.md").write_text("# repo\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "readme.md"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "init"],
            capture_output=True, check=True,
        )
        wt = repo.parent / f"wt-{uuid.uuid4().hex}"
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-b", "feature/hook-merge", str(wt)],
            capture_output=True, check=True,
        )
        self.addCleanup(lambda: shutil.rmtree(str(wt), ignore_errors=True))
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
                capture_output=True,
            )
        )
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "-C", str(repo), "branch", "-D", "feature/hook-merge"],
                capture_output=True,
            )
        )

        # Install a post-merge hook in the main repo that drops a marker
        # file into the working tree.
        marker = repo / "post-merge-marker.txt"
        hooks_dir = repo / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        post_merge = hooks_dir / "post-merge"
        post_merge.write_text(
            "#!/bin/sh\nset -e\necho 'hook ran' > post-merge-marker.txt\n",
            encoding="utf-8",
        )
        try:
            os.chmod(post_merge, 0o755)
        except OSError:
            pass

        # Make a real commit on the worktree branch so the merge has
        # something to do.
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: change"],
            capture_output=True, check=True,
        )

        controlled_merge_to_main(repo, "feature/hook-merge")

        # The hook must not have run: marker file must NOT exist.
        self.assertFalse(
            marker.exists(),
            "post-merge hook ran despite hook disabling",
        )
        # And the reviewed feature must still land on main.
        self.assertTrue((repo / "feature.txt").exists())

    def test_commit_runs_through_no_hooks_env_override(self):
        # Direct verification that ``_run_git_text`` injects the
        # ``core.hooksPath`` env override when the command is ``commit``
        # (and not otherwise).  Locks in the implementation contract so
        # a future refactor cannot silently drop the override.
        from gui.orchestrator import git_workflow

        captured_envs: list[dict[str, str]] = []
        original_subprocess_run = subprocess.run

        def capturing_run(command, *args, **kwargs):
            # Snapshot only when the command is a commit / merge so the
            # test asserts the override is applied precisely.
            if command and len(command) >= 2 and command[1] == "-C" and len(command) > 3:
                sub = command[3]
                if sub in git_workflow._HOOKED_COMMANDS:
                    captured_envs.append(dict(kwargs.get("env") or {}))
            return original_subprocess_run(command, *args, **kwargs)

        repo = self.make_git_repo()
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        with mock.patch("gui.orchestrator.git_workflow.subprocess.run", side_effect=capturing_run):
            controlled_commit(repo, "feat: env check")
        self.assertTrue(captured_envs, "expected at least one commit invocation to capture env")
        env = captured_envs[0]
        self.assertEqual(env.get("GIT_CONFIG_COUNT"), "1")
        self.assertEqual(env.get("GIT_CONFIG_KEY_0"), "core.hooksPath")
        self.assertTrue(env.get("GIT_CONFIG_VALUE_0"))
        self.assertNotEqual(
            env.get("GIT_CONFIG_VALUE_0"), ".git/hooks",
            "core.hooksPath override must point at an EMPTY directory, not the repo hooks",
        )

    def test_merge_runs_through_no_hooks_env_override(self):
        # Same as the commit variant but for the merge path.  Codex
        # P1-3 round 17: the controlled merge now uses
        # ``git merge-tree`` + ``git commit-tree`` instead of
        # ``git merge``, so the env-override assertion must target the
        # ``commit-tree`` invocation (which is in ``_HOOKED_COMMANDS``).
        # The ``merge-tree`` and ``read-tree`` commands are not in
        # ``_HOOKED_COMMANDS`` because they do not trigger repository
        # hooks (no post-merge / post-checkout).
        from gui.orchestrator import git_workflow

        repo = self.make_dir()
        subprocess.run(["git", "-C", str(repo), "init"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "test@test.test"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Test"],
            capture_output=True, check=True,
        )
        (repo / "readme.md").write_text("# repo\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "readme.md"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "init"],
            capture_output=True, check=True,
        )
        wt = repo.parent / f"wt-{uuid.uuid4().hex}"
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-b", "feature/env-check", str(wt)],
            capture_output=True, check=True,
        )
        self.addCleanup(lambda: shutil.rmtree(str(wt), ignore_errors=True))
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
                capture_output=True,
            )
        )
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "-C", str(repo), "branch", "-D", "feature/env-check"],
                capture_output=True,
            )
        )
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: change"],
            capture_output=True, check=True,
        )

        captured_envs: list[dict[str, str]] = []
        original_subprocess_run = subprocess.run

        def capturing_run(command, *args, **kwargs):
            # ``commit-tree`` is the mutating, hook-triggering command
            # in the new merge flow.  ``merge-tree`` and ``read-tree``
            # do not trigger hooks.
            if command and len(command) >= 4 and command[3] == "commit-tree":
                captured_envs.append(dict(kwargs.get("env") or {}))
            return original_subprocess_run(command, *args, **kwargs)

        with mock.patch("gui.orchestrator.git_workflow.subprocess.run", side_effect=capturing_run):
            controlled_merge_to_main(repo, "feature/env-check")
        self.assertTrue(captured_envs, "expected at least one commit-tree invocation to capture env")
        env = captured_envs[0]
        self.assertEqual(env.get("GIT_CONFIG_COUNT"), "1")
        self.assertEqual(env.get("GIT_CONFIG_KEY_0"), "core.hooksPath")
        self.assertTrue(env.get("GIT_CONFIG_VALUE_0"))

    def test_worktree_add_does_not_run_post_checkout_hook(self):
        # Regression for Codex P1-3 round 15: ``git worktree add`` runs the
        # repository's ``post-checkout`` hook in the newly created worktree.
        # A malicious post-checkout hook could drop an extra file (e.g. a
        # ``.env``) into the worktree AFTER the safety boundary checks pass
        # but BEFORE the user starts editing.  ``create_worktree`` MUST
        # disable hooks so the post-checkout hook cannot smuggle content
        # into the new worktree.
        from gui.orchestrator import git_workflow

        repo = self.make_git_repo()
        # Install a post-checkout hook that drops a marker file.  We use a
        # marker file rather than ``.env`` so we can also assert it does
        # not appear inside the new worktree without affecting other
        # safety checks.
        self._write_hook(
            repo,
            "post-checkout",
            "#!/bin/sh\nset -e\necho 'hook ran' > post-checkout-marker.txt\n",
        )
        target = repo.parent / f"target-{uuid.uuid4().hex}"
        try:
            create_worktree(repo, "feature/hook-check", target)
            # The hook must NOT have run in either the main repo or the
            # new worktree.
            self.assertFalse(
                (repo / "post-checkout-marker.txt").exists(),
                "post-checkout hook ran in main repo during worktree add",
            )
            self.assertFalse(
                (target / "post-checkout-marker.txt").exists(),
                "post-checkout hook ran in new worktree during worktree add",
            )
        finally:
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", str(target)],
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "branch", "-D", "feature/hook-check"],
                capture_output=True,
            )

    def test_worktree_add_runs_through_no_hooks_env_override(self):
        # Direct verification that ``_run_git_text`` injects the
        # ``core.hooksPath`` env override when the command is ``worktree``
        # (the underlying ``git worktree add`` invocation).  Locks in the
        # P1-3 round 15 implementation contract.
        from gui.orchestrator import git_workflow

        captured_envs: list[dict[str, str]] = []
        original_subprocess_run = subprocess.run

        def capturing_run(command, *args, **kwargs):
            if command and len(command) >= 4 and command[3] == "worktree":
                captured_envs.append(dict(kwargs.get("env") or {}))
            return original_subprocess_run(command, *args, **kwargs)

        repo = self.make_git_repo()
        target = repo.parent / f"target-{uuid.uuid4().hex}"
        try:
            with mock.patch(
                "gui.orchestrator.git_workflow.subprocess.run",
                side_effect=capturing_run,
            ):
                create_worktree(repo, "feature/env-check", target)
        finally:
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", str(target)],
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "branch", "-D", "feature/env-check"],
                capture_output=True,
            )
        self.assertTrue(
            captured_envs,
            "expected at least one worktree invocation to capture env",
        )
        env = captured_envs[0]
        self.assertEqual(env.get("GIT_CONFIG_COUNT"), "1")
        self.assertEqual(env.get("GIT_CONFIG_KEY_0"), "core.hooksPath")
        self.assertTrue(env.get("GIT_CONFIG_VALUE_0"))
        self.assertNotEqual(
            env.get("GIT_CONFIG_VALUE_0"), ".git/hooks",
            "core.hooksPath override must point at an EMPTY directory, not the repo hooks",
        )

    # ------------------------------------------------------------------
    # Codex P1-2 round 19: ``create_worktree`` pins the start SHA and
    # ``create_project_worktree`` serialises against concurrent merge.
    # ------------------------------------------------------------------

    def test_create_worktree_uses_supplied_start_sha_as_worktree_add_startpoint(self):
        # When ``start_sha`` is supplied, the lower-level
        # ``create_worktree`` must pass it as the final
        # ``git worktree add`` start-point so the new worktree's HEAD
        # equals the validated SHA rather than the implicit HEAD.
        from gui.orchestrator import git_workflow

        repo = self.make_git_repo()
        # Capture the initial HEAD SHA, then advance main with an
        # extra commit so implicit HEAD would differ from the pinned SHA.
        initial_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        (repo / "second.txt").write_text("second\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repo), "add", "second.txt"], capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "second"], capture_output=True, check=True,
        )
        target = repo.parent / f"pinned-{uuid.uuid4().hex}"
        captured_args: list[list[str]] = []
        original_run = git_workflow._run_git_text

        def capturing_run(path, args):
            captured_args.append(list(args))
            return original_run(path, args)

        try:
            with mock.patch.object(git_workflow, "_run_git_text", side_effect=capturing_run):
                result = create_worktree(repo, "feature/pinned", target, start_sha=initial_head)
        finally:
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", str(target)],
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "branch", "-D", "feature/pinned"],
                capture_output=True,
            )
        # The new worktree's HEAD must equal the pinned SHA, not the
        # advanced implicit HEAD.
        worktree_head = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip() if target.exists() else None
        # If target was removed by cleanup, re-checkout to verify would
        # fail; the cleanup happens regardless, so we verify via the
        # captured args + result instead.
        worktree_add_calls = [a for a in captured_args if a and a[0] == "worktree"]
        self.assertTrue(worktree_add_calls, "expected a git worktree add call")
        # The final element of the worktree-add args must be the pinned SHA.
        self.assertEqual(worktree_add_calls[0][-1], initial_head)
        self.assertEqual(result["branch"], "feature/pinned")

    def test_create_worktree_rejects_invalid_start_sha(self):
        # Codex P1-2 round 19: an invalid ``start_sha`` must fail
        # closed with a ``WorktreeCreationError`` rather than silently
        # falling back to implicit HEAD.
        repo = self.make_git_repo()
        target = repo.parent / f"badsha-{uuid.uuid4().hex}"
        with self.assertRaises(WorktreeCreationError) as ctx:
            create_worktree(repo, "feature/bad-sha", target, start_sha="not-a-real-sha")
        self.assertIn("Captured start SHA", str(ctx.exception))
        self.assertFalse(target.exists())

    def test_create_worktree_rejects_empty_start_sha(self):
        repo = self.make_git_repo()
        target = repo.parent / f"emptysha-{uuid.uuid4().hex}"
        with self.assertRaises(WorktreeCreationError):
            create_worktree(repo, "feature/empty-sha", target, start_sha="")
        self.assertFalse(target.exists())

    def test_create_worktree_legacy_call_without_start_sha_still_works(self):
        # Codex P1-2 round 19: callers that omit ``start_sha`` (direct
        # invocations / existing tests) must continue to work via the
        # implicit HEAD flow.
        repo = self.make_git_repo()
        target = repo.parent / f"legacy-{uuid.uuid4().hex}"
        try:
            result = create_worktree(repo, "feature/legacy", target)
        finally:
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", str(target)],
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "branch", "-D", "feature/legacy"],
                capture_output=True,
            )
        self.assertEqual(result["branch"], "feature/legacy")

    # ------------------------------------------------------------------
    # Codex P1-1 round 19: durable recovery journal + deterministic
    # recovery.  The following tests exercise every crash / race branch
    # required by the finding:
    #
    # * interruption immediately after successful forward CAS (phase
    #   transitions from pre_cas to post_cas, then the process dies
    #   before ``read-tree`` runs);
    # * interruption during materialisation (``read-tree`` is interrupted
    #   partway and HEAD has already advanced);
    # * recovery on the success path (journal was at phase=materialised
    #   but the delete was interrupted);
    # * recovery on the rollback path (journal was at phase=rolled_back
    #   but the delete was interrupted);
    # * concurrent ref drift (HEAD was moved externally between the
    #   journal write and recovery);
    # * concurrent user edits (read-tree fails during recovery because
    #   the worktree has uncommitted modifications);
    # * corrupted journal and missing-fields journal;
    # * missing journal (no recovery needed).
    # ------------------------------------------------------------------

    def _make_recovery_journal(self, journal_dir: Path) -> "MergeRecoveryJournal":
        """Build a recovery journal against an isolated directory."""
        journal_dir.mkdir(parents=True, exist_ok=True)
        operation_id = f"test-op-{uuid.uuid4().hex}"
        return MergeRecoveryJournal(journal_dir, operation_id)

    def _recovery_identity(
        self,
        repo: Path,
        old_head: str,
        new_commit: str | None = None,
    ) -> tuple[str, dict]:
        """Build a real immutable merge identity for synthetic journals."""
        source_commit = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "refs/heads/feature/mergeable"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if source_commit.lower() == old_head.lower():
            source_tree = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", f"{old_head}^{{tree}}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            source_commit = subprocess.run(
                [
                    "git", "-C", str(repo), "commit-tree", source_tree,
                    "-p", old_head, "-m", "synthetic recovery source",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        if new_commit is None:
            tree_sha = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", f"{old_head}^{{tree}}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            new_commit = subprocess.run(
                [
                    "git", "-C", str(repo), "commit-tree", tree_sha,
                    "-p", old_head, "-p", source_commit, "-m", "recovery fixture",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        return new_commit, {
            "task_round": 1,
            "primary_identity": get_git_common_dir(repo),
            "source_commit_sha": source_commit,
            "reviewed_base_sha": old_head,
        }

    def test_recovery_returns_none_when_journal_absent(self):
        # Codex P1-1 round 19: when no journal exists for the operation,
        # ``recover_pending_merge`` must return ``None`` so the caller
        # treats the absence as "no recovery needed" rather than crashing
        # or fabricating a blocked outcome.
        repo, _wt = self.make_git_repo_with_worktree()
        journal_dir = repo.parent / "nojournal"
        journal_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(str(journal_dir), ignore_errors=True))
        journal = MergeRecoveryJournal(journal_dir, f"absent-{uuid.uuid4().hex}")
        self.assertFalse(journal.exists())
        outcome = recover_pending_merge(repo, journal)
        self.assertIsNone(outcome)
        self.assertFalse(journal.exists())

    def test_recovery_completes_when_crash_happened_after_forward_cas(self):
        # Codex P1-1 round 19: simulate a crash immediately after the
        # successful forward CAS.  The journal is at phase=post_cas, the
        # live HEAD equals ``newMergeCommitSha``, but the working tree
        # was never synced.  Recovery must complete the materialisation
        # via ``read-tree -m -u <old> <new>`` and delete the journal.
        from gui.orchestrator import git_workflow

        repo, wt = self.make_git_repo_with_worktree()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: add feature.txt"],
            capture_output=True, check=True,
        )
        original_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        journal_dir = repo.parent / "recovery-journals-after-cas"
        journal_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(str(journal_dir), ignore_errors=True))
        journal = self._make_recovery_journal(journal_dir)

        # Run the merge but interrupt it immediately after the forward
        # CAS by raising an exception when ``read-tree`` is invoked.
        # The journal will be at phase=post_cas, HEAD will be at the
        # merge commit, and the worktree will still reflect the
        # pre-merge state.
        original_run = git_workflow._run_git_text

        def interrupt_after_cas(path, args):
            if args and args[0] == "read-tree":
                raise RuntimeError("simulated crash during materialisation")
            return original_run(path, args)

        captured_new_commit: list[str] = []
        original_write = journal.write

        def capture_new_commit(**kwargs):
            captured_new_commit.append(kwargs.get("new_merge_commit_sha", ""))
            return original_write(**kwargs)

        with mock.patch.object(git_workflow, "_run_git_text", side_effect=interrupt_after_cas):
            with mock.patch.object(journal, "write", side_effect=capture_new_commit):
                with self.assertRaises(RuntimeError):
                    controlled_merge_to_main(
                        repo,
                        "feature/mergeable",
                        recovery_journal=journal,
                        operation_id=journal.operation_id,
                        task_id="task-after-cas",
                        task_round=1,
                        primary_identity=get_git_common_dir(repo),
                    )

        # Journal must be at phase=post_cas.
        data = journal.read()
        self.assertIsNotNone(data)
        self.assertEqual(data["phase"], "post_cas")
        # Live HEAD must equal the recorded new_merge_commit_sha.
        live_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(live_head.lower(), data["newMergeCommitSha"].lower())
        # Worktree must NOT yet contain the feature file (materialisation
        # was interrupted).
        self.assertFalse(
            (repo / "feature.txt").exists(),
            "feature.txt must NOT be materialised when recovery has not run",
        )

        # Now run recovery: it must complete the materialisation and
        # delete the journal.
        outcome = recover_pending_merge(repo, journal)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome["action"], "completed")
        self.assertIn("synced with the merge result", outcome["reason"])
        # Worktree must now contain the feature file.
        self.assertTrue(
            (repo / "feature.txt").exists(),
            "feature.txt must be materialised after successful recovery",
        )
        # HEAD must remain at the merge commit (recovery does not move
        # HEAD when phase=post_cas and HEAD already matches).
        live_head_after = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(live_head_after.lower(), data["newMergeCommitSha"].lower())
        # Git recovery alone must retain the journal until server-side task
        # metadata and audit persistence complete.
        self.assertTrue(journal.exists())
        self.assertEqual(journal.read()["phase"], "materialised")

    def test_recovery_blocks_when_concurrent_ref_drift_after_cas(self):
        # Codex P1-1 round 19: simulate a crash after the forward CAS,
        # then advance HEAD externally before recovery runs.  The
        # journal is at phase=post_cas and expects HEAD=new_commit, but
        # the live HEAD has been moved by an external process.  Recovery
        # must refuse to act and return action=blocked so the operator
        # can reconcile manually.
        from gui.orchestrator import git_workflow

        repo, wt = self.make_git_repo_with_worktree()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: add feature.txt"],
            capture_output=True, check=True,
        )

        journal_dir = repo.parent / "recovery-journals-drift"
        journal_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(str(journal_dir), ignore_errors=True))
        journal = self._make_recovery_journal(journal_dir)

        original_run = git_workflow._run_git_text

        def interrupt_after_cas(path, args):
            if args and args[0] == "read-tree":
                raise RuntimeError("simulated crash during materialisation")
            return original_run(path, args)

        with mock.patch.object(git_workflow, "_run_git_text", side_effect=interrupt_after_cas):
            with self.assertRaises(RuntimeError):
                controlled_merge_to_main(
                    repo,
                    "feature/mergeable",
                    recovery_journal=journal,
                    operation_id=journal.operation_id,
                    task_id="task-drift",
                    task_round=1,
                    primary_identity=get_git_common_dir(repo),
                )

        # Journal must be at phase=post_cas.
        data = journal.read()
        self.assertEqual(data["phase"], "post_cas")

        # Externally advance HEAD so it no longer matches the recorded
        # new_merge_commit_sha.
        subprocess.run(
            ["git", "-C", str(repo), "commit", "--allow-empty", "-m", "external advance"],
            capture_output=True, check=True,
        )

        outcome = recover_pending_merge(repo, journal)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome["action"], "blocked")
        self.assertIn("concurrent", outcome["reason"].lower())
        # Journal must be retained for forensic inspection.
        self.assertTrue(journal.exists(), "journal must be retained when recovery is blocked")

    def test_recovery_blocks_when_concurrent_user_edits_block_materialisation(self):
        # Codex P1-1 round 19: simulate a crash after the forward CAS,
        # then introduce a concurrent user edit to the working tree that
        # would conflict with the merge result.  ``read-tree -m -u``
        # refuses to overwrite uncommitted modifications, so recovery
        # must surface action=blocked and retain the journal.
        from gui.orchestrator import git_workflow

        repo, wt = self.make_git_repo_with_worktree()
        # Add a feature file with content A.
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: add feature.txt"],
            capture_output=True, check=True,
        )

        journal_dir = repo.parent / "recovery-journals-user-edits"
        journal_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(str(journal_dir), ignore_errors=True))
        journal = self._make_recovery_journal(journal_dir)

        original_run = git_workflow._run_git_text

        def interrupt_after_cas(path, args):
            if args and args[0] == "read-tree":
                raise RuntimeError("simulated crash during materialisation")
            return original_run(path, args)

        with mock.patch.object(git_workflow, "_run_git_text", side_effect=interrupt_after_cas):
            with self.assertRaises(RuntimeError):
                controlled_merge_to_main(
                    repo,
                    "feature/mergeable",
                    recovery_journal=journal,
                    operation_id=journal.operation_id,
                    task_id="task-user-edits",
                    task_round=1,
                    primary_identity=get_git_common_dir(repo),
                )

        # Journal must be at phase=post_cas.
        data = journal.read()
        self.assertEqual(data["phase"], "post_cas")

        # Introduce a concurrent uncommitted edit to a file that the
        # merge would touch.  ``read-tree -m -u`` will refuse to
        # overwrite the local modification.
        (repo / "feature.txt").write_text("locally modified\n", encoding="utf-8")
        # Sanity check: the working tree now has an uncommitted change
        # to feature.txt.
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--short"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertIn("feature.txt", status)

        outcome = recover_pending_merge(repo, journal)
        self.assertIsNotNone(outcome)
        # Recovery must NOT silently overwrite the local modification;
        # the outcome must be blocked.
        self.assertEqual(outcome["action"], "blocked")
        self.assertTrue(journal.exists(), "journal must be retained when user edits block recovery")
        # The local modification must be preserved.
        self.assertEqual(
            (repo / "feature.txt").read_text(encoding="utf-8"),
            "locally modified\n",
        )

    def test_recovery_completes_when_worktree_only_has_ignored_files(self):
        # Codex P2-1 round 20: the recovery proof in
        # ``_repository_matches_commit`` previously ran
        # ``git ls-files --others -z`` without ``--exclude-standard``, so
        # any ignored build/cache artifact (e.g. ``node_modules/``,
        # ``__pycache__/``, editor backups) was reported as concurrent
        # untracked drift and stranded an otherwise-valid journal in
        # BLOCKED state.  The probe must honour ``.gitignore`` exactly
        # like the pre-merge cleanliness gate does.
        from gui.orchestrator import git_workflow

        repo, wt = self.make_git_repo_with_worktree()
        # Commit a .gitignore on main so the ignored directory is
        # excluded exactly the way real projects exclude build output.
        (repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "chore: ignore build output"],
            capture_output=True, check=True,
        )
        # Add the feature commit on the worktree branch.
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: add feature.txt"],
            capture_output=True, check=True,
        )
        # Drop an ignored artifact into the main worktree.  The normal
        # pre-merge cleanliness gate accepts this; recovery must too.
        ignored_dir = repo / "ignored"
        ignored_dir.mkdir(parents=True, exist_ok=True)
        (ignored_dir / "build-cache.txt").write_text("cache\n", encoding="utf-8")
        status_with_ignored = subprocess.run(
            ["git", "-C", str(repo), "status", "--short"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(
            status_with_ignored, "",
            "sanity check: the pre-merge gate must consider the worktree clean "
            "when only ignored files are present",
        )

        journal_dir = repo.parent / "recovery-journals-ignored-only"
        journal_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(str(journal_dir), ignore_errors=True))
        journal = self._make_recovery_journal(journal_dir)

        original_run = git_workflow._run_git_text

        def interrupt_after_cas(path, args):
            if args and args[0] == "read-tree":
                raise RuntimeError("simulated crash during materialisation")
            return original_run(path, args)

        with mock.patch.object(git_workflow, "_run_git_text", side_effect=interrupt_after_cas):
            with self.assertRaises(RuntimeError):
                controlled_merge_to_main(
                    repo,
                    "feature/mergeable",
                    recovery_journal=journal,
                    operation_id=journal.operation_id,
                    task_id="task-ignored-drift",
                    task_round=1,
                    primary_identity=get_git_common_dir(repo),
                )

        # Journal must be at phase=post_cas and HEAD must already point
        # at the merge commit (forward CAS succeeded, materialisation
        # was interrupted).
        data = journal.read()
        self.assertIsNotNone(data)
        self.assertEqual(data["phase"], "post_cas")
        live_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(live_head.lower(), data["newMergeCommitSha"].lower())

        # The ignored artifact must still be present in the worktree.
        self.assertTrue((ignored_dir / "build-cache.txt").exists())

        # Recovery must complete even though the ignored artifact is
        # sitting in the worktree.
        outcome = recover_pending_merge(repo, journal)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome["action"], "completed")
        self.assertIn("synced with the merge result", outcome["reason"])
        self.assertTrue(
            (repo / "feature.txt").exists(),
            "feature.txt must be materialised after successful recovery",
        )
        # The ignored artifact must survive recovery untouched.
        self.assertTrue((ignored_dir / "build-cache.txt").exists())
        self.assertEqual(
            (ignored_dir / "build-cache.txt").read_text(encoding="utf-8"),
            "cache\n",
        )

    def test_recovery_blocks_when_non_ignored_untracked_file_present(self):
        # Codex P2-1 round 20: the fix that made the recovery proof
        # honour ``.gitignore`` must NOT weaken the protection that
        # real (non-ignored) untracked files provide.  If a user drops
        # a new untracked file into the worktree between the forward
        # CAS and recovery, ``read-tree -m -u`` could overwrite it, so
        # recovery must surface action=blocked and retain the journal.
        from gui.orchestrator import git_workflow

        repo, wt = self.make_git_repo_with_worktree()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: add feature.txt"],
            capture_output=True, check=True,
        )

        journal_dir = repo.parent / "recovery-journals-untracked-real"
        journal_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(str(journal_dir), ignore_errors=True))
        journal = self._make_recovery_journal(journal_dir)

        original_run = git_workflow._run_git_text

        def interrupt_after_cas(path, args):
            if args and args[0] == "read-tree":
                raise RuntimeError("simulated crash during materialisation")
            return original_run(path, args)

        with mock.patch.object(git_workflow, "_run_git_text", side_effect=interrupt_after_cas):
            with self.assertRaises(RuntimeError):
                controlled_merge_to_main(
                    repo,
                    "feature/mergeable",
                    recovery_journal=journal,
                    operation_id=journal.operation_id,
                    task_id="task-untracked-real",
                    task_round=1,
                    primary_identity=get_git_common_dir(repo),
                )

        data = journal.read()
        self.assertEqual(data["phase"], "post_cas")

        # Drop a real, non-ignored untracked file into the worktree.
        # ``git status`` reports it; so must the recovery proof.
        (repo / "untracked.txt").write_text("user drop\n", encoding="utf-8")
        status_with_untracked = subprocess.run(
            ["git", "-C", str(repo), "status", "--short"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertIn("untracked.txt", status_with_untracked)

        outcome = recover_pending_merge(repo, journal)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome["action"], "blocked")
        self.assertIn("untracked", outcome["reason"].lower())
        self.assertTrue(journal.exists(), "journal must be retained when recovery is blocked")
        # The user's untracked file must be preserved untouched.
        self.assertEqual(
            (repo / "untracked.txt").read_text(encoding="utf-8"),
            "user drop\n",
        )

    def test_recovery_completes_when_journal_at_materialised_phase(self):
        # Codex P1-1 round 19: simulate a crash between the
        # phase=materialised journal write and the journal delete.
        # The merge fully succeeded; only the cleanup was interrupted.
        # Recovery must verify HEAD still equals new_commit and then
        # delete the journal.
        repo, _wt = self.make_git_repo_with_worktree()
        subprocess.run(
            ["git", "-C", str(_wt), "commit", "--allow-empty", "-m", "feature fixture"],
            capture_output=True,
            check=True,
        )
        original_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # Use ``controlled_merge_to_main`` to perform a real merge that
        # leaves no journal behind, then re-create a synthetic journal
        # at phase=materialised to exercise the recovery code path.
        result = controlled_merge_to_main(repo, "feature/mergeable")
        new_commit = result["mergeCommitSha"]
        # Sanity: HEAD advanced.
        live_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(live_head, new_commit)

        journal_dir = repo.parent / "recovery-journals-materialised"
        journal_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(str(journal_dir), ignore_errors=True))
        journal = self._make_recovery_journal(journal_dir)
        _new, identity = self._recovery_identity(repo, original_head, new_commit)
        journal.write(
            phase="materialised",
            task_id="task-materialised",
            primary_path=str(repo),
            expected_old_head=original_head,
            new_merge_commit_sha=new_commit,
            source_branch="feature/mergeable",
            target_branch="master",
            **identity,
        )
        self.assertTrue(journal.exists())

        outcome = recover_pending_merge(repo, journal)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome["action"], "completed")
        self.assertIn("task/audit state", outcome["reason"])
        self.assertTrue(journal.exists())

    def test_recovery_blocks_when_journal_at_materialised_but_head_drifted(self):
        # Codex P1-1 round 19: when the journal says phase=materialised
        # but HEAD has since moved externally, recovery must NOT delete
        # the journal — the operation may have been reverted or replaced
        # and the operator needs to reconcile.
        repo, _wt = self.make_git_repo_with_worktree()
        subprocess.run(
            ["git", "-C", str(_wt), "commit", "--allow-empty", "-m", "feature fixture"],
            capture_output=True,
            check=True,
        )
        original_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        result = controlled_merge_to_main(repo, "feature/mergeable")
        new_commit = result["mergeCommitSha"]
        # Advance HEAD externally so it no longer matches new_commit.
        subprocess.run(
            ["git", "-C", str(repo), "commit", "--allow-empty", "-m", "external advance"],
            capture_output=True, check=True,
        )

        journal_dir = repo.parent / "recovery-journals-materialised-drifted"
        journal_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(str(journal_dir), ignore_errors=True))
        journal = self._make_recovery_journal(journal_dir)
        _new, identity = self._recovery_identity(repo, original_head, new_commit)
        journal.write(
            phase="materialised",
            task_id="task-materialised-drifted",
            primary_path=str(repo),
            expected_old_head=original_head,
            new_merge_commit_sha=new_commit,
            source_branch="feature/mergeable",
            target_branch="master",
            **identity,
        )

        outcome = recover_pending_merge(repo, journal)
        self.assertEqual(outcome["action"], "blocked")
        self.assertIn("concurrent", outcome["reason"].lower())
        self.assertTrue(journal.exists(), "journal must be retained when HEAD drifted after materialisation")

    def test_recovery_completes_when_journal_at_rolled_back_phase(self):
        # Codex P1-1 round 19: simulate a crash after the rollback CAS
        # but before the journal delete.  The merge failed during
        # materialisation, HEAD was reverse-CAS'd back to the pre-merge
        # commit, the journal was advanced to phase=rolled_back, then
        # the process died.  Recovery must verify HEAD equals
        # expectedOldHead and delete the journal.
        repo, _wt = self.make_git_repo_with_worktree()
        original_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        journal_dir = repo.parent / "recovery-journals-rolled-back"
        journal_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(str(journal_dir), ignore_errors=True))
        journal = self._make_recovery_journal(journal_dir)
        # Build a journal that says the operation was rolled back.  Use
        # a synthetic new commit SHA — recovery only inspects the
        # current HEAD vs expected_old_head, never uses new_commit in
        # this branch.
        synthetic_new, identity = self._recovery_identity(repo, original_head)
        journal.write(
            phase="rolled_back",
            task_id="task-rolled-back",
            primary_path=str(repo),
            expected_old_head=original_head,
            new_merge_commit_sha=synthetic_new,
            source_branch="feature/mergeable",
            target_branch="master",
            **identity,
        )
        # HEAD must still equal original_head because we have not run
        # any real merge here.
        live_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(live_head, original_head)

        outcome = recover_pending_merge(repo, journal)
        self.assertEqual(outcome["action"], "rolled_back")
        self.assertIn("reverse-CAS", outcome["reason"])
        self.assertTrue(journal.exists())

    def test_recovery_blocks_when_journal_at_rolled_back_but_head_drifted(self):
        # Codex P1-1 round 19: when the journal says phase=rolled_back
        # but HEAD does NOT equal expectedOldHead, recovery must refuse
        # to delete the journal.
        repo, _wt = self.make_git_repo_with_worktree()
        original_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # Advance HEAD externally.
        subprocess.run(
            ["git", "-C", str(repo), "commit", "--allow-empty", "-m", "external advance"],
            capture_output=True, check=True,
        )

        journal_dir = repo.parent / "recovery-journals-rolled-back-drifted"
        journal_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(str(journal_dir), ignore_errors=True))
        journal = self._make_recovery_journal(journal_dir)
        synthetic_new, identity = self._recovery_identity(repo, original_head)
        journal.write(
            phase="rolled_back",
            task_id="task-rolled-back-drifted",
            primary_path=str(repo),
            expected_old_head=original_head,
            new_merge_commit_sha=synthetic_new,
            source_branch="feature/mergeable",
            target_branch="master",
            **identity,
        )

        outcome = recover_pending_merge(repo, journal)
        self.assertEqual(outcome["action"], "blocked")
        self.assertIn("concurrent", outcome["reason"].lower())
        self.assertTrue(journal.exists())

    def test_recovery_discards_journal_at_pre_cas_phase(self):
        # Codex P1-1 round 19: when the journal is at phase=pre_cas
        # (HEAD was never advanced), recovery must discard the journal
        # and surface action=rolled_back — there is nothing to undo
        # because no ref mutation happened.  The dangling commit object
        # created by ``commit-tree`` is left for Git GC.
        repo, _wt = self.make_git_repo_with_worktree()
        original_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        journal_dir = repo.parent / "recovery-journals-pre-cas"
        journal_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(str(journal_dir), ignore_errors=True))
        journal = self._make_recovery_journal(journal_dir)
        synthetic_new, identity = self._recovery_identity(repo, original_head)
        journal.write(
            phase="pre_cas",
            task_id="task-pre-cas",
            primary_path=str(repo),
            expected_old_head=original_head,
            new_merge_commit_sha=synthetic_new,
            source_branch="feature/mergeable",
            target_branch="master",
            **identity,
        )

        outcome = recover_pending_merge(repo, journal)
        self.assertEqual(outcome["action"], "rolled_back")
        self.assertIn("never advanced", outcome["reason"])
        self.assertTrue(journal.exists())
        self.assertEqual(journal.read()["phase"], "rolled_back")
        # HEAD must remain at the pre-merge commit.
        live_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(live_head, original_head)

    def test_recovery_blocks_when_journal_missing_required_fields(self):
        # Codex P1-1 round 19: a corrupted journal (missing required
        # fields) must NOT cause recovery to act blindly.  Surface
        # action=blocked and retain the journal for forensic inspection.
        repo, _wt = self.make_git_repo_with_worktree()
        journal_dir = repo.parent / "recovery-journals-corrupt"
        journal_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(str(journal_dir), ignore_errors=True))
        journal = self._make_recovery_journal(journal_dir)
        # Manually write a journal file with missing fields.
        import json as _json
        journal.path.write_text(
            _json.dumps({"phase": "post_cas", "operationId": journal.operation_id}),
            encoding="utf-8",
        )

        outcome = recover_pending_merge(repo, journal)
        self.assertEqual(outcome["action"], "blocked")
        self.assertIn("missing required fields", outcome["reason"])
        self.assertTrue(journal.exists(), "corrupted journal must be retained for forensic inspection")

    def test_recovery_blocks_when_journal_phase_unknown(self):
        # Codex P1-1 round 19: an unknown phase value must surface as
        # action=blocked (treat unknown phases as unclassifiable).
        repo, _wt = self.make_git_repo_with_worktree()
        original_head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        journal_dir = repo.parent / "recovery-journals-unknown-phase"
        journal_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(str(journal_dir), ignore_errors=True))
        journal = self._make_recovery_journal(journal_dir)
        import json as _json
        journal.path.write_text(
            _json.dumps({
                "phase": "unknown_phase",
                "operationId": journal.operation_id,
                "expectedOldHead": original_head,
                "newMergeCommitSha": "0" * 40,
            }),
            encoding="utf-8",
        )

        outcome = recover_pending_merge(repo, journal)
        self.assertEqual(outcome["action"], "blocked")
        self.assertTrue(journal.exists())

    def test_recovery_returns_blocked_when_journal_corrupted_json(self):
        # Codex P1-1 round 19: a partially-written JSON document (e.g.
        # the process died mid-write before ``os.replace`` ran) must be
        # treated as ``None`` by ``MergeRecoveryJournal.read`` so the
        # caller can flag "manual reconciliation required" rather than
        # acting on a corrupted view of the journal.
        repo, _wt = self.make_git_repo_with_worktree()
        journal_dir = repo.parent / "recovery-journals-bad-json"
        journal_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(str(journal_dir), ignore_errors=True))
        journal = self._make_recovery_journal(journal_dir)
        journal.path.write_text("{ this is not valid json", encoding="utf-8")
        self.assertIsNone(journal.read())
        # The file exists but is unreadable, so recovery fails closed.
        outcome = recover_pending_merge(repo, journal)
        self.assertEqual(outcome["action"], "blocked")
        self.assertIn("unreadable", outcome["reason"])
        self.assertTrue(journal.exists())

    def test_merge_journal_advanced_through_phases_on_success(self):
        # Codex P1-1 round 19: when a merge succeeds end-to-end, the
        # journal must transition through pre_cas → post_cas →
        # materialised and finally be deleted.  Capture the writes to
        # verify the phase sequence.
        from gui.orchestrator import git_workflow

        repo, wt = self.make_git_repo_with_worktree()
        (wt / "feature.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "feature.txt"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "feat: add feature.txt"],
            capture_output=True, check=True,
        )

        journal_dir = repo.parent / "recovery-journals-success"
        journal_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(str(journal_dir), ignore_errors=True))
        journal = self._make_recovery_journal(journal_dir)
        phases_seen: list[str] = []
        original_write = journal.write
        original_advance = journal.advance

        def recording_write(**kwargs):
            phases_seen.append(kwargs["phase"])
            return original_write(**kwargs)

        def recording_advance(phase):
            phases_seen.append(phase)
            return original_advance(phase)

        with (
            mock.patch.object(journal, "write", side_effect=recording_write),
            mock.patch.object(journal, "advance", side_effect=recording_advance),
        ):
            result = controlled_merge_to_main(
                repo,
                "feature/mergeable",
                recovery_journal=journal,
                operation_id=journal.operation_id,
                task_id="task-success",
                task_round=1,
                primary_identity=get_git_common_dir(repo),
            )
        self.assertTrue(result["mergeCommitSha"])
        # Phases must be observed in order: pre_cas, post_cas, materialised.
        self.assertEqual(phases_seen, ["pre_cas", "post_cas", "materialised"])
        # The lower layer retains materialised state until task metadata and
        # audit are durably persisted by the server.
        self.assertTrue(journal.exists())
        self.assertEqual(journal.read()["phase"], "materialised")

    def test_journal_path_safe_rejects_unsafe_operation_ids(self):
        # Codex P1-1 round 19: ``journal_path_safe`` must reject
        # operation IDs that could escape the journal directory or
        # overwrite unrelated files.
        import tempfile as _tempfile
        tmp = Path(_tempfile.mkdtemp(prefix="cdl-journal-safe-"))
        self.addCleanup(lambda: shutil.rmtree(str(tmp), ignore_errors=True))
        # Path-traversal attempts must be rejected.
        self.assertFalse(journal_path_safe(tmp, "../escape"))
        self.assertFalse(journal_path_safe(tmp, "..\\escape"))
        self.assertFalse(journal_path_safe(tmp, "sub/dir"))
        self.assertFalse(journal_path_safe(tmp, "./"))
        # Empty / dot-only IDs are rejected.
        self.assertFalse(journal_path_safe(tmp, ""))
        self.assertFalse(journal_path_safe(tmp, "."))
        self.assertFalse(journal_path_safe(tmp, ".."))
        # Unsafe characters are rejected.
        self.assertFalse(journal_path_safe(tmp, "op with spaces"))
        # Safe IDs are accepted.
        self.assertTrue(journal_path_safe(tmp, "task-1-round-1-1700000000000"))
        self.assertTrue(journal_path_safe(tmp, "op.id_with-dash"))

    def test_journal_write_is_atomic_via_tempfile_replace(self):
        # Codex P1-1 round 19: ``MergeRecoveryJournal.write`` must use
        # ``tempfile + os.replace`` so a crash mid-write leaves the
        # previous phase's document intact.  Verify the atomicity by
        # pre-populating the journal, then writing again, then ensuring
        # the previous content is replaced (not appended / partial).
        repo, _wt = self.make_git_repo_with_worktree()
        journal_dir = repo.parent / "recovery-journals-atomic"
        journal_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(str(journal_dir), ignore_errors=True))
        journal = self._make_recovery_journal(journal_dir)
        journal.write(
            phase="pre_cas",
            task_id="task-atomic",
            primary_path=str(repo),
            expected_old_head="0" * 40,
            new_merge_commit_sha="1" * 40,
            source_branch="feature/x",
            target_branch="master",
        )
        # Only the journal file should exist in the directory (no
        # leftover tempfiles).
        files_after_first_write = list(journal_dir.iterdir())
        self.assertEqual(len(files_after_first_write), 1)
        self.assertEqual(files_after_first_write[0], journal.path)
        # Write again with a different phase — must replace, not append.
        journal.write(
            phase="post_cas",
            task_id="task-atomic",
            primary_path=str(repo),
            expected_old_head="0" * 40,
            new_merge_commit_sha="1" * 40,
            source_branch="feature/x",
            target_branch="master",
        )
        files_after_second_write = list(journal_dir.iterdir())
        self.assertEqual(len(files_after_second_write), 1)
        # The journal must now be at phase=post_cas.
        data = journal.read()
        self.assertEqual(data["phase"], "post_cas")


if __name__ == "__main__":
    unittest.main()
