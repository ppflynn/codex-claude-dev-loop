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
from gui.orchestrator.state_machine import Status
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


class TerminalApiTests(unittest.TestCase):
    def make_store_and_task(self, task_id="task_termtest"):
        root = server.ROOT / ".gui" / "test-tmp" / f"term-{uuid.uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        task_store = TaskStore(root / "tasks")
        task = Task.create(
            task_id=task_id,
            project_id="project1",
            project_path=str(root / "project"),
            title="Terminal Task",
            description="Test terminal",
            acceptance="Pass",
        )
        task_store.save(task)
        return root, task_store, task

    def test_resolve_terminal_log_validates_client(self):
        _root, task_store, task = self.make_store_and_task()
        with self.assertRaises(server.ApiError) as ctx:
            server._resolve_terminal_log(task_store, task, "invalid_client")
        self.assertIn("Invalid terminal client", ctx.exception.message)

    def test_resolve_terminal_log_accepts_claude_and_codex(self):
        _root, task_store, task = self.make_store_and_task()
        claude_path = server._resolve_terminal_log(task_store, task, "claude")
        codex_path = server._resolve_terminal_log(task_store, task, "codex")
        self.assertEqual(claude_path.name, "claude_window_round_1.log")
        self.assertEqual(codex_path.name, "codex_window_round_1.log")
        self.assertIn("task_termtest", str(claude_path))

    def test_terminal_metadata_for_missing_log(self):
        _root, task_store, task = self.make_store_and_task()
        meta = server._terminal_metadata(task_store, task, "claude")
        self.assertEqual(meta["client"], "claude")
        self.assertEqual(meta["taskId"], "task_termtest")
        self.assertEqual(meta["round"], 1)
        self.assertEqual(meta["status"], "CREATED")
        self.assertFalse(meta["active"])
        self.assertFalse(meta["exists"])
        self.assertEqual(meta["size"], 0)
        self.assertIsNone(meta["updatedAt"])
        self.assertFalse(meta["finished"])
        self.assertIsNone(meta["exitCode"])
        self.assertIsNone(meta["lastLogUpdateAt"])

    def test_terminal_metadata_for_existing_log(self):
        _root, task_store, task = self.make_store_and_task()
        log_path = task_store.task_dir(task.id) / "claude_window_round_1.log"
        log_path.write_text("Hello from Claude\n", encoding="utf-8")
        task.status = Status.CLAUDE_WINDOW_STARTED
        task.activeClient = "claude"
        meta = server._terminal_metadata(task_store, task, "claude")
        self.assertTrue(meta["exists"])
        self.assertGreater(meta["size"], 0)
        self.assertEqual(meta["logName"], "claude_window_round_1.log")
        self.assertEqual(meta["round"], 1)
        self.assertEqual(meta["status"], "CLAUDE_WINDOW_STARTED")
        self.assertTrue(meta["active"])
        self.assertIsNotNone(meta["updatedAt"])
        self.assertFalse(meta["finished"])
        self.assertIsNone(meta["exitCode"])
        self.assertIsNotNone(meta["lastLogUpdateAt"])

    def test_terminal_metadata_uses_task_round(self):
        _root, task_store, task = self.make_store_and_task()
        task.round = 3
        meta = server._terminal_metadata(task_store, task, "codex")
        self.assertEqual(meta["logName"], "codex_window_round_3.log")
        self.assertEqual(meta["round"], 3)

    def test_terminal_api_endpoint_missing_task_returns_404(self):
        handler = _make_handler("GET", "/api/tasks/task_nonexistent/terminal/claude")
        handler.do_GET()
        self.assertEqual(handler._status, 404)

    def test_terminal_api_endpoint_invalid_client_returns_400(self):
        root = server.ROOT / ".gui" / "test-tmp" / f"termapi-{uuid.uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        task_store = TaskStore(root / "tasks")
        task = Task.create(
            task_id="task_termapi01",
            project_id="project1",
            project_path=str(root / "project"),
            title="T", description="D", acceptance="A",
        )
        task_store.save(task)

        with mock.patch.object(server.GuiHandler, "tasks", task_store):
            handler = _make_handler("GET", f"/api/tasks/{task.id}/terminal/evil")
            handler.do_GET()
            self.assertEqual(handler._status, 400)

    def test_terminal_api_endpoint_returns_metadata(self):
        root = server.ROOT / ".gui" / "test-tmp" / f"termapi-{uuid.uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        task_store = TaskStore(root / "tasks")
        task = Task.create(
            task_id="task_termapi02",
            project_id="project1",
            project_path=str(root / "project"),
            title="T", description="D", acceptance="A",
        )
        task_store.save(task)
        log_path = task_store.task_dir(task.id) / "claude_window_round_1.log"
        log_path.write_text("test output\n", encoding="utf-8")

        with mock.patch.object(server.GuiHandler, "tasks", task_store):
            handler = _make_handler("GET", f"/api/tasks/{task.id}/terminal/claude")
            handler.do_GET()
            self.assertEqual(handler._status, 200)
            body = json.loads(handler._body())
            self.assertTrue(body["exists"])
            self.assertEqual(body["client"], "claude")
            self.assertEqual(body["logName"], "claude_window_round_1.log")
            self.assertEqual(body["round"], 1)
            self.assertEqual(body["status"], "CREATED")
            self.assertFalse(body["active"])
            self.assertIsNotNone(body["updatedAt"])

    def test_terminal_metadata_active_client_reflects_running_state(self):
        """Active flag should be true only for the client matching activeClient in running status."""
        _root, task_store, task = self.make_store_and_task()
        task.status = Status.CLAUDE_WINDOW_STARTED
        task.activeClient = "claude"
        claude_meta = server._terminal_metadata(task_store, task, "claude")
        codex_meta = server._terminal_metadata(task_store, task, "codex")
        self.assertTrue(claude_meta["active"])
        self.assertFalse(codex_meta["active"])

    def test_terminal_stream_captured_round_survives_round_advance(self):
        """P2-1 regression: stream must keep reading original round log after task.round advances."""
        from unittest import mock as umock

        _root, task_store, task = self.make_store_and_task()
        task.status = Status.CODEX_WINDOW_STARTED
        task.round = 1
        task_store.save(task)

        log_path = task_store.task_dir(task.id) / "codex_window_round_1.log"
        log_path.write_text("line1\nline2\n", encoding="utf-8")

        # Pre-create the round-2 log with poison content so we can detect if the
        # stream ever switches to it after the round advances.
        log_path_r2 = task_store.task_dir(task.id) / "codex_window_round_2.log"
        log_path_r2.write_text("WRONG-ROUND\n", encoding="utf-8")

        wfile = _FakeWfile(None)
        flush_count = [0]

        def flush():
            flush_count[0] += 1

        sleep_count = [0]

        def fake_sleep(_):
            sleep_count[0] += 1
            if sleep_count[0] == 1:
                # After the first read, advance the round and append more data
                # to the *original* round-1 log.
                task.round = 2
                task_store.save(task)
                with log_path.open("a", encoding="utf-8") as f:
                    f.write("line3\n")
            elif sleep_count[0] == 2:
                # After the second read, mark the task complete.
                task.status = Status.WAITING_FOR_CLAUDE
                task_store.save(task)

        with umock.patch("time.sleep", fake_sleep):
            server._terminal_stream(task_store, task.id, "codex", wfile, flush)

        output = b"".join(wfile._buf).decode("utf-8")
        self.assertIn("line1", output)
        self.assertIn("line2", output)
        self.assertIn("line3", output)
        self.assertNotIn("WRONG-ROUND", output)

    def test_terminal_stream_multibyte_no_duplicate_or_corrupt(self):
        """P2-1: appending multibyte text mid-stream emits each line exactly once."""
        from unittest import mock as umock

        _root, task_store, task = self.make_store_and_task()
        task.status = Status.CODEX_WINDOW_STARTED
        task.round = 1
        task_store.save(task)

        log_path = task_store.task_dir(task.id) / "codex_window_round_1.log"
        # Start with ASCII, then append multibyte Chinese text between polls
        log_path.write_text("line1\n", encoding="utf-8")

        wfile = _FakeWfile(None)

        def flush():
            pass

        sleep_count = [0]
        content_plan = [
            "第2行中文内容\n",          # multibyte append after first read
            "line3 with trailing 中文\n",
        ]

        def fake_sleep(_):
            sleep_count[0] += 1
            idx = sleep_count[0] - 1
            if idx < len(content_plan):
                with log_path.open("ab") as f:
                    f.write(content_plan[idx].encode("utf-8"))
            elif idx == len(content_plan):
                task.status = Status.WAITING_FOR_CLAUDE
                task_store.save(task)

        with umock.patch("time.sleep", fake_sleep):
            server._terminal_stream(task_store, task.id, "codex", wfile, flush)

        output = b"".join(wfile._buf).decode("utf-8")
        self.assertIn("line1", output)
        self.assertIn("第2行中文内容", output)
        self.assertIn("line3 with trailing 中文", output)
        # Each line must appear exactly once across SSE data payloads
        self.assertEqual(output.count("line1"), 1)
        self.assertEqual(output.count("第2行中文内容"), 1)
        self.assertEqual(output.count("line3 with trailing 中文"), 1)

    def test_terminal_stream_split_multibyte_character_no_corruption(self):
        """P2-1: a UTF-8 character split across poll cycles is emitted correctly once."""
        from unittest import mock as umock

        _root, task_store, task = self.make_store_and_task()
        task.status = Status.CODEX_WINDOW_STARTED
        task.round = 1
        task_store.save(task)

        log_path = task_store.task_dir(task.id) / "codex_window_round_1.log"
        # "中" is \xe4\xb8\xad (3 bytes). Write first 2 bytes now.
        log_path.write_bytes(b"prefix:")

        wfile = _FakeWfile(None)

        def flush():
            pass

        sleep_count = [0]
        plan = [
            b"\xe4\xb8",           # first 2 of 3 bytes for "中" — split across boundary
            b"\xad\n",             # 3rd byte + newline
        ]

        def fake_sleep(_):
            sleep_count[0] += 1
            idx = sleep_count[0] - 1
            if idx < len(plan):
                with log_path.open("ab") as f:
                    f.write(plan[idx])
            elif idx == len(plan):
                task.status = Status.WAITING_FOR_CLAUDE
                task_store.save(task)

        with umock.patch("time.sleep", fake_sleep):
            server._terminal_stream(task_store, task.id, "codex", wfile, flush)

        output = b"".join(wfile._buf).decode("utf-8")
        self.assertIn("中", output)
        self.assertEqual(output.count("中"), 1)
        # With incremental decoder, no replacement characters should appear for
        # the split sequence.
        self.assertNotIn("�", output)

    def test_terminal_metadata_parses_cli_exit_code_zero(self):
        """Metadata should report finished=true, exitCode=0 when log contains CLI exit code: 0."""
        _root, task_store, task = self.make_store_and_task()
        log_path = task_store.task_dir(task.id) / "claude_window_round_1.log"
        log_path.write_text("some output\nCLI exit code: 0\nmore output\n", encoding="utf-8")
        meta = server._terminal_metadata(task_store, task, "claude")
        self.assertTrue(meta["finished"])
        self.assertEqual(meta["exitCode"], 0)

    def test_terminal_metadata_parses_nonzero_exit_code(self):
        """Metadata should report finished=true with the correct non-zero exit code."""
        _root, task_store, task = self.make_store_and_task()
        log_path = task_store.task_dir(task.id) / "codex_window_round_1.log"
        log_path.write_text("error output\nCLI exit code: 3\n", encoding="utf-8")
        meta = server._terminal_metadata(task_store, task, "codex")
        self.assertTrue(meta["finished"])
        self.assertEqual(meta["exitCode"], 3)

    def test_sse_done_event_includes_exit_code(self):
        """SSE done event should include exitCode when log contains CLI exit code."""
        from unittest import mock as umock

        _root, task_store, task = self.make_store_and_task()
        task.status = Status.CODEX_WINDOW_STARTED
        task.round = 1
        task_store.save(task)

        log_path = task_store.task_dir(task.id) / "codex_window_round_1.log"
        log_path.write_text("line1\nCLI exit code: 5\n", encoding="utf-8")

        wfile = _FakeWfile(None)

        def flush():
            pass

        with umock.patch("time.sleep", lambda _: None):
            server._terminal_stream(task_store, task.id, "codex", wfile, flush)

        output = b"".join(wfile._buf).decode("utf-8")
        # Find the done event
        done_payload = None
        for line in output.splitlines():
            if line.startswith("data: "):
                data = json.loads(line[6:])
                if data.get("done"):
                    done_payload = data
                    break
        self.assertIsNotNone(done_payload)
        self.assertTrue(done_payload["done"])
        self.assertEqual(done_payload["exitCode"], 5)

    def test_terminal_metadata_ignores_non_sentinel_text(self):
        """Metadata should NOT flag finished when CLI exit code: appears as embedded text."""
        _root, task_store, task = self.make_store_and_task()
        log_path = task_store.task_dir(task.id) / "claude_window_round_1.log"
        log_path.write_text("The CLI exit code: 5 was checked in the report\n", encoding="utf-8")
        meta = server._terminal_metadata(task_store, task, "claude")
        self.assertFalse(meta["finished"])
        self.assertIsNone(meta["exitCode"])

    def test_terminal_metadata_ignores_fallback_when_active(self):
        """Metadata must NOT flag finished when non-sentinel text ends with CLI exit code: N on an active/running task."""
        _root, task_store, task = self.make_store_and_task()
        task.status = "CLAUDE_WINDOW_STARTED"
        task.activeClient = "claude"
        task_store.save(task)
        log_path = task_store.task_dir(task.id) / "claude_window_round_1.log"
        # "Expected CLI exit code: 0" ends with the sentinel pattern — the
        # fallback re.search with $ would match, but active=True gates it.
        log_path.write_text("Expected CLI exit code: 0\n", encoding="utf-8")
        meta = server._terminal_metadata(task_store, task, "claude")
        self.assertFalse(meta["finished"])
        self.assertIsNone(meta["exitCode"])

    def test_terminal_metadata_uses_last_sentinel(self):
        """Metadata should use the last CLI exit code line when multiple exist."""
        _root, task_store, task = self.make_store_and_task()
        log_path = task_store.task_dir(task.id) / "claude_window_round_1.log"
        log_path.write_text("CLI exit code: 0\nsome output\nCLI exit code: 7\n", encoding="utf-8")
        meta = server._terminal_metadata(task_store, task, "claude")
        self.assertTrue(meta["finished"])
        self.assertEqual(meta["exitCode"], 7)

    def test_terminal_stream_sentinel_split_across_reads(self):
        """SSE should detect CLI exit code sentinel when split across file writes."""
        from unittest import mock as umock

        _root, task_store, task = self.make_store_and_task()
        task.status = Status.CODEX_WINDOW_STARTED
        task.round = 1
        task_store.save(task)

        log_path = task_store.task_dir(task.id) / "codex_window_round_1.log"
        log_path.write_text("", encoding="utf-8")

        wfile = _FakeWfile(None)

        def flush():
            pass

        sleep_count = [0]
        plan = [
            "prefix\nCLI exit ",      # partial sentinel line
            "code: 5\n",               # completes the sentinel
        ]

        def fake_sleep(_):
            sleep_count[0] += 1
            idx = sleep_count[0] - 1
            if idx < len(plan):
                with log_path.open("ab") as f:
                    f.write(plan[idx].encode("utf-8"))
            elif idx == len(plan):
                task.status = Status.WAITING_FOR_CLAUDE
                task_store.save(task)

        with umock.patch("time.sleep", fake_sleep):
            server._terminal_stream(task_store, task.id, "codex", wfile, flush)

        output = b"".join(wfile._buf).decode("utf-8")
        done_payload = None
        for line in output.splitlines():
            if line.startswith("data: "):
                data = json.loads(line[6:])
                if data.get("done"):
                    done_payload = data
                    break
        self.assertIsNotNone(done_payload)
        self.assertTrue(done_payload["done"])
        self.assertEqual(done_payload["exitCode"], 5)

    def test_terminal_stream_ignores_embedded_sentinel(self):
        """SSE only matches CLI exit code: on its own line, not embedded in other text."""
        from unittest import mock as umock

        _root, task_store, task = self.make_store_and_task()
        task.status = Status.CODEX_WINDOW_STARTED
        task.round = 1
        task_store.save(task)

        log_path = task_store.task_dir(task.id) / "codex_window_round_1.log"
        # Embedded text on one line, real sentinel on its own line
        log_path.write_text("Result: CLI exit code: 3 was returned\nCLI exit code: 0\n", encoding="utf-8")

        wfile = _FakeWfile(None)

        def flush():
            pass

        with umock.patch("time.sleep", lambda _: None):
            server._terminal_stream(task_store, task.id, "codex", wfile, flush)

        output = b"".join(wfile._buf).decode("utf-8")
        done_payload = None
        for line in output.splitlines():
            if line.startswith("data: "):
                data = json.loads(line[6:])
                if data.get("done"):
                    done_payload = data
                    break
        self.assertIsNotNone(done_payload)
        self.assertTrue(done_payload["done"])
        # The embedded text "Result: CLI exit code: 3 was returned" must NOT match
        # (anchored to ^CLI exit code:). The real sentinel on its own line gives exitCode=0.
        self.assertEqual(done_payload["exitCode"], 0)

    def test_terminal_metadata_sentinel_without_preceding_newline(self):
        """Metadata detects CLI exit code when appended directly after CLI output without newline."""
        _root, task_store, task = self.make_store_and_task()
        log_path = task_store.task_dir(task.id) / "claude_window_round_1.log"
        # Simulate: CLI's last output chunk had no trailing newline, sentinel glued directly
        log_path.write_text("final outputCLI exit code: 0\n", encoding="utf-8")
        meta = server._terminal_metadata(task_store, task, "claude")
        self.assertTrue(meta["finished"])
        self.assertEqual(meta["exitCode"], 0)

    def test_terminal_stream_sentinel_without_preceding_newline(self):
        """SSE ignores CLI exit code sentinel when not on its own line (glued to output).

        The launcher now guarantees a leading newline before the sentinel, so the SSE
        parser only matches ``^CLI exit code:`` at line start. A sentinel glued to the
        preceding output without a newline must NOT trigger a done event.
        """
        from unittest import mock as umock

        _root, task_store, task = self.make_store_and_task()
        task.status = Status.CLAUDE_WINDOW_STARTED
        task.round = 1
        task_store.save(task)

        log_path = task_store.task_dir(task.id) / "claude_window_round_1.log"
        # Simulate: CLI's last output chunk had no trailing newline (old launcher bug)
        log_path.write_text("doneCLI exit code: 2\n", encoding="utf-8")

        wfile = _FakeWfile(None)

        def flush():
            pass

        sleep_count = [0]

        def fake_sleep(_):
            sleep_count[0] += 1
            if sleep_count[0] == 2:
                # After the stream has read the content (and NOT detected the sentinel),
                # transition the task out of running to stop the stream cleanly.
                task.status = Status.WAITING_FOR_CLAUDE
                task_store.save(task)

        with umock.patch("time.sleep", fake_sleep):
            server._terminal_stream(task_store, task.id, "claude", wfile, flush)

        output = b"".join(wfile._buf).decode("utf-8")
        done_payload = None
        for line in output.splitlines():
            if line.startswith("data: "):
                data = json.loads(line[6:])
                if data.get("done"):
                    done_payload = data
                    break
        self.assertIsNotNone(done_payload)
        self.assertTrue(done_payload["done"])
        # The glued sentinel must NOT be detected — done event has no exitCode
        self.assertNotIn("exitCode", done_payload)

    def test_terminal_stream_endpoint_missing_log_still_responds(self):
        root = server.ROOT / ".gui" / "test-tmp" / f"termstream-{uuid.uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        task_store = TaskStore(root / "tasks")
        task = Task.create(
            task_id="task_termstrm1",
            project_id="project1",
            project_path=str(root / "project"),
            title="T", description="D", acceptance="A",
        )
        task_store.save(task)

        with mock.patch.object(server.GuiHandler, "tasks", task_store):
            handler = _make_handler("GET", f"/api/tasks/{task.id}/terminal/claude/stream")
            handler.do_GET()
            self.assertEqual(handler._status, 200)
            self.assertIn("text/event-stream", handler._content_type())


def _make_handler(method, path, body=None):
    """Create a GuiHandler with headers wired for testing."""
    handler = server.GuiHandler.__new__(server.GuiHandler)
    handler.command = method
    handler.path = path
    handler.headers = {}
    handler._status = None
    handler._response_body = None
    handler._response_headers = {}

    original_send_response = handler.send_response

    def send_response(code):
        handler._status = code

    handler.send_response = send_response

    def send_header(key, value):
        handler._response_headers[key] = value

    handler.send_header = send_header

    def end_headers():
        pass

    handler.end_headers = end_headers

    handler.wfile = _FakeWfile(handler)

    if body:
        handler.rfile = _FakeRfile(body)
    else:
        handler.rfile = _FakeRfile("")

    return handler


class _FakeWfile:
    def __init__(self, handler):
        self._handler = handler
        self._buf = []

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._buf.append(data)

    def flush(self):
        pass


class _FakeRfile:
    def __init__(self, content):
        self._content = content.encode("utf-8") if isinstance(content, str) else content

    def read(self, length):
        return self._content[:length]


def _body(self):
    if hasattr(self, "_response_body") and self._response_body is not None:
        return self._response_body
    if self.wfile and self.wfile._buf:
        return b"".join(self.wfile._buf).decode("utf-8")
    return ""

server.GuiHandler._body = _body


def _content_type(self):
    return self._response_headers.get("Content-Type", "")

server.GuiHandler._content_type = _content_type


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
