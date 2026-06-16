import sys
import unittest
import uuid
import shutil
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui.orchestrator.models import Task
from gui.orchestrator.prompts import (
    write_claude_implementation_prompt,
    write_codex_review_prompt,
    write_fix_prompt,
    RUNTIME_PROGRESS_PROTOCOL,
    CLAUDE_PROGRESS_RULES,
    CODEX_PROGRESS_RULES,
)


class PromptProtocolTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / ".gui" / "test-tmp" / uuid.uuid4().hex
        self.project = self.root / "project"
        self.task_dir = self.root / "tasks" / "task_protocol"
        self.project.mkdir(parents=True)
        self.task_dir.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.task = Task.create(
            task_id="task_protocol",
            project_id="project1",
            project_path=str(self.project),
            title="Title",
            description="Description",
            acceptance="Acceptance",
        )

    def test_protocol_constants_mention_event_shape_and_phases(self):
        text = RUNTIME_PROGRESS_PROTOCOL
        self.assertIn("::task-status{phase=\"<phase>\" message=\"<message>\"}", text)
        for phase in [
            "planning", "reading", "running", "editing",
            "testing", "reviewing", "writing", "waiting", "blocked", "done",
        ]:
            self.assertIn(phase, text)
        self.assertIn("CLI exit code:", text)
        # The protocol must document escape rules so the JS parser's
        # escaped-string regex has a contract to match against.
        self.assertIn("\\\\", text)
        self.assertIn("\\\"", text)

    def test_claude_rules_list_required_phases(self):
        text = CLAUDE_PROGRESS_RULES
        for required in ["planning", "reading", "editing", "running", "testing", "writing", "done"]:
            self.assertIn(required, text)
        self.assertIn("docs/IMPLEMENTATION_REPORT.md", text)

    def test_codex_rules_require_pure_json_final_response(self):
        text = CODEX_PROGRESS_RULES
        self.assertIn("reviewing", text)
        self.assertIn("reading", text)
        self.assertIn("blocked", text)
        self.assertIn("done", text)
        self.assertIn("FINAL response MUST be a single JSON object", text)
        self.assertIn("MUST NOT contain any `::task-status` events", text)

    def test_claude_implementation_prompt_includes_protocol(self):
        path = write_claude_implementation_prompt(self.task, self.task_dir)
        content = path.read_text(encoding="utf-8")
        self.assertIn("Runtime Terminal Progress Protocol", content)
        self.assertIn("::task-status{phase=\"<phase>\" message=\"<message>\"}", content)
        self.assertIn("planning", content)
        self.assertIn("editing", content)
        # Safety rules must still be present
        self.assertIn("Do not modify `.git`", content)
        # Required work items must still be present
        self.assertIn("docs/IMPLEMENTATION_REPORT.md", content)

    def test_codex_review_prompt_includes_protocol_and_json_constraint(self):
        # Seed artifacts so the prompt has something to embed
        (self.task_dir / f"git_status_round_{self.task.round}.txt").write_text(" M app.py\n", encoding="utf-8")
        (self.task_dir / f"git_diff_stat_round_{self.task.round}.txt").write_text(" app.py | 1 +\n", encoding="utf-8")
        (self.task_dir / f"git_diff_round_{self.task.round}.diff").write_text("diff --git a/app.py b/app.py\n", encoding="utf-8")
        (self.task_dir / f"test_results_round_{self.task.round}.txt").write_text("EXIT_CODE: 0\n", encoding="utf-8")

        path = write_codex_review_prompt(self.task, self.task_dir)
        content = path.read_text(encoding="utf-8")
        self.assertIn("Runtime Terminal Progress Protocol", content)
        self.assertIn("::task-status{phase=\"<phase>\" message=\"<message>\"}", content)
        self.assertIn("reviewing", content)
        # Codex must NOT edit files (existing rule preserved)
        self.assertIn("Do not edit files", content)
        # Final response must be pure JSON, no status events
        self.assertIn("single JSON object", content)
        self.assertIn("MUST NOT contain `::task-status` events", content)

    def test_fix_prompt_includes_protocol(self):
        review = {
            "status": "NEEDS_FIX",
            "findings": [
                {"id": "P1-1", "severity": "P1", "file": "app.py", "description": "bug"},
            ],
        }
        (self.task_dir / f"git_diff_round_{self.task.round}.diff").write_text("diff\n", encoding="utf-8")
        (self.task_dir / f"test_results_round_{self.task.round}.txt").write_text("tests\n", encoding="utf-8")

        path = write_fix_prompt(self.task, self.task_dir, review, next_round=2)
        content = path.read_text(encoding="utf-8")
        self.assertIn("Runtime Terminal Progress Protocol", content)
        self.assertIn("::task-status{phase=\"<phase>\" message=\"<message>\"}", content)
        self.assertIn("editing", content)
        # Existing fix requirements preserved
        self.assertIn("Update `docs/IMPLEMENTATION_REPORT.md`", content)
        self.assertIn("Codex Findings", content)


if __name__ == "__main__":
    unittest.main()
