import shutil
import os
import subprocess
import sys
import time
import unittest
import uuid
import json
from http import HTTPStatus
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui import server
from gui.orchestrator.git_tools import GitArtifacts, EnvFileChangedError, GitError
from gui.orchestrator.models import Task
from gui.orchestrator.state_machine import Status
from gui.orchestrator.store import TaskStore
from gui.orchestrator.test_runner import TestRunResult


def materialise_mock_merge(result):
    """Return a merge mock that also fulfils the durable-journal contract."""
    def effect(main_path, source_branch, *args, **kwargs):
        journal = kwargs["recovery_journal"]
        old_head = subprocess.run(
            ["git", "-C", str(main_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        journal.write(
            phase="materialised",
            task_id=kwargs["task_id"],
            task_round=kwargs["task_round"],
            primary_path=str(main_path),
            primary_identity=kwargs["primary_identity"],
            expected_old_head=old_head,
            new_merge_commit_sha=result["mergeCommitSha"],
            source_commit_sha=kwargs.get("expected_commit_sha"),
            reviewed_base_sha=kwargs.get("expected_base_sha"),
            source_branch=source_branch,
            target_branch=result["mergeTargetBranch"],
        )
        return dict(result)

    return effect


class GuiServerTests(unittest.TestCase):
    def make_dir(self):
        temp_root = server.ROOT / ".gui" / "test-tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        path = temp_root / f"case-{uuid.uuid4().hex}"
        path.mkdir()
        self.addCleanup(lambda: shutil.rmtree(path, ignore_errors=True))
        return path

    def test_frontend_partial_worktree_uses_top_level_path_without_selection(self):
        source = (server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        start = source.index("data.registeredAutomatically === false")
        end = source.index("if (data?.project?.id)", start)
        partial = source[start:end]
        self.assertLess(partial.index("data.path"), partial.index("data.project.path"))
        self.assertIn("data.recoveryInstructions", partial)
        self.assertNotIn("await selectProject(", partial)
        self.assertIn("CAS update-ref", source)
        self.assertIn("任务元数据和审计落盘", source)
        self.assertNotIn("git merge --no-ff", source)

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
        self.assertEqual(command[command.index("-MaxRounds") + 1], "15")
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

    def test_launch_claude_rejects_non_waiting_state_before_opening_window(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = Status.WAITING_FOR_CODEX
        task_store.save(task)

        with mock.patch("gui.server.ClaudeCliWindowAdapter") as adapter:
            with self.assertRaises(server.StateTransitionError):
                server.launch_claude_task(task.id, project_store, task_store)

        adapter.assert_not_called()

    def test_launch_codex_rejects_stale_state_before_writing_marker_or_opening_window(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = Status.CLAUDE_WINDOW_STARTED
        task_store.save(task)
        task_dir = task_store.task_dir(task.id)
        (task_dir / "CODEX_REVIEW_PROMPT.md").write_text("stale prompt", encoding="utf-8")

        with mock.patch("gui.server.CodexCliWindowAdapter") as adapter:
            with self.assertRaises(server.StateTransitionError):
                server.launch_codex_task(task.id, project_store, task_store)

        adapter.assert_not_called()
        self.assertFalse((task_dir / "codex_output_started_round_1.txt").exists())

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
        # Runtime Terminal Progress Protocol must be injected into the Codex prompt
        self.assertIn("Runtime Terminal Progress Protocol", prompt)
        self.assertIn("::task-status{phase=\"<phase>\" message=\"<message>\"}", prompt)
        # Codex final response must remain pure JSON
        self.assertIn("single JSON object", prompt)
        self.assertIn("MUST NOT contain `::task-status` events", prompt)

    def test_codex_pass_enters_terminal_pass(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "CODEX_WINDOW_STARTED"
        task.reviewedRound = task.round
        task.reviewedHeadSha = "h"
        task.reviewedStatusHash = "s"
        task.reviewedDiffHash = "d"
        task.reviewedTreeSha = "t"
        task_store.save(task)
        (task_store.task_dir(task.id) / "CODEX_REVIEW.json").write_text(
            '{"status":"PASS","reviewed_at":"2026-06-11T00:00:00Z","findings":[]}',
            encoding="utf-8",
        )

        with (
            mock.patch("gui.server.assert_git_work_tree"),
            mock.patch(
                "gui.server.compute_review_snapshot",
                return_value={"headSha": "h", "statusHash": "s", "diffHash": "d", "treeSha": "t"},
            ),
        ):
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

    def test_commit_task_rejects_non_pass_task(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        # status is WAITING_FOR_CLAUDE, not PASS
        with self.assertRaises(server.ApiError) as ctx:
            server.commit_task_changes(task.id, {"message": "msg"}, project_store, task_store)
        self.assertEqual(ctx.exception.status, 409)

    def test_commit_task_rejects_running_task(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "CLAUDE_WINDOW_STARTED"
        task_store.save(task)
        with self.assertRaises(server.ApiError):
            server.commit_task_changes(task.id, {"message": "msg"}, project_store, task_store)

    def test_commit_task_records_commit_metadata_on_success(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.reviewedRound = task.round
        task.reviewedHeadSha = "headsha1"
        task.reviewedStatusHash = "statushash1"
        task.reviewedDiffHash = "diffhash1"
        task.reviewedTreeSha = "treesha1"
        task_store.save(task)
        with mock.patch("gui.server.controlled_commit", return_value={
            "commitSha": "abc123def456",
            "commitShortSha": "abc123d",
            "commitMessage": "feat: work",
        }):
            updated = server.commit_task_changes(task.id, {"message": "feat: work"}, project_store, task_store)
        self.assertEqual(updated.commitSha, "abc123def456")
        self.assertEqual(updated.commitShortSha, "abc123d")
        self.assertEqual(updated.commitMessage, "feat: work")
        self.assertIsNotNone(updated.committedAt)
        self.assertEqual(updated.stage, "committed")
        events = [h["event"] for h in updated.history]
        self.assertIn("COMMITTED", events)

    def test_commit_task_failure_does_not_mark_committed(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.reviewedRound = task.round
        task.reviewedHeadSha = "headsha1"
        task.reviewedStatusHash = "statushash1"
        task.reviewedDiffHash = "diffhash1"
        task.reviewedTreeSha = "treesha1"
        task_store.save(task)
        with mock.patch("gui.server.controlled_commit", side_effect=server.CommitError("no changes")):
            with self.assertRaises(server.ApiError):
                server.commit_task_changes(task.id, {"message": "msg"}, project_store, task_store)
        reloaded = task_store.load(task.id)
        self.assertIsNone(reloaded.commitSha)
        events = [h["event"] for h in reloaded.history]
        self.assertIn("COMMIT_BLOCKED", events)

    def test_commit_task_rejects_already_committed(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.commitSha = "abc"
        task_store.save(task)
        with self.assertRaises(server.ApiError):
            server.commit_task_changes(task.id, {"message": "msg"}, project_store, task_store)

    def test_merge_task_records_merge_metadata_on_success(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.commitSha = "abc"
        task.worktreeBranch = "feature/x"
        # Codex P1-3 round 19: GUI merge now requires reviewedRound ==
        # round and non-empty reviewedHeadSha before invoking
        # controlled_merge_to_main.
        task.reviewedRound = task.round
        task.reviewedHeadSha = "basesha1"
        task_store.save(task)
        # Inject two projects in the store so the primary lookup succeeds
        project_store.save_projects(
            [
                {
                    "id": "primary1",
                    "name": "Primary",
                    "path": str(_project),
                    "kind": "git-uninitialized",
                    "worktreeType": "primary",
                    "repoId": "repo_xyz",
                    "available": True,
                },
                {
                    "id": "project1",
                    "name": "Project",
                    "path": str(_project),
                    "kind": "git-uninitialized",
                    "worktreeType": "worktree",
                    "repoId": "repo_xyz",
                    "branch": "feature/x",
                    "available": True,
                },
            ]
        )
        with mock.patch("gui.server.controlled_merge_to_main", side_effect=materialise_mock_merge({
            "mergeCommitSha": "deadbeef",
            "mergeShortSha": "deadbee",
            "mergeTargetBranch": "master",
            "mergeSourceBranch": "feature/x",
        })):
            updated = server.merge_task_to_main(task.id, project_store, task_store)
        self.assertEqual(updated.mergeCommitSha, "deadbeef")
        self.assertEqual(updated.mergeTargetBranch, "master")
        self.assertEqual(updated.mergeSourceBranch, "feature/x")
        self.assertIsNotNone(updated.mergedAt)
        events = [h["event"] for h in updated.history]
        self.assertIn("MERGED", events)

    def test_merge_task_rejects_uncommitted_task(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task_store.save(task)
        with self.assertRaises(server.ApiError):
            server.merge_task_to_main(task.id, project_store, task_store)

    def test_merge_task_blocks_when_reviewed_snapshot_missing(self):
        # Codex P1-3 round 19: GUI merge must NEVER invoke
        # ``controlled_merge_to_main`` when ``reviewedHeadSha`` is
        # missing, regardless of ``commitSha`` state.  The lower-level
        # merge API has an optional ``expected_base_sha`` for
        # backwards compatibility, but the GUI path must never use
        # compatibility mode.
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.commitSha = "abc"
        task.worktreeBranch = "feature/x"
        # reviewedRound / reviewedHeadSha left unset → must block.
        task_store.save(task)
        with mock.patch("gui.server.controlled_merge_to_main") as merge_mock:
            with self.assertRaises(server.ApiError) as ctx:
                server.merge_task_to_main(task.id, project_store, task_store)
        self.assertEqual(ctx.exception.status, 409)
        merge_mock.assert_not_called()
        reloaded = task_store.load(task.id)
        events = [h["event"] for h in reloaded.history]
        self.assertIn("MERGE_BLOCKED", events)

    def test_merge_task_blocks_when_reviewed_round_is_stale(self):
        # Codex P1-3 round 19: ``reviewedRound`` must equal
        # ``task.round``.  A snapshot captured in an earlier round
        # does not cover the current round's committed change.
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.commitSha = "abc"
        task.worktreeBranch = "feature/x"
        task.round = 2
        task.reviewedRound = 1
        task.reviewedHeadSha = "basesha1"
        task_store.save(task)
        with mock.patch("gui.server.controlled_merge_to_main") as merge_mock:
            with self.assertRaises(server.ApiError) as ctx:
                server.merge_task_to_main(task.id, project_store, task_store)
        self.assertEqual(ctx.exception.status, 409)
        merge_mock.assert_not_called()
        reloaded = task_store.load(task.id)
        events = [h["event"] for h in reloaded.history]
        self.assertIn("MERGE_BLOCKED", events)

    def test_merge_task_blocks_when_reviewed_head_sha_empty(self):
        # Codex P1-3 round 19: ``reviewedHeadSha`` must be non-empty.
        # An empty value would silently skip the lower-level
        # reachability + sole-parent checks.
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.commitSha = "abc"
        task.worktreeBranch = "feature/x"
        task.reviewedRound = task.round
        task.reviewedHeadSha = ""
        task_store.save(task)
        with mock.patch("gui.server.controlled_merge_to_main") as merge_mock:
            with self.assertRaises(server.ApiError) as ctx:
                server.merge_task_to_main(task.id, project_store, task_store)
        self.assertEqual(ctx.exception.status, 409)
        merge_mock.assert_not_called()
        reloaded = task_store.load(task.id)
        events = [h["event"] for h in reloaded.history]
        self.assertIn("MERGE_BLOCKED", events)

    def test_merge_task_failure_does_not_mark_merged(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.commitSha = "abc"
        task.worktreeBranch = "feature/x"
        # Codex P1-3 round 19: GUI merge requires reviewed baseline.
        task.reviewedRound = task.round
        task.reviewedHeadSha = "basesha1"
        task_store.save(task)
        project_store.save_projects(
            [
                {
                    "id": "primary1",
                    "name": "Primary",
                    "path": str(_project),
                    "kind": "git-uninitialized",
                    "worktreeType": "primary",
                    "repoId": "repo_xyz",
                    "available": True,
                },
                {
                    "id": "project1",
                    "name": "Project",
                    "path": str(_project),
                    "kind": "git-uninitialized",
                    "worktreeType": "worktree",
                    "repoId": "repo_xyz",
                    "branch": "feature/x",
                    "available": True,
                },
            ]
        )
        with mock.patch("gui.server.controlled_merge_to_main", side_effect=server.MergeError("conflict")):
            with self.assertRaises(server.ApiError):
                server.merge_task_to_main(task.id, project_store, task_store)
        reloaded = task_store.load(task.id)
        self.assertIsNone(reloaded.mergeCommitSha)
        self.assertIsNone(reloaded.mergedAt)
        events = [h["event"] for h in reloaded.history]
        self.assertIn("MERGE_BLOCKED", events)

    def test_merge_task_records_git_error_as_blocked_history(self):
        # Codex P2-1 round 14: when ``controlled_merge_to_main`` raises
        # the underlying ``GitError`` (e.g. from ``is_ancestor``,
        # ``get_commit_parents``, ``git_status``), the service must
        # still record a ``MERGE_BLOCKED`` history event and surface a
        # consistent 409 ``ApiError``.
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.commitSha = "abc"
        task.worktreeBranch = "feature/x"
        # Codex P1-3 round 19: GUI merge requires reviewed baseline.
        task.reviewedRound = task.round
        task.reviewedHeadSha = "basesha1"
        task_store.save(task)
        project_store.save_projects(
            [
                {
                    "id": "primary1",
                    "name": "Primary",
                    "path": str(_project),
                    "kind": "git-uninitialized",
                    "worktreeType": "primary",
                    "repoId": "repo_xyz",
                    "available": True,
                },
                {
                    "id": "project1",
                    "name": "Project",
                    "path": str(_project),
                    "kind": "git-uninitialized",
                    "worktreeType": "worktree",
                    "repoId": "repo_xyz",
                    "branch": "feature/x",
                    "available": True,
                },
            ]
        )
        with mock.patch(
            "gui.server.controlled_merge_to_main",
            side_effect=server.GitError("git merge-base failed."),
        ):
            with self.assertRaises(server.ApiError) as ctx:
                server.merge_task_to_main(task.id, project_store, task_store)
        self.assertEqual(ctx.exception.status, 409)
        reloaded = task_store.load(task.id)
        self.assertIsNone(reloaded.mergeCommitSha)
        self.assertIsNone(reloaded.mergedAt)
        events = [h["event"] for h in reloaded.history]
        self.assertIn("MERGE_BLOCKED", events)
        self.assertIn("merge-base", str(reloaded.history[-1]["message"]))

    def _install_primary_for_merge(self, project_store, project, branch):
        project_store.save_projects(
            [
                {
                    "id": "primary1",
                    "name": "Primary",
                    "path": str(project),
                    "kind": "git-uninitialized",
                    "worktreeType": "primary",
                    "repoId": "repo_xyz",
                    "available": True,
                },
                {
                    "id": "project1",
                    "name": "Project",
                    "path": str(project),
                    "kind": "git-uninitialized",
                    "worktreeType": "worktree",
                    "repoId": "repo_xyz",
                    "branch": branch,
                    "available": True,
                },
            ]
        )

    def test_merge_task_records_head_drift_in_history_and_audit(self):
        # Codex P2-2 round 18: when ``controlled_merge_to_main`` reports
        # that HEAD moved externally after the CAS ref update
        # (``headDriftSha`` non-null), the service must:
        #   1. persist the drift SHA on the task object so it survives
        #      reload (``Task.headDriftSha``);
        #   2. include it as a ``headDriftSha`` field on the ``MERGED``
        #      history event so the audit trail can reconcile the
        #      recorded ``mergeCommitSha`` with the live branch tip
        #      later;
        #   3. include it in the ``task.merge`` audit log details so
        #      forensic review of ``audit.log`` can correlate the
        #      controlled merge with subsequent branch movement.
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.commitSha = "abc123"
        task.worktreeBranch = "feature/x"
        # Codex P1-3 round 19: GUI merge requires reviewed baseline.
        task.reviewedRound = task.round
        task.reviewedHeadSha = "basesha1"
        task_store.save(task)
        self._install_primary_for_merge(project_store, _project, "feature/x")

        audit_payloads: list[dict] = []
        original_audit = server.write_audit_log

        def capture_audit(action, subject, details=None):
            audit_payloads.append({
                "action": action,
                "subject": subject,
                "details": details or {},
            })
            return original_audit(action, subject, details)

        with (
            mock.patch("gui.server.controlled_merge_to_main", side_effect=materialise_mock_merge({
                "mergeCommitSha": "mergedeadbeef",
                "mergeShortSha": "mergedeb",
                "mergeTargetBranch": "master",
                "mergeSourceBranch": "feature/x",
                "headDriftSha": "externalcafe",
            })),
            mock.patch("gui.server.write_audit_log", side_effect=capture_audit),
        ):
            updated = server.merge_task_to_main(task.id, project_store, task_store)

        # 1. ``Task.headDriftSha`` field is persisted on the task object.
        self.assertEqual(updated.headDriftSha, "externalcafe")
        reloaded = task_store.load(task.id)
        self.assertEqual(reloaded.headDriftSha, "externalcafe")
        # 2. ``MERGED`` history event includes the drift SHA.
        merged_events = [h for h in updated.history if h["event"] == "MERGED"]
        self.assertEqual(len(merged_events), 1)
        self.assertEqual(merged_events[0].get("headDriftSha"), "externalcafe")
        # Message includes the truncated SHA (``[:10]``).
        self.assertIn("externalca", merged_events[0]["message"])
        # 3. Audit log ``task.merge`` entry includes the drift SHA.
        merge_audit = next(
            (p for p in audit_payloads if p["action"] == "task.merge"), None,
        )
        self.assertIsNotNone(merge_audit, "task.merge audit event must be recorded")
        self.assertEqual(merge_audit["details"].get("headDriftSha"), "externalcafe")
        self.assertEqual(merge_audit["details"].get("mergeCommitSha"), "mergedeadbeef")

    def test_merge_task_omits_head_drift_fields_on_clean_path(self):
        # Codex P2-2 round 18: on the clean path (no drift), the
        # ``headDriftSha`` field must NOT be added to the history event
        # or audit details — absence is the signal the merge is
        # pristine, presence is the signal the recorded SHA needs
        # reconciliation.
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.commitSha = "abc123"
        task.worktreeBranch = "feature/x"
        # Codex P1-3 round 19: GUI merge requires reviewed baseline.
        task.reviewedRound = task.round
        task.reviewedHeadSha = "basesha1"
        task_store.save(task)
        self._install_primary_for_merge(project_store, _project, "feature/x")

        audit_payloads: list[dict] = []
        original_audit = server.write_audit_log

        def capture_audit(action, subject, details=None):
            audit_payloads.append({
                "action": action,
                "subject": subject,
                "details": details or {},
            })
            return original_audit(action, subject, details)

        with (
            mock.patch("gui.server.controlled_merge_to_main", side_effect=materialise_mock_merge({
                "mergeCommitSha": "mergedeadbeef",
                "mergeShortSha": "mergedeb",
                "mergeTargetBranch": "master",
                "mergeSourceBranch": "feature/x",
                "headDriftSha": None,
            })),
            mock.patch("gui.server.write_audit_log", side_effect=capture_audit),
        ):
            updated = server.merge_task_to_main(task.id, project_store, task_store)

        self.assertIsNone(updated.headDriftSha)
        reloaded = task_store.load(task.id)
        self.assertIsNone(reloaded.headDriftSha)
        merged_events = [h for h in updated.history if h["event"] == "MERGED"]
        self.assertEqual(len(merged_events), 1)
        self.assertNotIn("headDriftSha", merged_events[0])
        merge_audit = next(
            (p for p in audit_payloads if p["action"] == "task.merge"), None,
        )
        self.assertIsNotNone(merge_audit)
        self.assertNotIn("headDriftSha", merge_audit["details"])

    def test_merge_task_records_unreachable_when_merge_commit_left_head(self):
        # Codex P2-2 round 18: distinguish "branch advanced past the
        # merge" from "branch moved away from the merge".  When the
        # recorded merge commit is still reachable from the live HEAD
        # (e.g. another commit landed on top), the drift is informational.
        # When the merge commit is NOT reachable from live HEAD (e.g.
        # branch was force-moved / reset), the audit trail must record
        # a separate ``task.merge.head_unreachable`` event so forensic
        # review can flag the inconsistency for human follow-up.
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.commitSha = "abc123"
        task.worktreeBranch = "feature/x"
        # Codex P1-3 round 19: GUI merge requires reviewed baseline.
        task.reviewedRound = task.round
        task.reviewedHeadSha = "basesha1"
        task_store.save(task)
        self._install_primary_for_merge(project_store, _project, "feature/x")

        audit_payloads: list[dict] = []
        original_audit = server.write_audit_log

        def capture_audit(action, subject, details=None):
            audit_payloads.append({
                "action": action,
                "subject": subject,
                "details": details or {},
            })
            return original_audit(action, subject, details)

        # Simulate ``is_ancestor`` reporting that the merge commit is
        # NOT reachable from the live HEAD (e.g. branch was force-moved
        # between the CAS and the post-CAS reachability probe).
        with (
            mock.patch("gui.server.controlled_merge_to_main", side_effect=materialise_mock_merge({
                "mergeCommitSha": "mergedeadbeef",
                "mergeShortSha": "mergedeb",
                "mergeTargetBranch": "master",
                "mergeSourceBranch": "feature/x",
                "headDriftSha": "forcemoved",
            })),
            mock.patch("gui.server.is_ancestor", return_value=False),
            mock.patch("gui.server.write_audit_log", side_effect=capture_audit),
        ):
            updated = server.merge_task_to_main(task.id, project_store, task_store)

        # The merge still succeeds (CAS succeeded; drift is informational).
        self.assertEqual(updated.mergeCommitSha, "mergedeadbeef")
        # A separate ``task.merge.head_unreachable`` audit event must
        # be present alongside the normal ``task.merge`` event.
        actions = [p["action"] for p in audit_payloads]
        self.assertIn("task.merge", actions)
        self.assertIn("task.merge.head_unreachable", actions)
        unreachable_audit = next(
            (p for p in audit_payloads if p["action"] == "task.merge.head_unreachable"),
            None,
        )
        self.assertIsNotNone(unreachable_audit)
        self.assertEqual(unreachable_audit["details"].get("mergeCommitSha"), "mergedeadbeef")
        self.assertEqual(unreachable_audit["details"].get("liveHeadSha"), "forcemoved")

    def test_merge_task_does_not_record_unreachable_when_merge_still_reachable(self):
        # Codex P2-2 round 18: when the merge commit IS still reachable
        # from the live HEAD (e.g. an unrelated commit landed on top of
        # the merge), the drift is informational only — the merge
        # remains the recorded commit's ancestor.  The audit trail must
        # NOT emit ``task.merge.head_unreachable`` in that case; the
        # ``headDriftSha`` field on ``task.merge`` is sufficient.
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.commitSha = "abc123"
        task.worktreeBranch = "feature/x"
        # Codex P1-3 round 19: GUI merge requires reviewed baseline.
        task.reviewedRound = task.round
        task.reviewedHeadSha = "basesha1"
        task_store.save(task)
        self._install_primary_for_merge(project_store, _project, "feature/x")

        audit_payloads: list[dict] = []
        original_audit = server.write_audit_log

        def capture_audit(action, subject, details=None):
            audit_payloads.append({
                "action": action,
                "subject": subject,
                "details": details or {},
            })
            return original_audit(action, subject, details)

        with (
            mock.patch("gui.server.controlled_merge_to_main", side_effect=materialise_mock_merge({
                "mergeCommitSha": "mergedeadbeef",
                "mergeShortSha": "mergedeb",
                "mergeTargetBranch": "master",
                "mergeSourceBranch": "feature/x",
                "headDriftSha": "descendant",
            })),
            mock.patch("gui.server.is_ancestor", return_value=True),
            mock.patch("gui.server.write_audit_log", side_effect=capture_audit),
        ):
            updated = server.merge_task_to_main(task.id, project_store, task_store)

        self.assertEqual(updated.mergeCommitSha, "mergedeadbeef")
        actions = [p["action"] for p in audit_payloads]
        self.assertIn("task.merge", actions)
        self.assertNotIn("task.merge.head_unreachable", actions)

    def test_merge_task_emits_probe_failed_audit_when_is_ancestor_raises(self):
        # Codex P2-1 round 19: when ``is_ancestor`` raises ``GitError``
        # because the underlying ``git merge-base --is-ancestor`` exits
        # with an unclassifiable code (not 0 or 1), the merge service
        # must record a separate ``task.merge.reachability_probe_failed``
        # audit event rather than conflating the probe failure with the
        # "we proved unreachable" branch.
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.commitSha = "abc123"
        task.worktreeBranch = "feature/x"
        task.reviewedRound = task.round
        task.reviewedHeadSha = "basesha1"
        task_store.save(task)
        self._install_primary_for_merge(project_store, _project, "feature/x")

        audit_payloads: list[dict] = []
        original_audit = server.write_audit_log

        def capture_audit(action, subject, details=None):
            audit_payloads.append({
                "action": action,
                "subject": subject,
                "details": details or {},
            })
            return original_audit(action, subject, details)

        with (
            mock.patch("gui.server.controlled_merge_to_main", side_effect=materialise_mock_merge({
                "mergeCommitSha": "mergedeadbeef",
                "mergeShortSha": "mergedeb",
                "mergeTargetBranch": "master",
                "mergeSourceBranch": "feature/x",
                "headDriftSha": "externalcafe",
            })),
            mock.patch(
                "gui.server.is_ancestor",
                side_effect=server.GitError("git merge-base exited with code 2"),
            ),
            mock.patch("gui.server.write_audit_log", side_effect=capture_audit),
        ):
            updated = server.merge_task_to_main(task.id, project_store, task_store)

        # The merge itself succeeds; only the audit classification is
        # affected.
        self.assertEqual(updated.mergeCommitSha, "mergedeadbeef")
        actions = [p["action"] for p in audit_payloads]
        self.assertIn("task.merge", actions)
        # Probe-failure event must be emitted instead of head_unreachable.
        self.assertIn("task.merge.reachability_probe_failed", actions)
        self.assertNotIn("task.merge.head_unreachable", actions)
        probe_audit = next(
            (p for p in audit_payloads if p["action"] == "task.merge.reachability_probe_failed"),
            None,
        )
        self.assertIsNotNone(probe_audit)
        self.assertEqual(probe_audit["details"].get("liveHeadSha"), "externalcafe")
        self.assertIn("merge-base", str(probe_audit["details"].get("reason", "")))

    def test_merge_task_rejects_already_merged(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.commitSha = "abc"
        task.mergedAt = "2026-06-17T00:00:00Z"
        task_store.save(task)
        with self.assertRaises(server.ApiError):
            server.merge_task_to_main(task.id, project_store, task_store)

    def test_commit_task_rejects_archived_task(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.archivedAt = "2026-06-17T00:00:00Z"
        task_store.save(task)
        with self.assertRaises(server.ApiError):
            server.commit_task_changes(task.id, {"message": "msg"}, project_store, task_store)

    def test_commit_task_passes_review_snapshot_to_controlled_commit(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.reviewedRound = 1
        task.reviewedHeadSha = "headsha1"
        task.reviewedStatusHash = "statushash1"
        task.reviewedDiffHash = "diffhash1"
        task.reviewedTreeSha = "treesha1"
        task_store.save(task)
        captured = {}

        def fake_commit(path, message, expected_snapshot=None):
            captured["expected_snapshot"] = expected_snapshot
            return {
                "commitSha": "abc",
                "commitShortSha": "abc",
                "commitMessage": message,
            }

        with mock.patch("gui.server.controlled_commit", side_effect=fake_commit):
            server.commit_task_changes(task.id, {"message": "feat"}, project_store, task_store)
        self.assertIsNotNone(captured.get("expected_snapshot"))
        self.assertEqual(captured["expected_snapshot"]["headSha"], "headsha1")
        self.assertEqual(captured["expected_snapshot"]["statusHash"], "statushash1")
        self.assertEqual(captured["expected_snapshot"]["diffHash"], "diffhash1")
        self.assertEqual(captured["expected_snapshot"]["treeSha"], "treesha1")

    def test_commit_task_blocks_when_snapshot_missing_for_round(self):
        # A PASS task without a reviewed snapshot for the current round must
        # be refused at the commit endpoint so unreviewed changes cannot be
        # smuggled into a commit.  This covers both legacy PASS tasks (no
        # snapshot fields at all) and tasks where the snapshot is stale
        # (reviewedRound differs from current round).
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task_store.save(task)
        with mock.patch("gui.server.controlled_commit") as commit_mock:
            with self.assertRaises(server.ApiError) as ctx:
                server.commit_task_changes(task.id, {"message": "feat"}, project_store, task_store)
        self.assertEqual(ctx.exception.status, 409)
        commit_mock.assert_not_called()
        reloaded = task_store.load(task.id)
        events = [h["event"] for h in reloaded.history]
        self.assertIn("COMMIT_BLOCKED", events)
        self.assertIsNone(reloaded.commitSha)

    def test_commit_task_blocks_when_snapshot_round_is_stale(self):
        # reviewedRound != task.round means the snapshot was captured in an
        # earlier review cycle; the current round has not been re-snapshotted
        # and therefore cannot be trusted.
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.round = 2
        task.reviewedRound = 1
        task.reviewedHeadSha = "headsha1"
        task.reviewedStatusHash = "statushash1"
        task.reviewedDiffHash = "diffhash1"
        task.reviewedTreeSha = "treesha1"
        task_store.save(task)
        with mock.patch("gui.server.controlled_commit") as commit_mock:
            with self.assertRaises(server.ApiError):
                server.commit_task_changes(task.id, {"message": "feat"}, project_store, task_store)
        commit_mock.assert_not_called()

    def test_commit_task_drift_failure_is_recorded_as_blocked(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.reviewedRound = 1
        task.reviewedHeadSha = "headsha1"
        task.reviewedStatusHash = "statushash1"
        task.reviewedDiffHash = "diffhash1"
        task.reviewedTreeSha = "treesha1"
        task_store.save(task)
        with mock.patch(
            "gui.server.controlled_commit",
            side_effect=server.CommitError("drift detected"),
        ):
            with self.assertRaises(server.ApiError):
                server.commit_task_changes(task.id, {"message": "feat"}, project_store, task_store)
        reloaded = task_store.load(task.id)
        self.assertIsNone(reloaded.commitSha)
        events = [h["event"] for h in reloaded.history]
        self.assertIn("COMMIT_BLOCKED", events)

    def test_commit_task_blocks_when_reviewed_head_sha_missing(self):
        # Codex P1-1 round 14: the commit endpoint must require
        # ``reviewedHeadSha`` to be present.  Without it, the CAS ref
        # update and the merge base reachability check would silently
        # no-op, allowing unreviewed history to slip into the trunk.
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.reviewedRound = task.round
        task.reviewedStatusHash = "statushash1"
        task.reviewedDiffHash = "diffhash1"
        task.reviewedTreeSha = "treesha1"
        # reviewedHeadSha left as None
        task_store.save(task)
        with mock.patch("gui.server.controlled_commit") as commit_mock:
            with self.assertRaises(server.ApiError) as ctx:
                server.commit_task_changes(task.id, {"message": "feat"}, project_store, task_store)
        self.assertEqual(ctx.exception.status, 409)
        commit_mock.assert_not_called()
        reloaded = task_store.load(task.id)
        events = [h["event"] for h in reloaded.history]
        self.assertIn("COMMIT_BLOCKED", events)
        self.assertIsNone(reloaded.commitSha)

    def test_commit_task_records_git_error_as_blocked_history(self):
        # Codex P2-1 round 14: when ``controlled_commit`` raises the
        # underlying ``GitError`` (e.g. from snapshot computation,
        # path enumeration, or write-tree), the service must still
        # record a ``COMMIT_BLOCKED`` history event and surface a
        # consistent 409 ``ApiError`` — not let the generic handler
        # produce a different status code.
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.reviewedRound = task.round
        task.reviewedHeadSha = "headsha1"
        task.reviewedStatusHash = "statushash1"
        task.reviewedDiffHash = "diffhash1"
        task.reviewedTreeSha = "treesha1"
        task_store.save(task)
        with mock.patch(
            "gui.server.controlled_commit",
            side_effect=server.GitError("git write-tree failed."),
        ):
            with self.assertRaises(server.ApiError) as ctx:
                server.commit_task_changes(task.id, {"message": "feat"}, project_store, task_store)
        self.assertEqual(ctx.exception.status, 409)
        reloaded = task_store.load(task.id)
        self.assertIsNone(reloaded.commitSha)
        events = [h["event"] for h in reloaded.history]
        self.assertIn("COMMIT_BLOCKED", events)
        self.assertIn("write-tree", str(reloaded.history[-1]["message"]))
        reloaded = task_store.load(task.id)
        self.assertIsNone(reloaded.commitSha)
        events = [h["event"] for h in reloaded.history]
        self.assertIn("COMMIT_BLOCKED", events)

    def test_merge_task_passes_reviewed_commit_sha_to_controlled_merge(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.commitSha = "reviewedcommitsha"
        task.reviewedHeadSha = "reviewedheadsha"
        task.reviewedRound = task.round
        task.worktreeBranch = "feature/x"
        task_store.save(task)
        project_store.save_projects(
            [
                {
                    "id": "primary1",
                    "name": "Primary",
                    "path": str(_project),
                    "kind": "git-uninitialized",
                    "worktreeType": "primary",
                    "repoId": "repo_xyz",
                    "available": True,
                },
                {
                    "id": "project1",
                    "name": "Project",
                    "path": str(_project),
                    "kind": "git-uninitialized",
                    "worktreeType": "worktree",
                    "repoId": "repo_xyz",
                    "branch": "feature/x",
                    "available": True,
                },
            ]
        )
        captured = {}

        def fake_merge(
            main_path,
            source_branch,
            expected_commit_sha=None,
            expected_base_sha=None,
            **kwargs,
        ):
            captured["expected_commit_sha"] = expected_commit_sha
            captured["expected_base_sha"] = expected_base_sha
            result = {
                "mergeCommitSha": "deadbeef",
                "mergeShortSha": "deadbee",
                "mergeTargetBranch": "master",
                "mergeSourceBranch": source_branch,
            }
            return materialise_mock_merge(result)(
                main_path,
                source_branch,
                expected_commit_sha=expected_commit_sha,
                expected_base_sha=expected_base_sha,
                **kwargs,
            )

        with mock.patch("gui.server.controlled_merge_to_main", side_effect=fake_merge):
            server.merge_task_to_main(task.id, project_store, task_store)
        self.assertEqual(captured.get("expected_commit_sha"), "reviewedcommitsha")
        # Codex P1-1 round 12: the reviewed HEAD captured at artifact time
        # must also be plumbed through so the merge refuses to sweep
        # unreviewed pre-task commits into the trunk.
        self.assertEqual(captured.get("expected_base_sha"), "reviewedheadsha")


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
        task.reviewedRound = task.round
        task.reviewedHeadSha = "h"
        task.reviewedStatusHash = "s"
        task.reviewedDiffHash = "d"
        task.reviewedTreeSha = "t"
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

        with mock.patch(
            "gui.server.compute_review_snapshot",
            return_value={"headSha": "h", "statusHash": "s", "diffHash": "d", "treeSha": "t"},
        ):
            updated = server.complete_codex_task(task.id, project_store, task_store)

        self.assertEqual(updated.status, "PASS")
        self.assertEqual(updated.progress, 100)
        self.assertEqual(updated.stage, "review_complete")
        self.assertIsNone(updated.activeClient)
        self.assertIsNotNone(updated.lastActivityAt)

    def test_claude_completion_captures_review_snapshot_at_artifact_time(self):
        # Snapshot is captured at artifact-collection time (complete_claude_task),
        # not at PASS time, so post-artifact drift is not absorbed into the
        # "reviewed" state.  The snapshot is computed BEFORE and AFTER
        # ``collect_git_artifacts`` and the two are compared to ensure the
        # recorded baseline matches exactly what was collected into the
        # review artifacts (Codex P1-1 round 11).
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
            return GitArtifacts(status_path, diff_stat_path, diff_path, " M app.py\n",
                                " app.py | 1 +\n", "diff --git a/app.py b/app.py\n")

        def fake_tests(_project_path, task_path, round_number, _command):
            path = task_path / f"test_results_round_{round_number}.txt"
            path.write_text("EXIT_CODE: 0\n", encoding="utf-8")
            return TestRunResult(["test"], 0, "EXIT_CODE: 0\n", path)

        fake_snapshot = {
            "headSha": "headArtifact",
            "statusHash": "statusArtifact",
            "diffHash": "diffArtifact",
            "treeSha": "treeArtifact",
        }
        with (
            mock.patch("gui.server.assert_git_work_tree"),
            mock.patch("gui.server.collect_git_artifacts", side_effect=fake_collect),
            mock.patch("gui.server.run_tests", side_effect=fake_tests),
            mock.patch("gui.server.compute_review_snapshot", return_value=fake_snapshot) as snap_mock,
        ):
            updated = server.complete_claude_task(task.id, project_store, task_store)

        # Pre- and post-artifact snapshots both run; they return the same
        # fake so the snapshot is persisted.  Exactly two calls proves the
        # server captures before AND after, and the persisted values come
        # from the post-collection call (which is identical to the
        # pre-collection call here).
        self.assertEqual(snap_mock.call_count, 2)
        self.assertEqual(updated.status, "WAITING_FOR_CODEX")
        self.assertEqual(updated.reviewedRound, task.round)
        self.assertEqual(updated.reviewedHeadSha, "headArtifact")
        self.assertEqual(updated.reviewedStatusHash, "statusArtifact")
        self.assertEqual(updated.reviewedDiffHash, "diffArtifact")
        self.assertEqual(updated.reviewedTreeSha, "treeArtifact")

    def test_claude_completion_discards_snapshot_when_worktree_drifts_during_collection(self):
        # Regression for Codex P1-1 round 11: if the worktree mutates
        # between the pre-artifact and post-artifact snapshot capture, the
        # recorded snapshot is discarded so PASS-time verification blocks
        # instead of approving unreviewed content.  The fake returns a
        # different value on the second call to simulate drift between
        # the two ``compute_review_snapshot`` invocations.
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
            return GitArtifacts(status_path, diff_stat_path, diff_path, " M app.py\n",
                                " app.py | 1 +\n", "diff --git a/app.py b/app.py\n")

        def fake_tests(_project_path, task_path, round_number, _command):
            path = task_path / f"test_results_round_{round_number}.txt"
            path.write_text("EXIT_CODE: 0\n", encoding="utf-8")
            return TestRunResult(["test"], 0, "EXIT_CODE: 0\n", path)

        pre_snapshot = {
            "headSha": "headBefore",
            "statusHash": "statusBefore",
            "diffHash": "diffBefore",
            "treeSha": "treeBefore",
        }
        post_snapshot = {
            "headSha": "headAfter",
            "statusHash": "statusAfter",
            "diffHash": "diffAfter",
            "treeSha": "treeAfter",
        }
        with (
            mock.patch("gui.server.assert_git_work_tree"),
            mock.patch("gui.server.collect_git_artifacts", side_effect=fake_collect),
            mock.patch("gui.server.run_tests", side_effect=fake_tests),
            mock.patch(
                "gui.server.compute_review_snapshot",
                side_effect=[pre_snapshot, post_snapshot],
            ) as snap_mock,
        ):
            updated = server.complete_claude_task(task.id, project_store, task_store)

        self.assertEqual(snap_mock.call_count, 2)
        self.assertEqual(updated.status, "WAITING_FOR_CODEX")
        # Snapshot must be discarded: every recorded field is cleared and
        # the history records the failure.
        self.assertIsNone(updated.reviewedRound)
        self.assertIsNone(updated.reviewedHeadSha)
        self.assertIsNone(updated.reviewedStatusHash)
        self.assertIsNone(updated.reviewedDiffHash)
        self.assertIsNone(updated.reviewedTreeSha)
        events = [h["event"] for h in updated.history]
        self.assertIn("REVIEW_SNAPSHOT_FAILED", events)

    def test_claude_completion_blocks_when_env_present_at_pre_artifact_snapshot(self):
        # Pre-artifact snapshot capture raises ``EnvFileChangedError`` when
        # a forbidden ``.env`` file is present in the worktree at the start
        # of Claude completion.  The whole Claude completion must fail
        # fast with the existing artifact-collection env guard semantics,
        # not silently fall through to a partial snapshot.
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "CLAUDE_WINDOW_STARTED"
        task_store.save(task)

        with mock.patch(
            "gui.server.compute_review_snapshot",
            side_effect=server.EnvFileChangedError("a .env file is present"),
        ):
            updated = server.complete_claude_task(task.id, project_store, task_store)

        self.assertEqual(updated.status, "FAILED")
        self.assertIsNone(updated.reviewedRound)
        self.assertIsNone(updated.reviewedHeadSha)

    def test_codex_pass_does_not_overwrite_snapshot_and_blocks_drift(self):
        # Snapshot was captured at artifact time.  On Codex PASS the worktree
        # must still match it; otherwise the PASS is blocked because the
        # current worktree no longer corresponds to what Codex reviewed.
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "CODEX_WINDOW_STARTED"
        task.progress = 60
        task.stage = "codex_running"
        task.activeClient = "codex"
        task.reviewedRound = task.round
        task.reviewedHeadSha = "headX"
        task.reviewedStatusHash = "statusX"
        task.reviewedDiffHash = "diffX"
        task.reviewedTreeSha = "treeX"
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

        # Current worktree now differs from the stored snapshot.
        with mock.patch(
            "gui.server.compute_review_snapshot",
            return_value={
                "headSha": "headY",
                "statusHash": "statusY",
                "diffHash": "diffY",
                "treeSha": "treeY",
            },
        ):
            updated = server.complete_codex_task(task.id, project_store, task_store)

        # PASS is blocked; the stored snapshot is preserved (not overwritten).
        self.assertEqual(updated.status, "FAILED")
        self.assertEqual(updated.reviewedHeadSha, "headX")
        self.assertEqual(updated.reviewedStatusHash, "statusX")
        self.assertEqual(updated.reviewedDiffHash, "diffX")
        events = [h["event"] for h in updated.history]
        self.assertIn("REVIEW_DRIFT_BLOCKED", events)

    def test_codex_pass_allows_when_worktree_matches_snapshot(self):
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "CODEX_WINDOW_STARTED"
        task.progress = 60
        task.stage = "codex_running"
        task.activeClient = "codex"
        task.reviewedRound = task.round
        task.reviewedHeadSha = "headX"
        task.reviewedStatusHash = "statusX"
        task.reviewedDiffHash = "diffX"
        task.reviewedTreeSha = "treeX"
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

        with mock.patch(
            "gui.server.compute_review_snapshot",
            return_value={
                "headSha": "headX",
                "statusHash": "statusX",
                "diffHash": "diffX",
                "treeSha": "treeX",
            },
        ):
            updated = server.complete_codex_task(task.id, project_store, task_store)

        self.assertEqual(updated.status, "PASS")
        self.assertEqual(updated.reviewedHeadSha, "headX")
        self.assertEqual(updated.reviewedStatusHash, "statusX")
        self.assertEqual(updated.reviewedDiffHash, "diffX")
        self.assertEqual(updated.reviewedTreeSha, "treeX")

    def test_codex_pass_blocks_when_snapshot_missing_for_round(self):
        # No snapshot stored for the current round => cannot verify what
        # Codex reviewed; PASS must be blocked, not silently allowed.
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "CODEX_WINDOW_STARTED"
        task.progress = 60
        task.stage = "codex_running"
        task.activeClient = "codex"
        # reviewedRound / hashes are None
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

        with mock.patch("gui.server.compute_review_snapshot") as snap_mock:
            updated = server.complete_codex_task(task.id, project_store, task_store)
        self.assertEqual(updated.status, "FAILED")
        snap_mock.assert_not_called()
        events = [h["event"] for h in updated.history]
        self.assertIn("REVIEW_DRIFT_BLOCKED", events)

    def test_codex_pass_blocks_when_reviewed_head_sha_missing(self):
        # Codex P1-1 round 14: the PASS-time verifier must require
        # ``reviewedHeadSha`` to be present (alongside the other snapshot
        # fields).  Without it, the merge path's reachability check on
        # ``expected_base_sha`` would silently no-op and the merge could
        # sweep unreviewed commits into the trunk.
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "CODEX_WINDOW_STARTED"
        task.progress = 60
        task.stage = "codex_running"
        task.activeClient = "codex"
        task.reviewedRound = task.round
        # reviewedHeadSha left as None; the other snapshot fields are set.
        task.reviewedStatusHash = "statusX"
        task.reviewedDiffHash = "diffX"
        task.reviewedTreeSha = "treeX"
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

        with mock.patch("gui.server.compute_review_snapshot") as snap_mock:
            updated = server.complete_codex_task(task.id, project_store, task_store)
        self.assertEqual(updated.status, "FAILED")
        snap_mock.assert_not_called()
        events = [h["event"] for h in updated.history]
        self.assertIn("REVIEW_DRIFT_BLOCKED", events)

    def test_codex_pass_blocks_when_env_file_present_at_pass_time(self):
        # Regression for Codex P1-1 round 7: ``compute_review_snapshot`` now
        # raises ``EnvFileChangedError`` when a ``.env`` path is staged.
        # ``_verify_review_snapshot_at_pass`` must surface this as a
        # PASS-blocking reason so the user removes the ``.env`` change
        # before re-running the review cycle (and the backend never reads
        # the ``.env`` bytes / diff content).
        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "CODEX_WINDOW_STARTED"
        task.progress = 60
        task.stage = "codex_running"
        task.activeClient = "codex"
        task.reviewedRound = task.round
        task.reviewedHeadSha = "headX"
        task.reviewedStatusHash = "statusX"
        task.reviewedDiffHash = "diffX"
        task.reviewedTreeSha = "treeX"
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

        with mock.patch(
            "gui.server.compute_review_snapshot",
            side_effect=server.EnvFileChangedError(
                "A .env file is present in the worktree; review snapshot "
                "collection was blocked before any content was read."
            ),
        ):
            updated = server.complete_codex_task(task.id, project_store, task_store)

        # PASS is blocked; the stored snapshot is preserved (not overwritten).
        self.assertEqual(updated.status, "FAILED")
        self.assertEqual(updated.reviewedHeadSha, "headX")
        self.assertEqual(updated.reviewedStatusHash, "statusX")
        self.assertEqual(updated.reviewedDiffHash, "diffX")
        events = [h["event"] for h in updated.history]
        self.assertIn("REVIEW_DRIFT_BLOCKED", events)
        last_block = next(
            h for h in updated.history if h["event"] == "REVIEW_DRIFT_BLOCKED"
        )
        self.assertIn(".env", last_block["message"])

    def test_codex_completion_review_failed_does_not_capture_snapshot(self):
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
            json.dumps({"status": "FAILED", "reviewed_at": "2026-01-01T00:00:00Z", "findings": []}),
            encoding="utf-8",
        )

        with mock.patch("gui.server.compute_review_snapshot") as snap_mock:
            updated = server.complete_codex_task(task.id, project_store, task_store)

        self.assertEqual(updated.status, "FAILED")
        snap_mock.assert_not_called()


class ConcurrentOperationTests(unittest.TestCase):
    """Codex P1-4 round 15: ``ThreadingHTTPServer`` dispatches each request
    on its own thread, so duplicate concurrent POSTs to ``/api/tasks/{id}/commit``
    or ``/api/tasks/{id}/merge`` can race the per-task state machine.  The
    per-task ``RLock`` in ``_task_operation_lock`` must serialise the
    ``load → validate → mutate Git → save`` span so the loser of the race
    observes the winner's ``COMMITTED`` / ``MERGED`` metadata and
    short-circuits instead of overwriting it with a stale snapshot.
    """

    def make_project_store(self):
        root = server.ROOT / ".gui" / "test-tmp" / f"conc-{uuid.uuid4().hex}"
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

    def _install_primary_worktree_entry(self, project_store, project, branch):
        project_store.save_projects(
            [
                {
                    "id": "primary1",
                    "name": "Primary",
                    "path": str(project),
                    "kind": "git-uninitialized",
                    "worktreeType": "primary",
                    "repoId": "repo_xyz",
                    "available": True,
                },
                {
                    "id": "project1",
                    "name": "Project",
                    "path": str(project),
                    "kind": "git-uninitialized",
                    "worktreeType": "worktree",
                    "repoId": "repo_xyz",
                    "branch": branch,
                    "available": True,
                },
            ]
        )

    def test_concurrent_commit_requests_are_serialised(self):
        # Two concurrent commit requests for the same task must be
        # serialised: the winner commits and saves ``COMMITTED`` metadata;
        # the loser observes ``task.commitSha`` set inside the lock and
        # short-circuits with a 409 ``ApiError`` instead of attempting a
        # second commit (which would fail because HEAD has advanced) and
        # overwriting the winner's metadata with stale data.
        import threading

        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.reviewedRound = task.round
        task.reviewedHeadSha = "headsha1"
        task.reviewedStatusHash = "statushash1"
        task.reviewedDiffHash = "diffhash1"
        task.reviewedTreeSha = "treesha1"
        task_store.save(task)

        commit_calls: list[float] = []
        commit_lock = threading.Lock()

        def slow_commit(path, message, expected_snapshot=None):
            # Record arrival time and sleep long enough that both threads
            # are guaranteed to be inside ``controlled_commit`` if locking
            # is absent.  The per-task lock in ``commit_task_changes``
            # should prevent this — only one call should ever be in flight.
            with commit_lock:
                commit_calls.append(time.time())
            time.sleep(0.2)
            return {
                "commitSha": "abc123def456",
                "commitShortSha": "abc123d",
                "commitMessage": message,
            }

        results: dict[str, object] = {}
        errors: dict[str, Exception] = {}

        def worker(label):
            try:
                results[label] = server.commit_task_changes(
                    task.id, {"message": "feat: work"}, project_store, task_store
                )
            except Exception as exc:  # noqa: BLE001 — record for assertion
                errors[label] = exc

        with mock.patch("gui.server.controlled_commit", side_effect=slow_commit):
            threads = [
                threading.Thread(target=worker, args=("a",)),
                threading.Thread(target=worker, args=("b",)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # Exactly one ``controlled_commit`` call should have been made:
        # the second request must observe the first's ``commitSha`` inside
        # the lock and short-circuit.  If locking is missing, both threads
        # call ``controlled_commit`` and both pass validation.
        self.assertEqual(
            len(commit_calls),
            1,
            f"expected exactly one controlled_commit call, got {len(commit_calls)}",
        )
        # Exactly one worker succeeds, exactly one gets the 409 already-committed.
        self.assertEqual(len(results), 1, f"expected exactly one success, got {results}")
        self.assertEqual(len(errors), 1)
        winner_label = next(iter(results.keys()))
        loser_label = next(iter(errors.keys()))
        winner = results[winner_label]
        loser_err = errors[loser_label]
        self.assertIsInstance(loser_err, server.ApiError)
        self.assertEqual(loser_err.status, HTTPStatus.CONFLICT)
        self.assertIn("already been committed", str(loser_err))
        self.assertEqual(winner.commitSha, "abc123def456")

    def test_concurrent_merge_requests_are_serialised(self):
        # Same pattern as the commit test but for merge: two concurrent
        # POSTs to ``/api/tasks/{id}/merge`` must be serialised so only
        # one calls ``controlled_merge_to_main`` and the loser short-circuits.
        import threading

        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "PASS"
        task.commitSha = "abc"
        task.worktreeBranch = "feature/x"
        # Codex P1-3 round 19: GUI merge requires reviewed baseline.
        task.reviewedRound = task.round
        task.reviewedHeadSha = "basesha1"
        task_store.save(task)
        self._install_primary_worktree_entry(project_store, _project, "feature/x")

        merge_calls: list[float] = []
        merge_lock = threading.Lock()

        def slow_merge(*args, **kwargs):
            with merge_lock:
                merge_calls.append(time.time())
            time.sleep(0.2)
            result = {
                "mergeCommitSha": "deadbeef",
                "mergeShortSha": "deadbee",
                "mergeTargetBranch": "master",
                "mergeSourceBranch": "feature/x",
            }
            return materialise_mock_merge(result)(*args, **kwargs)

        results: dict[str, object] = {}
        errors: dict[str, Exception] = {}

        def worker(label):
            try:
                results[label] = server.merge_task_to_main(
                    task.id, project_store, task_store
                )
            except Exception as exc:  # noqa: BLE001 — record for assertion
                errors[label] = exc

        with mock.patch("gui.server.controlled_merge_to_main", side_effect=slow_merge):
            threads = [
                threading.Thread(target=worker, args=("a",)),
                threading.Thread(target=worker, args=("b",)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(
            len(merge_calls),
            1,
            f"expected exactly one controlled_merge_to_main call, got {len(merge_calls)}",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        loser_err = next(iter(errors.values()))
        self.assertIsInstance(loser_err, server.ApiError)
        self.assertEqual(loser_err.status, HTTPStatus.CONFLICT)
        self.assertIn("already been merged", str(loser_err))
        winner = next(iter(results.values()))
        self.assertEqual(winner.mergeCommitSha, "deadbeef")

    def test_concurrent_commit_requests_for_different_tasks_do_not_block(self):
        # Sanity check that the per-task lock does NOT serialise unrelated
        # tasks: two different task IDs should both be able to proceed
        # concurrently.  This protects against a regression where the lock
        # becomes a single global lock instead of a per-task registry.
        # Codex P1-3 round 16: the two tasks must also live in *different*
        # worktrees so the per-resource lock (keyed by worktree path)
        # does not serialise them — the resource lock is supposed to
        # serialise same-worktree mutations, not unrelated repositories.
        import threading

        _root, _project, project_store, task_store = self.make_project_store()
        # Create a second project with a distinct worktree path so the
        # resource lock does not see them as the same resource.
        project_b_dir = _root / "project_b"
        project_b_dir.mkdir(parents=True, exist_ok=True)
        project_store.save_projects(
            [
                {
                    "id": "project1",
                    "name": "Project",
                    "path": str(_project),
                    "kind": "git-uninitialized",
                },
                {
                    "id": "project2",
                    "name": "Project B",
                    "path": str(project_b_dir),
                    "kind": "git-uninitialized",
                },
            ]
        )
        task_a = self.create_waiting_task(project_store, task_store)
        # Create a second task bound to ``project2`` so its worktree path
        # differs from task_a's.
        with mock.patch("gui.server.assert_git_work_tree"), mock.patch("gui.server.assert_clean_work_tree"):
            task_b = server.create_task(
                {
                    "projectId": "project2",
                    "title": "Task B",
                    "description": "Do work",
                    "acceptance": "Pass",
                    "maxRounds": 2,
                },
                project_store,
                task_store,
            )

        for t in (task_a, task_b):
            t.status = "PASS"
            t.reviewedRound = t.round
            t.reviewedHeadSha = "headsha1"
            t.reviewedStatusHash = "statushash1"
            t.reviewedDiffHash = "diffhash1"
            t.reviewedTreeSha = "treesha1"
            task_store.save(t)

        in_flight: list[int] = []
        max_in_flight: list[int] = [0]
        counter_lock = threading.Lock()

        def concurrent_commit(path, message, expected_snapshot=None):
            with counter_lock:
                in_flight.append(1)
                if len(in_flight) > max_in_flight[0]:
                    max_in_flight[0] = len(in_flight)
            time.sleep(0.2)
            with counter_lock:
                in_flight.pop()
            return {
                "commitSha": "abc123def456",
                "commitShortSha": "abc123d",
                "commitMessage": message,
            }

        results: dict[str, object] = {}
        errors: dict[str, Exception] = {}

        def worker(label, task_id):
            try:
                results[label] = server.commit_task_changes(
                    task_id, {"message": "feat: work"}, project_store, task_store
                )
            except Exception as exc:  # noqa: BLE001 — record for assertion
                errors[label] = exc

        with mock.patch("gui.server.controlled_commit", side_effect=concurrent_commit):
            threads = [
                threading.Thread(target=worker, args=("a", task_a.id)),
                threading.Thread(target=worker, args=("b", task_b.id)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # Both should succeed (different tasks on different worktrees →
        # no shared per-task lock, no shared resource lock).
        self.assertEqual(len(results), 2)
        self.assertEqual(len(errors), 0)
        # And both ``controlled_commit`` calls should have run concurrently
        # (max in-flight >= 2).  This proves neither the per-task lock nor
        # the resource lock over-serialises unrelated repositories.
        self.assertGreaterEqual(
            max_in_flight[0],
            2,
            f"expected two different worktrees to run concurrently, max in-flight was {max_in_flight[0]}",
        )

    def test_concurrent_commit_requests_for_different_tasks_same_worktree_are_serialised(self):
        # Codex P1-3 round 16: the per-task lock does not serialise
        # different tasks bound to the same worktree.  A resource-level
        # lock keyed by canonical worktree path must serialise the
        # actual Git mutations (``controlled_commit`` invocations) so
        # the two operations do not interleave ``git add`` /
        # ``commit-tree`` / ``update-ref`` against the same index and
        # refs.  The test fails if the resource lock is missing or
        # keyed by task ID instead of worktree path.
        import threading

        _root, project, project_store, task_store = self.make_project_store()
        task_a = self.create_waiting_task(project_store, task_store)
        with mock.patch("gui.server.assert_git_work_tree"), mock.patch("gui.server.assert_clean_work_tree"):
            task_b = server.create_task(
                {
                    "projectId": "project1",
                    "title": "Task B",
                    "description": "Do work",
                    "acceptance": "Pass",
                    "maxRounds": 2,
                },
                project_store,
                task_store,
            )

        for t in (task_a, task_b):
            t.status = "PASS"
            t.reviewedRound = t.round
            t.reviewedHeadSha = "headsha1"
            t.reviewedStatusHash = "statushash1"
            t.reviewedDiffHash = "diffhash1"
            t.reviewedTreeSha = "treesha1"
            task_store.save(t)

        in_flight: list[int] = []
        max_in_flight: list[int] = [0]
        counter_lock = threading.Lock()

        def concurrent_commit(path, message, expected_snapshot=None):
            with counter_lock:
                in_flight.append(1)
                if len(in_flight) > max_in_flight[0]:
                    max_in_flight[0] = len(in_flight)
            time.sleep(0.2)
            with counter_lock:
                in_flight.pop()
            return {
                "commitSha": "abc123def456",
                "commitShortSha": "abc123d",
                "commitMessage": message,
            }

        results: dict[str, object] = {}
        errors: dict[str, Exception] = {}

        def worker(label, task_id):
            try:
                results[label] = server.commit_task_changes(
                    task_id, {"message": "feat: work"}, project_store, task_store
                )
            except Exception as exc:  # noqa: BLE001 — record for assertion
                errors[label] = exc

        with mock.patch("gui.server.controlled_commit", side_effect=concurrent_commit):
            threads = [
                threading.Thread(target=worker, args=("a", task_a.id)),
                threading.Thread(target=worker, args=("b", task_b.id)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # Without a resource lock, both tasks pass validation concurrently
        # and max_in_flight reaches 2.  With the resource lock, the second
        # task waits for the first to finish the controlled_commit call,
        # then short-circuits on the "already committed" check (different
        # task → different commitSha, so it cannot short-circuit on that,
        # but the in-flight Git mutations are still serialised).
        self.assertEqual(
            max_in_flight[0],
            1,
            f"expected same-worktree operations to be serialised, max in-flight was {max_in_flight[0]}",
        )

    # ------------------------------------------------------------------
    # Codex P1-2 round 17 regression coverage: per-task lock must cover
    # cancel / archive / move_to_trash / restore endpoints.
    # ------------------------------------------------------------------

    def test_concurrent_cancel_requests_are_serialised(self):
        # Codex P1-2 round 17: the per-task lock now wraps
        # ``cancel_task``, so two concurrent cancel requests for the same
        # task must serialise.  Without the lock, both would load the
        # task in its pre-cancel state, both would run the state
        # transition, and the loser of the race would save its stale
        # ``Task`` object — potentially overwriting concurrent
        # ``COMMITTED`` / ``MERGED`` metadata.
        import threading

        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "WAITING_FOR_CLAUDE"
        task_store.save(task)

        in_flight: list[int] = []
        max_in_flight: list[int] = [0]
        counter_lock = threading.Lock()

        original_cancel_status = server.cancel_status

        def slow_cancel_status(status):
            with counter_lock:
                in_flight.append(1)
                if len(in_flight) > max_in_flight[0]:
                    max_in_flight[0] = len(in_flight)
            time.sleep(0.2)
            with counter_lock:
                in_flight.pop()
            return original_cancel_status(status)

        results: dict[str, object] = {}
        errors: dict[str, Exception] = {}

        def worker(label):
            try:
                results[label] = server.cancel_task(task.id, task_store)
            except Exception as exc:  # noqa: BLE001
                errors[label] = exc

        with mock.patch("gui.server.cancel_status", side_effect=slow_cancel_status):
            threads = [
                threading.Thread(target=worker, args=("a",)),
                threading.Thread(target=worker, args=("b",)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # Critical invariant: max in-flight is 1 because the per-task
        # lock serialises the two cancel requests.  One or both may
        # succeed (cancelling an already-CANCELLED task raises a state
        # transition error, which is the loser's expected outcome); the
        # important thing is they did not race the state mutation.
        self.assertEqual(
            max_in_flight[0],
            1,
            f"expected concurrent cancel requests to be serialised, max in-flight was {max_in_flight[0]}",
        )
        self.assertEqual(len(results) + len(errors), 2)
        # The final task state must be CANCELLED regardless of which
        # thread won.
        final_task = task_store.load(task.id)
        self.assertEqual(final_task.status, "CANCELLED")

    def test_concurrent_archive_and_cancel_are_serialised(self):
        # Codex P1-2 round 17: archive and cancel now share the same
        # per-task lock.  Concurrent POSTs to ``/api/tasks/{id}/archive``
        # and ``/api/tasks/{id}/cancel`` must not race the state machine
        # — the loser should observe the winner's mutation rather than
        # overwrite it with a stale snapshot.
        import threading

        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "WAITING_FOR_CLAUDE"
        task_store.save(task)

        in_flight: list[int] = []
        max_in_flight: list[int] = [0]
        counter_lock = threading.Lock()

        def slow_load(task_id):
            with counter_lock:
                in_flight.append(1)
                if len(in_flight) > max_in_flight[0]:
                    max_in_flight[0] = len(in_flight)
            time.sleep(0.2)
            with counter_lock:
                in_flight.pop()
            return task_store.TaskStore__load_fallback(task_id) if hasattr(task_store, "TaskStore__load_fallback") else _original_load(task_id)

        _original_load = task_store.load
        results: dict[str, object] = {}
        errors: dict[str, Exception] = {}

        def archive_worker():
            try:
                results["archive"] = server.archive_task(task.id, task_store)
            except Exception as exc:  # noqa: BLE001
                errors["archive"] = exc

        def cancel_worker():
            try:
                results["cancel"] = server.cancel_task(task.id, task_store)
            except Exception as exc:  # noqa: BLE001
                errors["cancel"] = exc

        with mock.patch.object(task_store, "load", side_effect=slow_load):
            threads = [
                threading.Thread(target=archive_worker),
                threading.Thread(target=cancel_worker),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # Critical invariant: the per-task lock must serialise the two
        # state mutations so they cannot both observe the pre-mutation
        # snapshot and overwrite each other.
        self.assertEqual(
            max_in_flight[0],
            1,
            f"expected concurrent archive+cancel to be serialised by the per-task lock, max in-flight was {max_in_flight[0]}",
        )

    def test_concurrent_move_to_trash_requests_are_serialised(self):
        # Codex P1-2 round 17: ``move_task_to_trash`` now acquires the
        # per-task lock.  Two concurrent trash requests for the same
        # task must serialise so the second one observes the first's
        # ``deletedAt`` state rather than racing the file rename.
        import threading

        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = "WAITING_FOR_CLAUDE"
        task_store.save(task)

        in_flight: list[int] = []
        max_in_flight: list[int] = [0]
        counter_lock = threading.Lock()

        def slow_load(task_id):
            with counter_lock:
                in_flight.append(1)
                if len(in_flight) > max_in_flight[0]:
                    max_in_flight[0] = len(in_flight)
            time.sleep(0.2)
            with counter_lock:
                in_flight.pop()
            return _original_load(task_id)

        _original_load = task_store.load

        results: dict[str, object] = {}
        errors: dict[str, Exception] = {}

        def worker(label):
            try:
                results[label] = server.move_task_to_trash(task.id, task_store)
            except Exception as exc:  # noqa: BLE001
                errors[label] = exc

        def fake_rename(src, dst):
            # Simulate the rename succeeding; the real implementation
            # uses ``Path.rename`` which the test patcher may break
            # under Windows.  Allow either success (first call) or
            # a no-op (second call when the dir is already moved).
            try:
                import pathlib
                pathlib.Path(src).rename(dst)
            except OSError:
                pass
            return dst

        with mock.patch.object(task_store, "load", side_effect=slow_load), \
             mock.patch("pathlib.Path.rename", autospec=True, side_effect=fake_rename):
            threads = [
                threading.Thread(target=worker, args=("a",)),
                threading.Thread(target=worker, args=("b",)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # At most one of the two requests should succeed; the per-task
        # lock must serialise them so the second one observes the
        # first's mutation rather than racing it.
        total_success = len(results)
        total_errors = len(errors)
        self.assertEqual(total_success + total_errors, 2)
        # Critical: no concurrent in-flight loads (max 1).
        self.assertEqual(
            max_in_flight[0],
            1,
            f"expected concurrent move_to_trash to be serialised, max in-flight was {max_in_flight[0]}",
        )

    def _write_launch_prompt(self, task_store, task):
        # ``launch_claude_task`` requires the prompt file to exist; the
        # default ``create_task`` flow writes it, but tests that
        # short-circuit ``create_task`` may need to add it manually.
        task_dir = task_store.task_dir(task.id)
        task_dir.mkdir(parents=True, exist_ok=True)
        prompt_name = (
            "CLAUDE_IMPLEMENT_PROMPT.md"
            if task.round == 1
            else f"FIX_PROMPT_ROUND_{task.round}.md"
        )
        (task_dir / prompt_name).write_text("do work", encoding="utf-8")

    def test_concurrent_launch_claude_requests_are_serialised(self):
        # Codex P1-4 round 18: the per-task ``RLock`` must cover
        # ``launch_claude_task`` for the same reason it covers commit /
        # merge — duplicate concurrent POSTs to
        # ``/api/tasks/{id}/launch-claude`` can both load the task in
        # ``WAITING_FOR_CLAUDE``, both validate, and both launch CLI
        # windows.  The loser would then save its ``Task`` object and
        # overwrite the winner's ``claudeWindow`` metadata.
        import threading

        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        self._write_launch_prompt(task_store, task)

        launch_calls: list[float] = []
        launch_lock = threading.Lock()

        class FakeAdapter:
            def __init__(self, *_args, **_kwargs):
                pass

            def launch(self, _task, _task_dir, _prompt_path):
                with launch_lock:
                    launch_calls.append(time.time())
                time.sleep(0.2)
                return {"script": "fake-script", "pid": 12345}

        results: dict[str, object] = {}
        errors: dict[str, Exception] = {}

        def worker(label):
            try:
                results[label] = server.launch_claude_task(
                    task.id, project_store, task_store
                )
            except Exception as exc:  # noqa: BLE001 — record for assertion
                errors[label] = exc

        with (
            mock.patch("gui.server.assert_git_work_tree"),
            mock.patch("gui.server.validate_task_project", side_effect=lambda t, _ps: Path(t.projectPath)),
            mock.patch("gui.server.load_settings", return_value={"claudeCommand": "claude"}),
            mock.patch("gui.server.ClaudeCliWindowAdapter", FakeAdapter),
        ):
            threads = [
                threading.Thread(target=worker, args=("a",)),
                threading.Thread(target=worker, args=("b",)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # Exactly one ``launch`` call should have been made: the second
        # request must observe the first's status change inside the lock
        # and short-circuit with a state-transition error.
        self.assertEqual(
            len(launch_calls),
            1,
            f"expected exactly one adapter launch call, got {len(launch_calls)}",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        loser_err = next(iter(errors.values()))
        self.assertIsInstance(loser_err, (server.StateTransitionError, server.ApiError))

    def test_concurrent_launch_codex_requests_are_serialised(self):
        # Codex P1-4 round 18: same invariant as launch-claude but for
        # the Codex flow.  Without the per-task lock, two concurrent
        # ``launch-codex`` POSTs can both write the marker file, both
        # launch CLI windows, and both save ``codexWindow`` metadata —
        # the loser overwriting the winner.
        import threading

        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = Status.WAITING_FOR_CODEX
        task.reviewedRound = task.round
        task.reviewedHeadSha = "h"
        task.reviewedStatusHash = "s"
        task.reviewedDiffHash = "d"
        task.reviewedTreeSha = "t"
        task_store.save(task)
        task_dir = task_store.task_dir(task.id)
        (task_dir / "CODEX_REVIEW_PROMPT.md").write_text("review", encoding="utf-8")

        launch_calls: list[float] = []
        launch_lock = threading.Lock()

        class FakeAdapter:
            def __init__(self, *_args, **_kwargs):
                pass

            def launch(self, _task, _task_dir, _prompt_path, _output_path):
                with launch_lock:
                    launch_calls.append(time.time())
                time.sleep(0.2)
                return {"script": "fake-script", "pid": 54321}

        results: dict[str, object] = {}
        errors: dict[str, Exception] = {}

        def worker(label):
            try:
                results[label] = server.launch_codex_task(
                    task.id, project_store, task_store
                )
            except Exception as exc:  # noqa: BLE001 — record for assertion
                errors[label] = exc

        with (
            mock.patch("gui.server.assert_git_work_tree"),
            mock.patch("gui.server.validate_task_project", side_effect=lambda t, _ps: Path(t.projectPath)),
            mock.patch("gui.server.load_settings", return_value={"codexCommand": "codex"}),
            mock.patch("gui.server.CodexCliWindowAdapter", FakeAdapter),
        ):
            threads = [
                threading.Thread(target=worker, args=("a",)),
                threading.Thread(target=worker, args=("b",)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(
            len(launch_calls),
            1,
            f"expected exactly one adapter launch call, got {len(launch_calls)}",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)

    def test_concurrent_claude_completion_and_cancel_are_serialised(self):
        # Codex P1-4 round 18: ``complete_claude_task`` and
        # ``cancel_task`` mutate the same task object.  Without the
        # per-task lock they race: completion may capture artifacts
        # while cancel is moving the task to CANCELLED, and the loser
        # overwrites the winner's status / metadata.
        import threading

        _root, _project, project_store, task_store = self.make_project_store()
        task = self.create_waiting_task(project_store, task_store)
        task.status = Status.CLAUDE_WINDOW_STARTED
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

        # Slow down artifact collection so both threads are guaranteed
        # to be in the critical section simultaneously unless the
        # per-task lock serialises them.
        original_collect = fake_collect

        def slow_collect(*args, **kwargs):
            time.sleep(0.2)
            return original_collect(*args, **kwargs)

        results: dict[str, object] = {}
        errors: dict[str, Exception] = {}

        def worker(label):
            try:
                if label == "complete":
                    results[label] = server.complete_claude_task(
                        task.id, project_store, task_store
                    )
                else:
                    results[label] = server.cancel_task(task.id, task_store)
            except Exception as exc:  # noqa: BLE001 — record for assertion
                errors[label] = exc

        with (
            mock.patch("gui.server.assert_git_work_tree"),
            mock.patch("gui.server.collect_git_artifacts", side_effect=slow_collect),
            mock.patch("gui.server.run_tests", side_effect=fake_tests),
        ):
            threads = [
                threading.Thread(target=worker, args=("complete",)),
                threading.Thread(target=worker, args=("cancel",)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # Exactly one of the two operations should have produced the
        # final state; the other must observe the mutation inside the
        # lock and either error out (state transition refused) or
        # surface a different status.
        total_success = len(results)
        total_errors = len(errors)
        self.assertEqual(total_success + total_errors, 2)
        # If both succeeded, the final persisted status must be one or
        # the other (not a corrupt merge of both).
        final_task = task_store.load(task.id)
        self.assertIn(
            final_task.status,
            {Status.WAITING_FOR_CODEX, Status.CANCELLED, Status.FAILED},
            f"unexpected final status after concurrent cancel+complete: {final_task.status}",
        )


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

    def test_terminal_stream_status_events_do_not_mask_exit_code(self):
        """`::task-status{...}` lines must not interfere with `CLI exit code:` sentinel detection."""
        from unittest import mock as umock

        _root, task_store, task = self.make_store_and_task()
        task.status = Status.CLAUDE_WINDOW_STARTED
        task.round = 1
        task_store.save(task)

        log_path = task_store.task_dir(task.id) / "claude_window_round_1.log"
        # Include an event with escaped quotes / backslashes (per protocol) so
        # the SSE stream is exercised against the escape rules the JS parser
        # must tolerate.
        log_path.write_text(
            "::task-status{phase=\"editing\" message=\"Fixing bug\"}\n"
            "::task-status{phase=\"testing\" message=\"Running pytest\"}\n"
            "::task-status{phase=\"editing\" message=\"He said \\\"hi\\\" and \\\\path\"}\n"
            "CLI exit code: 0\n",
            encoding="utf-8",
        )

        wfile = _FakeWfile(None)

        def flush():
            pass

        with umock.patch("time.sleep", lambda _: None):
            server._terminal_stream(task_store, task.id, "claude", wfile, flush)

        output = b"".join(wfile._buf).decode("utf-8")
        chunks = []
        done_payload = None
        for line in output.splitlines():
            if line.startswith("data: "):
                data = json.loads(line[6:])
                if data.get("chunk"):
                    chunks.append(data["chunk"])
                if data.get("done"):
                    done_payload = data
                    break
        self.assertIsNotNone(done_payload)
        # Status events streamed through unchanged — UI parses them client-side
        combined = "".join(chunks)
        self.assertIn("::task-status{phase=\"editing\" message=\"Fixing bug\"}", combined)
        self.assertIn("::task-status{phase=\"testing\" message=\"Running pytest\"}", combined)
        # Escaped quotes / backslashes must survive server-side forwarding
        # verbatim so the JS escaped-string regex can match them.
        self.assertIn('::task-status{phase="editing" message="He said \\"hi\\" and \\\\path"}', combined)
        # Exit code sentinel still detected on its own line
        self.assertTrue(done_payload["done"])
        self.assertEqual(done_payload["exitCode"], 0)

    def test_terminal_stream_forwards_content_without_trailing_newline(self):
        """A CLI chunk that does not end in `\\n` (prompts, progress bars) must
        still be forwarded by the SSE stream so the xterm renderer can display
        it in real time. The JS chunk processor is responsible for streaming
        such partial lines straight to xterm instead of buffering them."""
        from unittest import mock as umock

        _root, task_store, task = self.make_store_and_task()
        task.status = Status.CLAUDE_WINDOW_STARTED
        task.round = 1
        task_store.save(task)

        log_path = task_store.task_dir(task.id) / "claude_window_round_1.log"
        # Include carriage-return progress updates (no `\n` between them), an
        # unterminated partial line, then the sentinel on its own `\n`-delimited
        # line so the server can detect it and close the stream. write_bytes
        # avoids Windows text-mode newline translation so the `\r` characters
        # reach the SSE stream verbatim.
        log_path.write_bytes(
            (
                "Building... 42%\rBuilding... 87%\r\n"
                "Partial output without newline"
                "\nCLI exit code: 0\n"
            ).encode("utf-8")
        )

        wfile = _FakeWfile(None)

        def flush():
            pass

        with umock.patch("time.sleep", lambda _: None):
            server._terminal_stream(task_store, task.id, "claude", wfile, flush)

        output = b"".join(wfile._buf).decode("utf-8")
        chunks = []
        done_payload = None
        for line in output.splitlines():
            if line.startswith("data: "):
                data = json.loads(line[6:])
                if data.get("chunk"):
                    chunks.append(data["chunk"])
                if data.get("done"):
                    done_payload = data
                    break
        self.assertIsNotNone(done_payload)
        combined = "".join(chunks)
        # Carriage-return progress updates and the unterminated "Partial output"
        # fragment must be forwarded so the client can stream them to xterm
        # instead of buffering until the next newline arrives.
        self.assertIn("Building... 42%\rBuilding... 87%\r\n", combined)
        self.assertIn("Partial output without newline", combined)
        self.assertTrue(done_payload["done"])
        self.assertEqual(done_payload["exitCode"], 0)

    def test_terminal_metadata_handles_status_event_lines(self):
        """Metadata should still detect CLI exit code even when status events precede it."""
        _root, task_store, task = self.make_store_and_task()
        log_path = task_store.task_dir(task.id) / "claude_window_round_1.log"
        log_path.write_text(
            "::task-status{phase=\"done\" message=\"Finished\"}\nCLI exit code: 0\n",
            encoding="utf-8",
        )
        meta = server._terminal_metadata(task_store, task, "claude")
        self.assertTrue(meta["finished"])
        self.assertEqual(meta["exitCode"], 0)


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


class WorktreeRegistrationTests(unittest.TestCase):
    """Codex P2-1 round 17 regression coverage.

    Previously, ``create_project_worktree`` raised a generic ``500``
    error when ``git worktree add`` succeeded but the subsequent
    ``project_store.add_project`` call failed, leaving the new worktree
    directory on disk in an "orphan" state with no project-list entry.
    Round 17 wraps the registration in a recovery flow that retries
    via the primary path's auto-discovery and otherwise returns a
    partial-success payload (HTTP 201 with ``worktreeCreated: true`` and
    ``project: null``) plus recovery instructions.
    """

    def make_project_store(self):
        root = server.ROOT / ".gui" / "test-tmp" / f"wt-reg-{uuid.uuid4().hex}"
        primary = root / "primary"
        primary.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        # Initialize a real git repo at the primary path so worktree
        # creation can succeed.
        subprocess.run(["git", "-C", str(primary), "init"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(primary), "config", "user.email", "test@test.test"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(primary), "config", "user.name", "Test"],
            capture_output=True, check=True,
        )
        (primary / "readme.md").write_text("# primary\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(primary), "add", "readme.md"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(primary), "commit", "-m", "init"],
            capture_output=True, check=True,
        )
        project_store = server.ProjectStore(root / "projects.json")
        project_store.save_projects(
            [
                {
                    "id": "primary1",
                    "name": "Primary",
                    "path": str(primary),
                    "kind": "git-uninitialized",
                    "worktreeType": "primary",
                    "repoId": "repo_wtreg",
                    "available": True,
                }
            ]
        )
        return root, primary, project_store

    def test_worktree_registration_returns_partial_success_when_add_project_fails(self):
        # Simulate a registration failure: ``add_project`` raises.  The
        # retry via primary-path discovery also fails.  The endpoint
        # must return a partial-success payload (no exception) so the
        # frontend can surface the orphan worktree.
        _root, primary, project_store = self.make_project_store()
        target = _root / "linked-wt"
        # Force ``add_project`` to raise every time it is called.
        with mock.patch.object(
            project_store,
            "add_project",
            side_effect=RuntimeError("simulated registration failure"),
        ):
            result = server.create_project_worktree(
                "primary1",
                {"branch": "feature/reg-test", "path": str(target)},
                project_store,
            )
        # Worktree must have been created on disk by ``git worktree add``.
        self.assertTrue(target.is_dir(), "worktree directory must exist after creation")
        # Result must report partial success, not raise.
        self.assertTrue(result["worktreeCreated"])
        self.assertFalse(result["registeredAutomatically"])
        self.assertIsNone(result["project"])
        self.assertEqual(result["branch"], "feature/reg-test")
        self.assertIn("recoveryInstructions", result)
        self.assertIn(str(target), result["recoveryInstructions"])
        # Cleanup the linked worktree so the test does not leak state.
        subprocess.run(
            ["git", "-C", str(primary), "worktree", "remove", "--force", str(target)],
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(primary), "branch", "-D", "feature/reg-test"],
            capture_output=True,
        )

    def test_worktree_registration_recovers_via_primary_path_discovery(self):
        # Simulate the first ``add_project(new_path)`` call failing,
        # but the retry via ``add_project(primary_path)`` succeeds.
        # Because the primary path's project entry already exists in
        # the store, the retry triggers
        # ``_auto_discover_sibling_worktrees`` which scans the worktree
        # list and registers the new linked worktree automatically.
        _root, primary, project_store = self.make_project_store()
        target = _root / "linked-wt-disc"
        original_add_project = project_store.add_project
        call_count = {"n": 0}

        def flaky_add_project(path, name=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First call (the explicit new-path registration) fails.
                raise RuntimeError("simulated first-call failure")
            # Subsequent calls (the retry via primary path) succeed.
            return original_add_project(path, name)

        with mock.patch.object(project_store, "add_project", side_effect=flaky_add_project):
            result = server.create_project_worktree(
                "primary1",
                {"branch": "feature/reg-disc", "path": str(target)},
                project_store,
            )
        self.assertTrue(target.is_dir(), "worktree directory must exist after creation")
        self.assertTrue(result["worktreeCreated"])
        self.assertTrue(result["registeredAutomatically"])
        self.assertIsNotNone(result["project"])
        # Cleanup.
        subprocess.run(
            ["git", "-C", str(primary), "worktree", "remove", "--force", str(target)],
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(primary), "branch", "-D", "feature/reg-disc"],
            capture_output=True,
        )

    def test_worktree_registration_happy_path_records_audit_event(self):
        # Sanity check: when both ``add_project`` calls succeed, the
        # endpoint must return a normal success payload with a non-null
        # project entry, a registered-automatically flag of True, and
        # no recovery instructions.
        _root, primary, project_store = self.make_project_store()
        audit_log = _root / "audit.log"
        target = _root / "linked-wt-happy"
        with mock.patch.object(server, "AUDIT_LOG_FILE", audit_log):
            result = server.create_project_worktree(
                "primary1",
                {"branch": "feature/reg-happy", "path": str(target)},
                project_store,
            )
        self.assertTrue(result["worktreeCreated"])
        self.assertTrue(result["registeredAutomatically"])
        self.assertIsNotNone(result["project"])
        self.assertNotIn("recoveryInstructions", result)
        # Audit log must record the success event (not the partial one).
        entries = [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines()]
        actions = [e["action"] for e in entries]
        self.assertIn("project.worktree.create", actions)
        self.assertNotIn("project.worktree.create.partial", actions)
        # Cleanup.
        subprocess.run(
            ["git", "-C", str(primary), "worktree", "remove", "--force", str(target)],
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(primary), "branch", "-D", "feature/reg-happy"],
            capture_output=True,
        )

    # ------------------------------------------------------------------
    # Codex P1-2 round 19: ``create_project_worktree`` must hold the
    # primary worktree's per-resource ``RLock`` around the complete
    # operation so a concurrent controlled merge cannot advance main
    # between checks and the ``git worktree add`` invocation.  Codex
    # P2-2 round 19: the partial-success response must include the
    # top-level ``path`` field so the frontend can surface the
    # orphan worktree directory.
    # ------------------------------------------------------------------

    def test_worktree_registration_serialises_with_concurrent_merge(self):
        # Codex P1-2 round 19: ``create_project_worktree`` acquires
        # ``_resource_operation_lock(primary_path)`` for the full
        # operation span, while ``merge_task_to_main`` (via
        # ``_merge_task_to_main_locked``) acquires the same lock around
        # the actual Git merge.  The two operations on the same primary
        # repository must therefore serialise — they cannot both be
        # inside their respective critical sections at the same time.
        import threading

        _root, primary, project_store = self.make_project_store()
        target = _root / "linked-wt-serialised"

        # Build a fake task + task_store so we can drive the merge path
        # through ``server.merge_task_to_main``.  The fake
        # ``controlled_merge_to_main`` records timing and sleeps so we
        # can observe overlap (or lack thereof) with the worktree-
        # creation critical section.
        captured_primary = primary.resolve(strict=False)
        captured_target = target.resolve(strict=False)
        # Save a primary project entry that the merge code can resolve.
        project_store.save_projects(
            [
                {
                    "id": "primary1",
                    "name": "Primary",
                    "path": str(primary),
                    "kind": "git-uninitialized",
                    "worktreeType": "primary",
                    "repoId": "repo_wtreg",
                    "available": True,
                    "branch": "master",
                }
            ]
        )

        # Set up the task store + task for the merge call.
        task_store = server.TaskStore(_root / "tasks")
        task = Task(
            id="task_serial",
            projectId="primary1",
            projectPath=str(primary),
            title="serial",
            description="d",
            acceptance="a",
            testCommand="",
            maxRounds=3,
            status=Status.PASS,
            round=1,
            createdAt="2026-01-01T00:00:00Z",
            updatedAt="2026-01-01T00:00:00Z",
        )
        task.worktreeBranch = "feature/serial"
        task.commitSha = "abc"
        task.reviewedRound = 1
        task.reviewedHeadSha = "basesha1"
        # Set repoId so the primary-project lookup succeeds.
        task.repoId = "repo_wtreg"
        task_store.save(task)

        in_critical_section: dict[str, bool] = {"merge": False, "worktree": False}
        overlap_observed = {"value": False}
        lock = threading.Lock()

        def mark_enter(label):
            with lock:
                other = "worktree" if label == "merge" else "merge"
                if in_critical_section[other]:
                    overlap_observed["value"] = True
                in_critical_section[label] = True

        def mark_exit(label):
            with lock:
                in_critical_section[label] = False

        # Patch ``controlled_merge_to_main`` so we can observe when the
        # merge's critical section is active.  The fake returns a valid
        # result so the task's MERGED metadata is set; we do not need
        # real Git work for this concurrency check.
        def fake_merge(*args, **kwargs):
            mark_enter("merge")
            try:
                time.sleep(0.3)  # hold the critical section long enough to observe overlap
                result = {
                    "mergeCommitSha": "deadbeef",
                    "mergeShortSha": "deadbee",
                    "mergeTargetBranch": "master",
                    "mergeSourceBranch": "feature/serial",
                }
                return materialise_mock_merge(result)(*args, **kwargs)
            finally:
                mark_exit("merge")

        # Patch ``create_worktree`` so we can observe when the worktree
        # critical section is active.  Returns a minimal result so
        # registration can proceed.
        def fake_create(*args, **kwargs):
            mark_enter("worktree")
            try:
                time.sleep(0.3)
                return {
                    "path": str(target),
                    "branch": "feature/serial-wt",
                    "worktreeType": "worktree",
                }
            finally:
                mark_exit("worktree")

        results: dict[str, object] = {}
        errors: dict[str, Exception] = {}

        def merge_worker():
            try:
                results["merge"] = server.merge_task_to_main(
                    task.id, project_store, task_store
                )
            except Exception as exc:  # noqa: BLE001 — record for assertion
                errors["merge"] = exc

        def worktree_worker():
            try:
                results["worktree"] = server.create_project_worktree(
                    "primary1",
                    {"branch": "feature/serial-wt", "path": str(target)},
                    project_store,
                )
            except Exception as exc:  # noqa: BLE001 — record for assertion
                errors["worktree"] = exc

        with mock.patch("gui.server.controlled_merge_to_main", side_effect=fake_merge):
            with mock.patch("gui.server.create_worktree", side_effect=fake_create):
                threads = [
                    threading.Thread(target=merge_worker),
                    threading.Thread(target=worktree_worker),
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

        # The two critical sections must not overlap — they hold the
        # same per-resource lock.
        self.assertFalse(
            overlap_observed["value"],
            "create_project_worktree and merge_task_to_main must serialise on the "
            "same per-resource lock; observed overlap.",
        )
        # Cleanup: the fake create_worktree did not actually create the
        # worktree, so no Git cleanup is needed; but reset the task so
        # the merge is not re-run if a later test reuses the store.

    def test_worktree_creation_for_different_repositories_can_overlap(self):
        import threading

        root, primary1, project_store = self.make_project_store()
        primary2 = root / "primary2"
        primary2.mkdir()
        subprocess.run(["git", "-C", str(primary2), "init"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(primary2), "config", "user.email", "test@test.test"],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(primary2), "config", "user.name", "Test"],
            capture_output=True,
            check=True,
        )
        (primary2 / "readme.md").write_text("# second\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(primary2), "add", "readme.md"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(primary2), "commit", "-m", "init"], capture_output=True, check=True)
        projects = project_store.list_projects()
        projects.append(
            {
                "id": "primary2",
                "name": "Primary 2",
                "path": str(primary2),
                "kind": "git-uninitialized",
                "worktreeType": "primary",
                "repoId": "repo_wtreg_2",
                "available": True,
            }
        )
        project_store.save_projects(projects)
        barrier = threading.Barrier(2)
        active = 0
        max_active = 0
        guard = threading.Lock()
        captured_starts: dict[str, str] = {}

        def fake_create(path, branch, target, *, start_sha=None):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
                captured_starts[str(Path(path).resolve())] = start_sha
            try:
                barrier.wait(timeout=3)
                return {"path": str(target), "branch": branch, "worktreeType": "worktree"}
            finally:
                with guard:
                    active -= 1

        errors: list[Exception] = []

        def worker(project_id, branch, target):
            try:
                server.create_project_worktree(
                    project_id, {"branch": branch, "path": str(target)}, project_store
                )
            except Exception as exc:  # noqa: BLE001 — asserted below
                errors.append(exc)

        with mock.patch("gui.server.create_worktree", side_effect=fake_create):
            threads = [
                threading.Thread(
                    target=worker,
                    args=("primary1", "feature/one", root / "wt-one"),
                ),
                threading.Thread(
                    target=worker,
                    args=("primary2", "feature/two", root / "wt-two"),
                ),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertFalse(errors)
        self.assertEqual(max_active, 2)
        for primary in (primary1, primary2):
            expected = subprocess.run(
                ["git", "-C", str(primary), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(captured_starts[str(primary.resolve())], expected)

    def test_worktree_registration_partial_success_includes_top_level_path(self):
        # Codex P2-2 round 19: when ``create_project_worktree`` cannot
        # register the new worktree as a project, the partial-success
        # response must include the top-level ``path`` field (set to
        # the created worktree directory) so the frontend can surface
        # the orphan path even when ``project`` is ``null``.
        _root, primary, project_store = self.make_project_store()
        target = _root / "linked-wt-payload"
        # Force ``add_project`` to raise every time so both registration
        # attempts fail; the endpoint must return a partial-success
        # payload with the top-level ``path`` field set.
        with mock.patch.object(
            project_store,
            "add_project",
            side_effect=RuntimeError("simulated registration failure"),
        ):
            result = server.create_project_worktree(
                "primary1",
                {"branch": "feature/payload-test", "path": str(target)},
                project_store,
            )
        self.assertTrue(result["worktreeCreated"])
        self.assertFalse(result["registeredAutomatically"])
        self.assertIsNone(result["project"])
        # Top-level ``path`` must be present and equal the target path.
        self.assertIn("path", result)
        self.assertEqual(result["path"], str(target))
        # ``branch`` must be the created branch.
        self.assertEqual(result["branch"], "feature/payload-test")
        # Cleanup.
        subprocess.run(
            ["git", "-C", str(primary), "worktree", "remove", "--force", str(target)],
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(primary), "branch", "-D", "feature/payload-test"],
            capture_output=True,
        )


class MergeRecoveryPersistenceTests(unittest.TestCase):
    def make_state(self):
        root = server.ROOT / ".gui" / "test-tmp" / f"recovery-{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        store = TaskStore(root / "tasks", root / "trash")
        task = store.create(
            project_id="project1",
            project_path=str(root),
            title="Recovery",
            description="Recovery test",
            acceptance="Persist all phases",
        )
        task.round = 1
        task.commitSha = "source-sha"
        task.reviewedRound = 1
        task.reviewedHeadSha = "old-sha"
        task.worktreeBranch = "feature/recovery"
        store.save(task)
        journal = server.MergeRecoveryJournal(root / "merge_recovery", "operation-1")
        journal.write(
            phase="materialised",
            task_id=task.id,
            task_round=1,
            primary_path=str(root),
            primary_identity=str(root / ".git"),
            expected_old_head="old-head",
            new_merge_commit_sha="merge-sha",
            source_commit_sha="source-sha",
            reviewed_base_sha="old-sha",
            source_branch="feature/recovery",
            target_branch="main",
        )
        return root, store, task, journal

    def test_journal_survives_task_metadata_save_failure(self):
        _root, store, task, journal = self.make_state()
        with mock.patch.object(store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                server._persist_completed_merge_journal(
                    task, store, journal, journal.read(), project_id="project1"
                )
        self.assertTrue(journal.exists())
        self.assertEqual(journal.read()["phase"], "materialised")

    def test_journal_survives_audit_persistence_failure(self):
        _root, store, task, journal = self.make_state()
        with mock.patch(
            "gui.server._write_merge_audit_once", side_effect=OSError("audit disk full")
        ):
            with self.assertRaises(OSError):
                server._persist_completed_merge_journal(
                    task, store, journal, journal.read(), project_id="project1"
                )
        self.assertTrue(journal.exists())
        self.assertEqual(journal.read()["phase"], "task_persisted")
        self.assertEqual(store.load(task.id).mergeCommitSha, "merge-sha")

    def test_startup_recovery_acquires_task_before_resource_lock(self):
        _root, store, _task, journal = self.make_state()
        events: list[str] = []

        class RecordingLock:
            def __init__(self, label):
                self.label = label

            def __enter__(self):
                events.append(f"enter:{self.label}")

            def __exit__(self, exc_type, exc, tb):
                events.append(f"exit:{self.label}")

        with (
            mock.patch.object(server, "MERGE_RECOVERY_DIR", journal.journal_dir),
            mock.patch(
                "gui.server._task_operation_lock",
                side_effect=lambda _task_id: RecordingLock("task"),
            ),
            mock.patch(
                "gui.server._resource_operation_lock",
                side_effect=lambda _path: RecordingLock("resource"),
            ),
            mock.patch("gui.server._task_journal_identity_error", return_value=None),
            mock.patch(
                "gui.server.recover_pending_merge",
                side_effect=lambda *_args: events.append("recover")
                or {"action": "blocked", "reason": "manual"},
            ),
            mock.patch("gui.server._record_blocked_recovery"),
        ):
            server._recover_pending_merges_at_startup(store)
        self.assertEqual(
            events,
            ["enter:task", "enter:resource", "recover", "exit:resource", "exit:task"],
        )


if __name__ == "__main__":
    unittest.main()
