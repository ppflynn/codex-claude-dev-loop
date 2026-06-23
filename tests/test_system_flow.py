import json
import shutil
import sys
import threading
import time
import unittest
import urllib.request
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui import server
from gui.orchestrator.git_tools import GitArtifacts
from gui.orchestrator.store import TaskStore
from gui.orchestrator.test_runner import TestRunResult


class HttpSystemFlowTests(unittest.TestCase):
    def setUp(self):
        self.root = server.ROOT / ".gui" / "test-tmp" / f"system-{uuid.uuid4().hex}"
        self.project = self.root / "project"
        self.project.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))

        self.old_store = server.GuiHandler.store
        self.old_tasks = server.GuiHandler.tasks
        self.old_runs = server.GuiHandler.runs

        self.project_store = server.ProjectStore(self.root / "projects.json")
        self.task_store = TaskStore(self.root / "tasks")
        server.GuiHandler.store = self.project_store
        server.GuiHandler.tasks = self.task_store
        server.GuiHandler.runs = server.RunManager(self.project_store)

        self.httpd = server.create_server("127.0.0.1", 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        server.GuiHandler.store = self.old_store
        server.GuiHandler.tasks = self.old_tasks
        server.GuiHandler.runs = self.old_runs

    def api(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))

    @contextmanager
    def patched_boundaries(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch("gui.server.is_inside_git_repo", return_value=True))
            stack.enter_context(mock.patch("gui.server.assert_git_work_tree"))
            stack.enter_context(mock.patch("gui.server.assert_clean_work_tree"))
            stack.enter_context(mock.patch("gui.server.collect_git_artifacts", side_effect=fake_collect_git_artifacts))
            stack.enter_context(mock.patch("gui.server.run_tests", side_effect=fake_run_tests))
            # Snapshot is captured at Claude completion and verified on Codex
            # PASS.  A stable fake ensures the PASS-time drift check passes.
            stack.enter_context(
                mock.patch(
                    "gui.server.compute_review_snapshot",
                    side_effect=fake_compute_review_snapshot,
                )
            )
            stack.enter_context(
                mock.patch(
                    "gui.server.load_settings",
                    return_value={"claudeCommand": ["fake-claude"], "codexCommand": ["fake-codex"]},
                )
            )
            stack.enter_context(mock.patch("gui.server.ClaudeCliWindowAdapter.launch", fake_launch_claude))
            stack.enter_context(mock.patch("gui.server.CodexCliWindowAdapter.launch", fake_launch_codex))
            yield

    def import_project(self):
        response = self.api("POST", "/api/projects", {"path": str(self.project)})
        return response["project"]

    def create_task(self, project_id, title="Synthetic task", max_rounds=3):
        response = self.api(
            "POST",
            "/api/tasks",
            {
                "projectId": project_id,
                "title": title,
                "description": "Exercise the complete local collaboration flow.",
                "acceptance": "The task reaches a terminal review state.",
                "testCommand": "fake-test",
                "maxRounds": max_rounds,
            },
        )
        return response["task"]

    def write_review(self, task_id, status, findings=None):
        review = {
            "status": status,
            "reviewed_at": "2026-06-12T00:00:00Z",
            "summary": status,
            "findings": findings or [],
        }
        path = self.task_store.task_dir(task_id) / "CODEX_REVIEW.json"
        # Sleep briefly so the review file's mtime is strictly newer than the
        # codex_output_started marker written by launch-codex.  Without this,
        # the staleness guard in complete_codex_task (review mtime <= marker
        # mtime) can race on filesystems with coarse mtime resolution and
        # reject the freshly written review.
        time.sleep(0.05)
        path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")

    def test_http_pass_flow_runs_from_project_import_to_terminal_pass(self):
        with self.patched_boundaries():
            project = self.import_project()
            task = self.create_task(project["id"])

            self.assertEqual(task["status"], "WAITING_FOR_CLAUDE")
            task_id = task["id"]

            task = self.api("POST", f"/api/tasks/{task_id}/launch-claude", {})["task"]
            self.assertEqual(task["status"], "CLAUDE_WINDOW_STARTED")

            task = self.api("POST", f"/api/tasks/{task_id}/claude-completed", {})["task"]
            self.assertEqual(task["status"], "WAITING_FOR_CODEX")

            artifacts = self.api("GET", f"/api/tasks/{task_id}/artifacts")["artifacts"]
            self.assertIn("CLAUDE_IMPLEMENT_PROMPT.md", artifacts)
            self.assertIn("CODEX_REVIEW_PROMPT.md", artifacts)
            self.assertIn("git_diff_round_1.diff", artifacts)
            self.assertIn("test_results_round_1.txt", artifacts)

            task = self.api("POST", f"/api/tasks/{task_id}/launch-codex", {})["task"]
            self.assertEqual(task["status"], "CODEX_WINDOW_STARTED")

            self.write_review(task_id, "PASS")
            task = self.api("POST", f"/api/tasks/{task_id}/codex-completed", {})["task"]
            self.assertEqual(task["status"], "PASS")

    def test_http_needs_fix_flow_generates_fix_round_and_can_pass_second_review(self):
        with self.patched_boundaries():
            project = self.import_project()
            task = self.create_task(project["id"], title="Fix flow", max_rounds=3)
            task_id = task["id"]

            task = self.api("POST", f"/api/tasks/{task_id}/launch-claude", {})["task"]
            self.assertEqual(task["status"], "CLAUDE_WINDOW_STARTED")
            task = self.api("POST", f"/api/tasks/{task_id}/claude-completed", {})["task"]
            self.assertEqual(task["status"], "WAITING_FOR_CODEX")
            task = self.api("POST", f"/api/tasks/{task_id}/launch-codex", {})["task"]
            self.assertEqual(task["status"], "CODEX_WINDOW_STARTED")

            self.write_review(
                task_id,
                "NEEDS_FIX",
                [
                    {
                        "id": "P1-1",
                        "severity": "P1",
                        "file": "app.py",
                        "description": "Synthetic issue.",
                    }
                ],
            )
            task = self.api("POST", f"/api/tasks/{task_id}/codex-completed", {})["task"]
            self.assertEqual(task["status"], "WAITING_FOR_CLAUDE")
            self.assertEqual(task["round"], 2)
            self.assertTrue((self.task_store.task_dir(task_id) / "FIX_PROMPT_ROUND_2.md").exists())

            task = self.api("POST", f"/api/tasks/{task_id}/launch-claude", {})["task"]
            self.assertEqual(task["status"], "CLAUDE_WINDOW_STARTED")
            self.assertIn("FIX_PROMPT_ROUND_2.md", task["artifacts"][-2]["path"])

            task = self.api("POST", f"/api/tasks/{task_id}/claude-completed", {})["task"]
            self.assertEqual(task["status"], "WAITING_FOR_CODEX")
            self.assertTrue((self.task_store.task_dir(task_id) / "git_diff_round_2.diff").exists())

            task = self.api("POST", f"/api/tasks/{task_id}/launch-codex", {})["task"]
            self.assertEqual(task["status"], "CODEX_WINDOW_STARTED")

            self.write_review(task_id, "PASS")
            task = self.api("POST", f"/api/tasks/{task_id}/codex-completed", {})["task"]
            self.assertEqual(task["status"], "PASS")
            self.assertEqual(task["round"], 2)


def fake_collect_git_artifacts(_project_path, task_dir, round_number):
    status_path = task_dir / f"git_status_round_{round_number}.txt"
    diff_stat_path = task_dir / f"git_diff_stat_round_{round_number}.txt"
    diff_path = task_dir / f"git_diff_round_{round_number}.diff"
    status_path.write_text(" M app.py\n", encoding="utf-8")
    diff_stat_path.write_text(" app.py | 1 +\n", encoding="utf-8")
    diff_path.write_text(f"diff --git a/app.py b/app.py\n+round {round_number}\n", encoding="utf-8")
    return GitArtifacts(status_path, diff_stat_path, diff_path, "", "", "")


def fake_compute_review_snapshot(_project_path):
    # Return a stable, identical snapshot on every call so the PASS-time
    # drift check passes (Claude-completion snapshot == Codex-PASS snapshot).
    return {
        "headSha": "fake-head-sha",
        "statusHash": "fake-status-hash",
        "diffHash": "fake-diff-hash",
        "treeSha": "fake-tree-sha",
    }


def fake_run_tests(_project_path, task_dir, round_number, _command):
    path = task_dir / f"test_results_round_{round_number}.txt"
    output = f"COMMAND: fake-test\nEXIT_CODE: 0\nROUND: {round_number}\n"
    path.write_text(output, encoding="utf-8")
    return TestRunResult(["fake-test"], 0, output, path)


def fake_launch_claude(_adapter, task, task_dir, prompt_path):
    script = task_dir / f"fake_claude_window_round_{task.round}.ps1"
    script.write_text(f"prompt={prompt_path.name}\n", encoding="utf-8")
    return {"script": str(script), "command": ["fake-claude"], "pid": 111, "cliAvailable": True}


def fake_launch_codex(_adapter, task, task_dir, prompt_path, output_path):
    script = task_dir / f"fake_codex_window_round_{task.round}.ps1"
    script.write_text(f"prompt={prompt_path.name}\noutput={output_path.name}\n", encoding="utf-8")
    return {"script": str(script), "command": ["fake-codex"], "pid": 222, "cliAvailable": True}


if __name__ == "__main__":
    unittest.main()
