from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

from .models import Task, utc_now
from .path_safety import PathSafetyError, ensure_child_path, path_has_env_segment


class TaskStoreError(ValueError):
    pass


class TaskStore:
    def __init__(self, tasks_root: Path, trash_root: Path | None = None):
        self.tasks_root = tasks_root
        self.trash_root = trash_root or tasks_root.parent / "trash" / "tasks"
        self._lock = threading.RLock()

    def _reject_dangerous_root(self, root: Path) -> Path:
        resolved = root.resolve(strict=False)
        if resolved == Path(resolved.anchor):
            raise TaskStoreError("Refusing to operate on a disk root.")
        return resolved

    def _ensure_task_id(self, task_id: str) -> None:
        if not task_id.startswith("task_") or any(part in task_id for part in ("/", "\\", "..")):
            raise TaskStoreError("Invalid task id.")

    def task_dir(self, task_id: str) -> Path:
        self._ensure_task_id(task_id)
        path = self.tasks_root / task_id
        try:
            return ensure_child_path(self._reject_dangerous_root(self.tasks_root), path)
        except PathSafetyError as exc:
            raise TaskStoreError(str(exc)) from exc

    def trash_task_dir(self, task_id: str) -> Path:
        self._ensure_task_id(task_id)
        path = self.trash_root / task_id
        try:
            return ensure_child_path(self._reject_dangerous_root(self.trash_root), path)
        except PathSafetyError as exc:
            raise TaskStoreError(str(exc)) from exc

    def task_json_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "task.json"

    def _read_task_json(self, path: Path) -> Task:
        return Task.from_dict(json.loads(path.read_text(encoding="utf-8-sig")))

    def list_tasks(self, *, archived: bool = False, project_id: str | None = None) -> list[Task]:
        with self._lock:
            if not self.tasks_root.exists():
                return []
            tasks: list[Task] = []
            for path in sorted(self.tasks_root.glob("task_*/task.json"), reverse=True):
                try:
                    task = self._read_task_json(path)
                except (OSError, KeyError, TypeError, json.JSONDecodeError, ValueError):
                    continue
                if task.deletedAt:
                    continue
                if bool(task.archivedAt) != archived:
                    continue
                if project_id and task.projectId != project_id:
                    continue
                tasks.append(task)
            return tasks

    def list_trash_tasks(self) -> list[Task]:
        with self._lock:
            if not self.trash_root.exists():
                return []
            tasks: list[Task] = []
            for path in sorted(self.trash_root.glob("task_*/task.json"), reverse=True):
                try:
                    task = self._read_task_json(path)
                except (OSError, KeyError, TypeError, json.JSONDecodeError, ValueError):
                    continue
                tasks.append(task)
            return tasks

    def load(self, task_id: str) -> Task:
        with self._lock:
            path = self.task_json_path(task_id)
            if not path.is_file():
                raise TaskStoreError("Task not found.")
            try:
                return self._read_task_json(path)
            except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
                raise TaskStoreError("Task file is invalid.") from exc

    def save(self, task: Task) -> Task:
        with self._lock:
            task_dir = self.task_dir(task.id)
            task_dir.mkdir(parents=True, exist_ok=True)
            path = task_dir / "task.json"
            path.write_text(json.dumps(task.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            return task

    def _write_task_json(self, task_dir: Path, task: Task) -> None:
        ensure_child_path(task_dir, task_dir / "task.json")
        (task_dir / "task.json").write_text(
            json.dumps(task.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def create(
        self,
        *,
        project_id: str,
        project_path: str,
        title: str,
        description: str,
        acceptance: str,
        test_command: str = "",
        max_rounds: int = 3,
    ) -> Task:
        if not title.strip():
            raise TaskStoreError("Task title is required.")
        if not description.strip():
            raise TaskStoreError("Task description is required.")
        task_id = f"task_{uuid.uuid4().hex[:16]}"
        task = Task.create(
            task_id=task_id,
            project_id=project_id,
            project_path=project_path,
            title=title,
            description=description,
            acceptance=acceptance,
            test_command=test_command,
            max_rounds=max_rounds,
        )
        self.save(task)
        return task

    def archive(self, task_id: str) -> Task:
        with self._lock:
            task = self.load(task_id)
            if task.archivedAt:
                return task
            task.archivedAt = utc_now()
            task.add_history("ARCHIVED", "Task archived.")
            return self.save(task)

    def restore_archived(self, task_id: str) -> Task:
        with self._lock:
            task = self.load(task_id)
            if not task.archivedAt:
                return task
            task.archivedAt = None
            task.add_history("RESTORED", "Task restored from archive.")
            return self.save(task)

    def move_to_trash(self, task_id: str) -> Task:
        with self._lock:
            task = self.load(task_id)
            source = self.task_dir(task.id)
            if not source.is_dir():
                raise TaskStoreError("Task directory not found.")
            destination = self.trash_task_dir(task.id)
            if destination.exists():
                raise TaskStoreError("Task already exists in trash.")
            ensure_child_path(self._reject_dangerous_root(self.tasks_root), source)
            ensure_child_path(self._reject_dangerous_root(self.trash_root), destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                source.rename(destination)
            except OSError as exc:
                raise TaskStoreError(f"Failed to move task directory to trash: {exc}") from exc
            task.deletedAt = utc_now()
            task.archivedAt = None
            task.trashPath = str(destination)
            task.add_history("MOVED_TO_TRASH", "Task record moved to application trash.")
            self._write_task_json(destination, task)
            return task

    def restore_from_trash(self, task_id: str) -> Task:
        with self._lock:
            source = self.trash_task_dir(task_id)
            path = source / "task.json"
            if not path.is_file():
                raise TaskStoreError("Trash task not found.")
            try:
                task = self._read_task_json(path)
            except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
                raise TaskStoreError("Trash task file is invalid.") from exc
            destination = self.task_dir(task.id)
            if destination.exists():
                raise TaskStoreError("An active task with the same id already exists.")
            ensure_child_path(self._reject_dangerous_root(self.trash_root), source)
            ensure_child_path(self._reject_dangerous_root(self.tasks_root), destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                source.rename(destination)
            except OSError as exc:
                raise TaskStoreError(f"Failed to restore task directory from trash: {exc}") from exc
            task.deletedAt = None
            task.trashPath = None
            task.add_history("RESTORED_FROM_TRASH", "Task restored from application trash.")
            self._write_task_json(destination, task)
            return task

    def read_artifacts(self, task_id: str) -> dict[str, dict[str, Any]]:
        task_dir = self.task_dir(task_id)
        artifacts: dict[str, dict[str, Any]] = {}
        if not task_dir.exists():
            return artifacts
        for path in sorted(task_dir.iterdir()):
            if not path.is_file():
                continue
            rel = path.name
            if path_has_env_segment(rel):
                continue
            try:
                ensure_child_path(task_dir, path)
            except PathSafetyError:
                continue
            artifacts[rel] = {
                "path": rel,
                "exists": True,
                "content": path.read_text(encoding="utf-8", errors="replace"),
            }
        return artifacts
