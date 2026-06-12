from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class Task:
    id: str
    projectId: str
    projectPath: str
    title: str
    description: str
    acceptance: str
    testCommand: str
    status: str
    round: int
    maxRounds: int
    createdAt: str
    updatedAt: str
    claudeWindow: dict[str, Any] | None = None
    codexWindow: dict[str, Any] | None = None
    archivedAt: str | None = None
    deletedAt: str | None = None
    trashPath: str | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        *,
        task_id: str,
        project_id: str,
        project_path: str,
        title: str,
        description: str,
        acceptance: str,
        test_command: str = "",
        max_rounds: int = 3,
    ) -> "Task":
        now = utc_now()
        task = cls(
            id=task_id,
            projectId=project_id,
            projectPath=project_path,
            title=title.strip(),
            description=description.strip(),
            acceptance=acceptance.strip(),
            testCommand=test_command.strip(),
            status="CREATED",
            round=1,
            maxRounds=max(1, min(10, int(max_rounds))),
            createdAt=now,
            updatedAt=now,
        )
        task.add_history("CREATED", "Task created.")
        return task

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        return cls(
            id=str(data["id"]),
            projectId=str(data["projectId"]),
            projectPath=str(data["projectPath"]),
            title=str(data.get("title") or ""),
            description=str(data.get("description") or ""),
            acceptance=str(data.get("acceptance") or ""),
            testCommand=str(data.get("testCommand") or ""),
            status=str(data["status"]),
            round=int(data.get("round") or 1),
            maxRounds=int(data.get("maxRounds") or 3),
            createdAt=str(data.get("createdAt") or utc_now()),
            updatedAt=str(data.get("updatedAt") or utc_now()),
            claudeWindow=data.get("claudeWindow"),
            codexWindow=data.get("codexWindow"),
            archivedAt=data.get("archivedAt"),
            deletedAt=data.get("deletedAt"),
            trashPath=data.get("trashPath"),
            artifacts=list(data.get("artifacts") or []),
            history=list(data.get("history") or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "projectId": self.projectId,
            "projectPath": self.projectPath,
            "title": self.title,
            "description": self.description,
            "acceptance": self.acceptance,
            "testCommand": self.testCommand,
            "status": self.status,
            "round": self.round,
            "maxRounds": self.maxRounds,
            "createdAt": self.createdAt,
            "updatedAt": self.updatedAt,
            "claudeWindow": self.claudeWindow,
            "codexWindow": self.codexWindow,
            "archivedAt": self.archivedAt,
            "deletedAt": self.deletedAt,
            "trashPath": self.trashPath,
            "artifacts": self.artifacts,
            "history": self.history,
        }

    def add_history(self, event: str, message: str, **extra: Any) -> None:
        item = {"at": utc_now(), "event": event, "message": message}
        item.update(extra)
        self.history.append(item)
        self.updatedAt = item["at"]

    def set_status(self, status: str, message: str) -> None:
        previous = self.status
        self.status = status
        self.add_history("STATUS_CHANGED", message, previous=previous, status=status)

    def add_artifact(self, name: str, path: str, kind: str = "text") -> None:
        existing = next((item for item in self.artifacts if item.get("name") == name), None)
        payload = {"name": name, "path": path, "kind": kind}
        if existing:
            existing.update(payload)
        else:
            self.artifacts.append(payload)
        self.updatedAt = utc_now()
