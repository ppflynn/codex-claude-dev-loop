import shutil
import os
import sys
import time
import unittest
import uuid
import json
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui import server
from gui.orchestrator.git_tools import GitArtifacts, EnvFileChangedError, GitError
from gui.orchestrator.models import Task
from gui.orchestrator.store import TaskStore
from gui.orchestrator.test_runner import TestRunResult


class GuiServerTests(unittest.TestCase):
    def make_dir(self):
        temp_root = server.ROOT / ".gui" / "test-tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        path = temp_root / f"case-{uuid.uuid4().hex}"
        path.mkdir()
        self.addCleanup(lambda: shutil.rmtree(path, ignore_errors=True))
        return path

    def test_detects_orchestrator_project(self):
        root = self.make_dir()
        (root / "scripts").mkdir()
        (root / "docs").mkdir()
        (root / "scripts" / "run-claude.ps1").write_text("# fake", encoding="utf-8")

        self.assertEqual(server.detect_project_kind(root), "orchestrator")
        project = server.make_project(root)
        self.assertEqual(project["kind"], "orchestrator")
        self.assertEqual(project["name"], root.name)

    def test_detects_plain_git_repo_as_uninitialized(self):
        root = self.make_dir()
        (root / ".git").mkdir()

        self.assertEqual(server.detect_project_kind(root), "git-uninitialized")

    def test_rejects_non_git_non_orchestrator_directory(self):
        root = self.make_dir()

        with mock.patch("gui.server.is_inside_git_repo", return_value=False):
            with self.assertRaises(server.ApiError):
                server.detect_project_kind(root)

    def test_build_run_command_maps_options_and_clamps_rounds(self):
        root = Path("E:/example/project")
        command = server.build_run_command(
            root,
            {
                "maxRounds": 99,
                "skipTests": True,
                "allowNoTests": True,
                "skipCodexReview": True,
                "reviewCommand": "powershell -File scripts/reviewer.ps1",
            },
        )

        self.assertEqual(command[:5], ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"])
        self.assertIn(str(root / "scripts" / "run-claude.ps1"), command)
        self.assertIn("-MaxRounds", command)
        self.assertEqual(command[command.index("-MaxRounds") + 1], "10")
        self.assertIn("-SkipTests", command)
        self.assertIn("-AllowNoTests", command)
        self.assertIn("-SkipCodexReview", command)
        self.assertEqual(command[-2:], ["-ReviewCommand", "powershell -File scripts/reviewer.ps1"])

    @unittest.skipIf(shutil.which("powershell") is None, "PowerShell is required for run manager test")
    def test_run_manager_captures_fake_orchestrator_output(self):
        root = self.make_dir()
        (root / "scripts").mkdir()
        (root / "docs").mkdir()
        (root / "docs" / "PLAN.md").write_text("# Fake plan", encoding="utf-8")
        (root / "scripts" / "run-claude.ps1").write_text(
            "\n".join(
                [
                    "param(",
                    "  [int]$MaxRounds = 3,",
                    "  [switch]$SkipTests = $false,",
                    "  [switch]$SkipCodexReview = $false",
                    ")",
                    'Write-Host "fake orchestrator start"',
                    'Write-Host "rounds=$MaxRounds skipTests=$SkipTests skipReview=$SkipCodexReview"',
                    'Set-Content -Path "docs/IMPLEMENTATION_REPORT.md" -Value "# Fake report" -Encoding UTF8',
                    "exit 0",
                ]
            ),
            encoding="utf-8",
        )

        store = server.ProjectStore(root / ".gui" / "projects.json")
        project = store.add_project(root)
        runs = server.RunManager(store)
        run = runs.start(project, {"maxRounds": 2, "skipTests": True, "skipCodexReview": True})

        self.assertEqual(run["status"], "running")

        deadline = time.time() + 15
        snapshot = runs.snapshot()
        while time.time() < deadline:
            snapshot = runs.snapshot()
            if snapshot and snapshot["status"] != "running":
                break
            time.sleep(0.1)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["status"], "finished")
        self.assertEqual(snapshot["exitCode"], 0)
        self.assertEqual(snapshot["result"], "PASS")
        self.assertTrue(any("fake orchestrator start" in line for line in snapshot["logs"]))

        updated_project = store.get_project(project["id"])
        self.assertEqual(updated_project["lastResult"], "PASS")
        self.assertEqual(updated_project["lastExitCode"], 0)


class TaskApiCoreTests(unittest.TestCase):
    def make_project_store(self):
        root = server.ROOT / ".gui" / "test-tmp" / f"api-{uuid.uuid4().hex}"
        project = root / "project"
        project.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        project_store = server.ProjectStore(root / "projects.json")
        project_store.save_projects(
            [
                {
                    "id": "project1",
                    "name": "Project",
                    "path": str(project),
                    "kind": "git-uninitialized",
                }
            ]
        )
        task_store = TaskStore(root / "tasks")
        return root, project, project_store, task_store

    def create_waiting_task(self, project_store, task_store):
        with mock.patch("gui.server.assert_git_work_tree"), mock.patch("gui.server.assert_clean_work_tree"):
            return server.create_task(
                {
                    "projectId": "project1",
                    "title": "Task",
                    "description": "Do work",
                    "acceptance": "Pass",
                    "maxRounds": 2,
                },
                project_store,
                task_store,
            )

    def test_create_task_writes_task_and_initial_prompt(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)

        self.assertEqual(task.status, "WAITING_FOR_CLAUDE")
        self.assertTrue((task_store.task_dir(task.id) / "task.json").exists())
        self.assertTrue((task_store.task_dir(task.id) / "CLAUDE_IMPLEMENT_PROMPT.md").exists())

    def test_claude_completed_collects_artifacts_and_generates_codex_prompt(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "CLAUDE_WINDOW_STARTED"
        task_store.save(task)
        task_dir = task_store.task_dir(task.id)

        def fake_collect(_project_path, task_path, round_number):
            status_path = task_path / f"git_status_round_{round_number}.txt"
            diff_stat_path = task_path / f"git_diff_stat_round_{round_number}.txt"
            diff_path = task_path / f"git_diff_round_{round_number}.diff"
            status_path.write_text(" M app.py\n", encoding="utf-8")
            diff_stat_path.write_text(" app.py | 1 +\n", encoding="utf-8")
            diff_path.write_text("diff --git a/app.py b/app.py\n", encoding="utf-8")
            return GitArtifacts(status_path, diff_stat_path, diff_path, "", "", "")

        def fake_tests(_project_path, task_path, round_number, _command):
            path = task_path / f"test_results_round_{round_number}.txt"
            path.write_text("EXIT_CODE: 0\n", encoding="utf-8")
            return TestRunResult(["test"], 0, "EXIT_CODE: 0\n", path)

        with (
            mock.patch("gui.server.assert_git_work_tree"),
            mock.patch("gui.server.collect_git_artifacts", side_effect=fake_collect),
            mock.patch("gui.server.run_tests", side_effect=fake_tests),
        ):
            updated = server.complete_claude_task(task.id, project_store, task_store)

        self.assertEqual(updated.status, "WAITING_FOR_CODEX")
        self.assertTrue((task_dir / "CODEX_REVIEW_PROMPT.md").exists())
        prompt = (task_dir / "CODEX_REVIEW_PROMPT.md").read_text(encoding="utf-8")
        self.assertIn("Do not edit files. The launcher will save your final response", prompt)
        self.assertNotIn("Write the final structured review JSON", prompt)

    def test_codex_pass_enters_terminal_pass(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "CODEX_WINDOW_STARTED"
        task_store.save(task)
        (task_store.task_dir(task.id) / "CODEX_REVIEW.json").write_text(
            '{"status":"PASS","reviewed_at":"2026-06-11T00:00:00Z","findings":[]}',
            encoding="utf-8",
        )

        with mock.patch("gui.server.assert_git_work_tree"):
            updated = server.complete_codex_task(task.id, project_store, task_store)

        self.assertEqual(updated.status, "PASS")

    def test_codex_needs_fix_generates_next_round_prompt(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "CODEX_WINDOW_STARTED"
        task_store.save(task)
        task_dir = task_store.task_dir(task.id)
        (task_dir / "git_diff_round_1.diff").write_text("diff", encoding="utf-8")
        (task_dir / "test_results_round_1.txt").write_text("tests", encoding="utf-8")
        (task_dir / "CODEX_REVIEW.json").write_text(
            '{"status":"NEEDS_FIX","reviewed_at":"2026-06-11T00:00:00Z","findings":[{"id":"P1-1","severity":"P1","file":"app.py","description":"bug"}]}',
            encoding="utf-8",
        )

        with mock.patch("gui.server.assert_git_work_tree"):
            updated = server.complete_codex_task(task.id, project_store, task_store)

        self.assertEqual(updated.status, "WAITING_FOR_CLAUDE")
        self.assertEqual(updated.round, 2)
        self.assertTrue((task_dir / "FIX_PROMPT_ROUND_2.md").exists())

    def test_codex_invalid_json_fails_task_and_cancel_cancels_non_terminal(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "CODEX_WINDOW_STARTED"
        task_store.save(task)
        (task_store.task_dir(task.id) / "CODEX_REVIEW.json").write_text("{bad json", encoding="utf-8")

        with mock.patch("gui.server.assert_git_work_tree"):
            failed = server.complete_codex_task(task.id, project_store, task_store)
        self.assertEqual(failed.status, "FAILED")

        task2 = self.create_waiting_task(project_store, task_store)
        cancelled = server.cancel_task(task2.id, task_store)
        self.assertEqual(cancelled.status, "CANCELLED")

    def test_codex_missing_output_returns_to_waiting_for_retry(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "CODEX_WINDOW_STARTED"
        task_store.save(task)
        task_dir = task_store.task_dir(task.id)
        (task_dir / "codex_window_round_1.log").write_text("CLI exit code: 1\n", encoding="utf-8")

        with mock.patch("gui.server.assert_git_work_tree"):
            updated = server.complete_codex_task(task.id, project_store, task_store)

        self.assertEqual(updated.status, "WAITING_FOR_CODEX")
        self.assertIn("codex_window_round_1.log", [artifact["name"] for artifact in updated.artifacts])
        self.assertEqual(updated.history[-2]["event"], "CODEX_REVIEW_MISSING")

    def test_codex_stale_output_returns_to_waiting_for_retry(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "CODEX_WINDOW_STARTED"
        task_store.save(task)
        task_dir = task_store.task_dir(task.id)
        review_path = task_dir / "CODEX_REVIEW.json"
        marker_path = task_dir / "codex_output_started_round_1.txt"
        review_path.write_text('{"status":"BLOCKED","reviewed_at":"old","findings":[]}', encoding="utf-8")
        marker_path.write_text("marker", encoding="utf-8")
        old_time = marker_path.stat().st_mtime - 10
        os.utime(review_path, (old_time, old_time))

        with mock.patch("gui.server.assert_git_work_tree"):
            updated = server.complete_codex_task(task.id, project_store, task_store)

        self.assertEqual(updated.status, "WAITING_FOR_CODEX")
        self.assertEqual(updated.history[-2]["event"], "CODEX_REVIEW_MISSING")

    def test_running_task_cannot_be_archived_or_deleted(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "CLAUDE_WINDOW_STARTED"
        task_store.save(task)

        with self.assertRaises(server.ApiError):
            server.archive_task(task.id, task_store)
        with self.assertRaises(server.ApiError):
            server.move_task_to_trash(task.id, task_store)

        self.assertTrue(task_store.task_dir(task.id).exists())
        self.assertEqual(task_store.load(task.id).status, "CLAUDE_WINDOW_STARTED")

    def test_task_lifecycle_operations_write_audit_log(self):
        root, _project, project_store, task_store = self.make_project_store()
        audit_log = root / "audit.log"
        task = self.create_waiting_task(project_store, task_store)

        with mock.patch.object(server, "AUDIT_LOG_FILE", audit_log):
            archived = server.archive_task(task.id, task_store)
            self.assertIsNotNone(archived.archivedAt)
            restored = server.restore_archived_task(task.id, task_store)
            self.assertIsNone(restored.archivedAt)
            with mock.patch("pathlib.Path.rename", autospec=True, side_effect=fake_directory_rename):
                trashed = server.move_task_to_trash(task.id, task_store)
            self.assertIsNotNone(trashed.deletedAt)

            restore_task = Task.create(
                task_id="task_restore000000",
                project_id="project1",
                project_path=str(_project),
                title="Restore me",
                description="D",
                acceptance="A",
            )
            restore_task.deletedAt = "2026-06-12T00:00:00Z"
            trash_dir = task_store.trash_task_dir(restore_task.id)
            trash_dir.mkdir(parents=True)
            (trash_dir / "task.json").write_text(
                json.dumps(restore_task.to_dict(), ensure_ascii=False),
                encoding="utf-8",
            )
            with mock.patch("pathlib.Path.rename", autospec=True, side_effect=fake_directory_rename):
                restored_trash = server.restore_task_from_trash(restore_task.id, task_store)
            self.assertIsNone(restored_trash.deletedAt)

        entries = [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(
            [entry["action"] for entry in entries],
            ["task.archive", "task.restore_archive", "task.move_to_trash", "task.restore_trash"],
        )

    def test_remove_project_only_removes_registration_and_can_readd(self):
        root = server.ROOT / ".gui" / "test-tmp" / f"remove-{uuid.uuid4().hex}"
        project = root / "project"
        (project / ".git").mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        project_store = server.ProjectStore(root / "projects.json")
        task_store = TaskStore(root / "tasks")
        registered = project_store.add_project(project)
        audit_log = root / "audit.log"

        with mock.patch.object(server, "AUDIT_LOG_FILE", audit_log):
            removed = server.remove_project(registered["id"], project_store, task_store, server.RunManager(project_store))

        self.assertEqual(removed["id"], registered["id"])
        self.assertEqual(project_store.list_projects(), [])
        self.assertTrue(project.exists())
        self.assertEqual(project_store.add_project(project)["id"], registered["id"])
        entry = json.loads(audit_log.read_text(encoding="utf-8").strip())
        self.assertFalse(entry["details"]["localFilesDeleted"])

    def test_remove_project_blocked_when_project_has_running_task(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "CODEX_WINDOW_STARTED"
        task_store.save(task)

        with self.assertRaises(server.ApiError):
            server.remove_project("project1", project_store, task_store, server.RunManager(project_store))


    def test_empty_diff_after_claude_marks_task_failed(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "CLAUDE_WINDOW_STARTED"
        task_store.save(task)
        task_dir = task_store.task_dir(task.id)

        def fake_collect_empty(_project_path, task_path, round_number):
            status_path = task_path / f"git_status_round_{round_number}.txt"
            diff_stat_path = task_path / f"git_diff_stat_round_{round_number}.txt"
            diff_path = task_path / f"git_diff_round_{round_number}.diff"
            status_path.write_text("", encoding="utf-8")
            diff_stat_path.write_text("", encoding="utf-8")
            diff_path.write_text("", encoding="utf-8")
            return GitArtifacts(status_path, diff_stat_path, diff_path, "", "", "")

        with (
            mock.patch("gui.server.assert_git_work_tree"),
            mock.patch("gui.server.collect_git_artifacts", side_effect=fake_collect_empty),
        ):
            updated = server.complete_claude_task(task.id, project_store, task_store)

        self.assertEqual(updated.status, "FAILED")
        self.assertEqual(updated.stage, "no_changes")
        self.assertIsNone(updated.activeClient)
        self.assertEqual(updated.progress, 100)
        history_events = [h["event"] for h in updated.history]
        self.assertIn("NO_DIFF_DETECTED", history_events)
        self.assertFalse((task_dir / "CODEX_REVIEW_PROMPT.md").exists())

    def test_staged_only_changes_not_treated_as_no_diff(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "CLAUDE_WINDOW_STARTED"
        task_store.save(task)

        def fake_collect_staged_only(_project_path, task_path, round_number):
            status_path = task_path / f"git_status_round_{round_number}.txt"
            diff_stat_path = task_path / f"git_diff_stat_round_{round_number}.txt"
            diff_path = task_path / f"git_diff_round_{round_number}.diff"
            status_path.write_text("M  staged_file.py", encoding="utf-8")
            diff_stat_path.write_text(" staged_file.py | 5 +++++", encoding="utf-8")
            diff_path.write_text("", encoding="utf-8")
            return GitArtifacts(status_path, diff_stat_path, diff_path,
                              "M  staged_file.py", " staged_file.py | 5 +++++", "")

        with (
            mock.patch("gui.server.assert_git_work_tree"),
            mock.patch("gui.server.collect_git_artifacts", side_effect=fake_collect_staged_only),
            mock.patch("gui.server.run_tests") as mock_run_tests,
        ):
            mock_run_tests.return_value = mock.MagicMock(path=None)
            updated = server.complete_claude_task(task.id, project_store, task_store)

        self.assertNotEqual(updated.status, "FAILED")
        self.assertNotEqual(updated.stage, "no_changes")
        mock_run_tests.assert_called_once()

    def test_missing_codex_review_sets_consistent_progress_and_stage(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "CODEX_WINDOW_STARTED"
        task.progress = 60
        task.stage = "codex_running"
        task.activeClient = "codex"
        task_store.save(task)
        task_dir = task_store.task_dir(task.id)

        def fake_collect_git(_project_path, task_path, round_number):
            status_path = task_path / f"git_status_round_{round_number}.txt"
            diff_stat_path = task_path / f"git_diff_stat_round_{round_number}.txt"
            diff_path = task_path / f"git_diff_round_{round_number}.diff"
            status_path.write_text("M modified.py", encoding="utf-8")
            diff_stat_path.write_text(" modified.py | 3 +++", encoding="utf-8")
            diff_path.write_text("+code change", encoding="utf-8")
            return GitArtifacts(status_path, diff_stat_path, diff_path,
                              "M modified.py", " modified.py | 3 +++", "+code change")

        with (
            mock.patch("gui.server.assert_git_work_tree"),
            mock.patch("gui.server.collect_git_artifacts", side_effect=fake_collect_git),
            mock.patch("gui.server.run_tests") as mock_run_tests,
        ):
            mock_run_tests.return_value = mock.MagicMock(path=mock.MagicMock(name="test_result.txt"))
            updated = server.complete_codex_task(task.id, project_store, task_store)

        self.assertEqual(updated.status, "WAITING_FOR_CODEX")
        self.assertEqual(updated.progress, 50)
        self.assertEqual(updated.stage, "waiting_for_codex")
        self.assertIsNone(updated.activeClient)
        history_events = [h["event"] for h in updated.history]
        self.assertIn("CODEX_REVIEW_MISSING", history_events)

    def test_task_api_returns_progress_fields(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)

        d = task.to_dict()
        self.assertIn("progress", d)
        self.assertIn("stage", d)
        self.assertIn("activeClient", d)
        self.assertIn("lastActivityAt", d)
        self.assertEqual(d["progress"], 0)
        self.assertEqual(d["stage"], "created")
        self.assertIsNone(d["activeClient"])
        self.assertIsNotNone(d["lastActivityAt"])

        task.status = "CLAUDE_WINDOW_STARTED"
        task.progress = 20
        task.stage = "claude_running"
        task.activeClient = "claude"
        task.lastActivityAt = utc_now()
        task_store.save(task)

        reloaded = task_store.load(task.id)
        self.assertEqual(reloaded.progress, 20)
        self.assertEqual(reloaded.stage, "claude_running")
        self.assertEqual(reloaded.activeClient, "claude")
        self.assertIsNotNone(reloaded.lastActivityAt)


    def test_env_file_changed_during_claude_completion_clears_progress_fields(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "CLAUDE_WINDOW_STARTED"
        task.progress = 20
        task.stage = "claude_running"
        task.activeClient = "claude"
        task_store.save(task)

        with (
            mock.patch("gui.server.assert_git_work_tree"),
            mock.patch(
                "gui.server.collect_git_artifacts",
                side_effect=EnvFileChangedError("Working tree files are out of sync"),
            ),
        ):
            updated = server.complete_claude_task(task.id, project_store, task_store)

        self.assertEqual(updated.status, "FAILED")
        self.assertEqual(updated.stage, "git_collection_failed")
        self.assertIsNone(updated.activeClient)
        self.assertEqual(updated.progress, 20)
        self.assertIsNotNone(updated.lastActivityAt)

    def test_from_dict_legacy_claude_running_derives_fields_from_status(self):
        legacy = {
            "id": "task_legacy1",
            "projectId": "proj1",
            "projectPath": "/test",
            "title": "Legacy Claude Task",
            "description": "No new fields",
            "acceptance": "",
            "testCommand": "",
            "status": "CLAUDE_WINDOW_STARTED",
            "round": 1,
            "maxRounds": 3,
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "artifacts": [],
            "history": [],
        }
        task = Task.from_dict(legacy)
        self.assertEqual(task.progress, 20)
        self.assertEqual(task.stage, "claude_running")
        self.assertEqual(task.activeClient, "claude")
        self.assertIsNotNone(task.lastActivityAt)

    def test_from_dict_legacy_codex_running_derives_fields_from_status(self):
        legacy = {
            "id": "task_legacy2",
            "projectId": "proj1",
            "projectPath": "/test",
            "title": "Legacy Codex Task",
            "description": "No new fields",
            "acceptance": "",
            "testCommand": "",
            "status": "CODEX_WINDOW_STARTED",
            "round": 1,
            "maxRounds": 3,
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "artifacts": [],
            "history": [],
        }
        task = Task.from_dict(legacy)
        self.assertEqual(task.progress, 60)
        self.assertEqual(task.stage, "codex_running")
        self.assertEqual(task.activeClient, "codex")

    def test_from_dict_legacy_waiting_for_codex_derives_fields(self):
        legacy = {
            "id": "task_legacy3",
            "projectId": "proj1",
            "projectPath": "/test",
            "title": "Legacy Waiting Task",
            "description": "No new fields",
            "acceptance": "",
            "testCommand": "",
            "status": "WAITING_FOR_CODEX",
            "round": 1,
            "maxRounds": 3,
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "artifacts": [],
            "history": [],
        }
        task = Task.from_dict(legacy)
        self.assertEqual(task.progress, 50)
        self.assertEqual(task.stage, "waiting_for_codex")
        self.assertIsNone(task.activeClient)

    def test_from_dict_legacy_failed_derives_fields(self):
        legacy = {
            "id": "task_legacy4",
            "projectId": "proj1",
            "projectPath": "/test",
            "title": "Legacy Failed Task",
            "description": "No new fields",
            "acceptance": "",
            "testCommand": "",
            "status": "FAILED",
            "round": 1,
            "maxRounds": 3,
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "artifacts": [],
            "history": [],
        }
        task = Task.from_dict(legacy)
        self.assertEqual(task.progress, 100)
        self.assertEqual(task.stage, "no_changes")
        self.assertIsNone(task.activeClient)

    def test_from_dict_legacy_cancelled_derives_fields(self):
        legacy = {
            "id": "task_legacy5",
            "projectId": "proj1",
            "projectPath": "/test",
            "title": "Legacy Cancelled Task",
            "description": "No new fields",
            "acceptance": "",
            "testCommand": "",
            "status": "CANCELLED",
            "round": 1,
            "maxRounds": 3,
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "artifacts": [],
            "history": [],
        }
        task = Task.from_dict(legacy)
        self.assertEqual(task.progress, 100)
        self.assertEqual(task.stage, "cancelled")
        self.assertIsNone(task.activeClient)

    def test_from_dict_legacy_created_derives_fields(self):
        legacy = {
            "id": "task_legacy6",
            "projectId": "proj1",
            "projectPath": "/test",
            "title": "Legacy Created Task",
            "description": "No new fields",
            "acceptance": "",
            "testCommand": "",
            "status": "CREATED",
            "round": 1,
            "maxRounds": 3,
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "artifacts": [],
            "history": [],
        }
        task = Task.from_dict(legacy)
        self.assertEqual(task.progress, 0)
        self.assertEqual(task.stage, "created")
        self.assertIsNone(task.activeClient)

    def test_from_dict_preserves_explicit_fields_when_present(self):
        data = {
            "id": "task_explicit1",
            "projectId": "proj1",
            "projectPath": "/test",
            "title": "Explicit Task",
            "description": "Has new fields",
            "acceptance": "",
            "testCommand": "",
            "status": "CLAUDE_WINDOW_STARTED",
            "round": 1,
            "maxRounds": 3,
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "artifacts": [],
            "history": [],
            "progress": 45,
            "stage": "custom_stage",
            "activeClient": "claude",
            "lastActivityAt": "2026-01-01T12:00:00Z",
        }
        task = Task.from_dict(data)
        self.assertEqual(task.progress, 45)
        self.assertEqual(task.stage, "custom_stage")
        self.assertEqual(task.activeClient, "claude")
        self.assertEqual(task.lastActivityAt, "2026-01-01T12:00:00Z")

    def test_codex_completion_review_pass_clears_active_client(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "CODEX_WINDOW_STARTED"
        task.progress = 60
        task.stage = "codex_running"
        task.activeClient = "codex"
        task_store.save(task)
        task_dir = task_store.task_dir(task.id)

        marker_path = task_dir / f"codex_output_started_round_{task.round}.txt"
        marker_path.write_text(str(time.time() - 120), encoding="utf-8")
        time.sleep(0.1)
        review_path = task_dir / "CODEX_REVIEW.json"
        review_path.write_text(
            json.dumps({"status": "PASS", "reviewed_at": "2026-01-01T00:00:00Z", "findings": []}),
            encoding="utf-8",
        )

        updated = server.complete_codex_task(task.id, project_store, task_store)

        self.assertEqual(updated.status, "PASS")
        self.assertEqual(updated.progress, 100)
        self.assertEqual(updated.stage, "review_complete")
        self.assertIsNone(updated.activeClient)
        self.assertIsNotNone(updated.lastActivityAt)


def utc_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def fake_directory_rename(source, destination):
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return destination


if __name__ == "__main__":
    unittest.main()
