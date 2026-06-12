import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui.orchestrator.store import TaskStore, TaskStoreError
from gui.orchestrator.models import Task


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / ".gui" / "test-tmp" / uuid.uuid4().hex
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.store = TaskStore(self.root / "tasks")

    def test_create_save_load_and_list_task(self):
        task = self.store.create(
            project_id="project1",
            project_path=str(self.root / "project"),
            title="Implement thing",
            description="Do work",
            acceptance="Pass tests",
            max_rounds=3,
        )
        task.status = "WAITING_FOR_CLAUDE"
        self.store.save(task)

        loaded = self.store.load(task.id)
        self.assertEqual(loaded.title, "Implement thing")
        self.assertEqual(loaded.status, "WAITING_FOR_CLAUDE")
        self.assertEqual([item.id for item in self.store.list_tasks()], [task.id])

    def test_task_id_cannot_escape_tasks_root(self):
        with self.assertRaises(TaskStoreError):
            self.store.task_dir("task_../escape")

    def test_artifact_reader_skips_env_files(self):
        task = self.store.create(
            project_id="project1",
            project_path=str(self.root / "project"),
            title="T",
            description="D",
            acceptance="A",
        )
        task_dir = self.store.task_dir(task.id)
        (task_dir / "safe.txt").write_text("safe", encoding="utf-8")
        (task_dir / ".env").write_text("secret", encoding="utf-8")

        artifacts = self.store.read_artifacts(task.id)
        self.assertIn("safe.txt", artifacts)
        self.assertNotIn(".env", artifacts)

    def test_archive_restore_and_list_filters(self):
        task = self.store.create(
            project_id="project1",
            project_path=str(self.root / "project"),
            title="Archive me",
            description="D",
            acceptance="A",
        )

        archived = self.store.archive(task.id)
        self.assertIsNotNone(archived.archivedAt)
        self.assertEqual(self.store.list_tasks(), [])
        self.assertEqual([item.id for item in self.store.list_tasks(archived=True)], [task.id])

        restored = self.store.restore_archived(task.id)
        self.assertIsNone(restored.archivedAt)
        self.assertEqual([item.id for item in self.store.list_tasks()], [task.id])

    def test_move_to_trash_and_restore(self):
        task = self.store.create(
            project_id="project1",
            project_path=str(self.root / "project"),
            title="Trash me",
            description="D",
            acceptance="A",
        )
        task_dir = self.store.task_dir(task.id)
        (task_dir / "artifact.txt").write_text("kept", encoding="utf-8")

        with mock.patch("pathlib.Path.rename", autospec=True, side_effect=fake_directory_rename):
            trashed = self.store.move_to_trash(task.id)
        self.assertIsNotNone(trashed.deletedAt)
        self.assertEqual([item.id for item in self.store.list_trash_tasks()], [task.id])

        restore_task = Task.create(
            task_id="task_restore000000",
            project_id="project1",
            project_path=str(self.root / "project"),
            title="Restore me",
            description="D",
            acceptance="A",
        )
        restore_task.deletedAt = "2026-06-12T00:00:00Z"
        trash_dir = self.store.trash_task_dir(restore_task.id)
        trash_dir.mkdir(parents=True)
        (trash_dir / "task.json").write_text(
            json.dumps(restore_task.to_dict(), ensure_ascii=False),
            encoding="utf-8",
        )

        with mock.patch("pathlib.Path.rename", autospec=True, side_effect=fake_directory_rename):
            restored = self.store.restore_from_trash(restore_task.id)
        self.assertIsNone(restored.deletedAt)
        self.assertTrue((self.store.task_dir(restore_task.id) / "task.json").is_file())


def fake_directory_rename(source, destination):
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return destination


if __name__ == "__main__":
    unittest.main()
