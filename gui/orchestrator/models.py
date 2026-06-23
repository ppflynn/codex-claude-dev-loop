from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _progress_for_status(status: str) -> int:
    """Derive a default progress value from status for legacy task records."""
    if status in ("CLAUDE_WINDOW_STARTED",):
        return 20
    if status in ("WAITING_FOR_CODEX",):
        return 50
    if status in ("CODEX_WINDOW_STARTED",):
        return 60
    if status in ("PASS", "BLOCKED", "FAILED", "CANCELLED"):
        return 100
    if status in ("NEEDS_FIX", "WAITING_FOR_CLAUDE"):
        return 20
    return 0


def _stage_for_status(status: str) -> str:
    """Derive a default stage from status for legacy task records."""
    if status in ("CLAUDE_WINDOW_STARTED",):
        return "claude_running"
    if status in ("WAITING_FOR_CODEX",):
        return "waiting_for_codex"
    if status in ("CODEX_WINDOW_STARTED",):
        return "codex_running"
    if status in ("PASS", "BLOCKED"):
        return "review_complete"
    if status in ("FAILED",):
        return "no_changes"
    if status in ("CANCELLED",):
        return "cancelled"
    if status in ("NEEDS_FIX", "WAITING_FOR_CLAUDE"):
        return "fix_round"
    return "created"


def _client_for_status(status: str) -> str | None:
    """Derive a default activeClient from status for legacy task records."""
    if status in ("CLAUDE_WINDOW_STARTED",):
        return "claude"
    if status in ("CODEX_WINDOW_STARTED",):
        return "codex"
    return None


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
    progress: int = 0
    stage: str = ""
    activeClient: str | None = None
    lastActivityAt: str | None = None
    repoId: str | None = None
    worktreeType: str | None = None
    worktreeBranch: str | None = None
    commitSha: str | None = None
    commitShortSha: str | None = None
    commitMessage: str | None = None
    committedAt: str | None = None
    mergedAt: str | None = None
    mergeCommitSha: str | None = None
    mergeShortSha: str | None = None
    mergeTargetBranch: str | None = None
    mergeSourceBranch: str | None = None
    # Codex P2-2 round 18: SHA the primary worktree HEAD moved to after
    # the controlled merge's CAS ref update.  ``None`` on the clean
    # path; a non-empty SHA signals that the recorded ``mergeCommitSha``
    # may no longer be the branch tip and forensic review of the audit
    # trail may need to reconcile the two.  Surfaced on the ``MERGED``
    # history event and the ``task.merge`` audit log entry when set.
    headDriftSha: str | None = None
    reviewedRound: int | None = None
    reviewedHeadSha: str | None = None
    reviewedStatusHash: str | None = None
    reviewedDiffHash: str | None = None
    reviewedTreeSha: str | None = None

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
            maxRounds=max(1, min(15, int(max_rounds))),
            createdAt=now,
            updatedAt=now,
        )
        task.progress = 0
        task.stage = "created"
        task.activeClient = None
        task.lastActivityAt = now
        task.add_history("CREATED", "Task created.")
        return task

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        status = str(data["status"])
        has_progress = "progress" in data
        has_stage = "stage" in data
        has_client = "activeClient" in data
        has_last_activity = "lastActivityAt" in data

        return cls(
            id=str(data["id"]),
            projectId=str(data["projectId"]),
            projectPath=str(data["projectPath"]),
            title=str(data.get("title") or ""),
            description=str(data.get("description") or ""),
            acceptance=str(data.get("acceptance") or ""),
            testCommand=str(data.get("testCommand") or ""),
            status=status,
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
            progress=int(data["progress"]) if has_progress else _progress_for_status(status),
            stage=str(data["stage"]) if has_stage else _stage_for_status(status),
            activeClient=data["activeClient"] if has_client else _client_for_status(status),
            lastActivityAt=(
                data["lastActivityAt"] if has_last_activity
                else (data.get("updatedAt") or data.get("createdAt") or utc_now())
            ),
            repoId=data.get("repoId"),
            worktreeType=data.get("worktreeType"),
            worktreeBranch=data.get("worktreeBranch"),
            commitSha=data.get("commitSha"),
            commitShortSha=data.get("commitShortSha"),
            commitMessage=data.get("commitMessage"),
            committedAt=data.get("committedAt"),
            mergedAt=data.get("mergedAt"),
            mergeCommitSha=data.get("mergeCommitSha"),
            mergeShortSha=data.get("mergeShortSha"),
            mergeTargetBranch=data.get("mergeTargetBranch"),
            mergeSourceBranch=data.get("mergeSourceBranch"),
            headDriftSha=data.get("headDriftSha"),
            reviewedRound=(
                int(data["reviewedRound"])
                if data.get("reviewedRound") is not None
                else None
            ),
            reviewedHeadSha=data.get("reviewedHeadSha"),
            reviewedStatusHash=data.get("reviewedStatusHash"),
            reviewedDiffHash=data.get("reviewedDiffHash"),
            reviewedTreeSha=data.get("reviewedTreeSha"),
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
            "progress": self.progress,
            "stage": self.stage,
            "activeClient": self.activeClient,
            "lastActivityAt": self.lastActivityAt,
            "repoId": self.repoId,
            "worktreeType": self.worktreeType,
            "worktreeBranch": self.worktreeBranch,
            "commitSha": self.commitSha,
            "commitShortSha": self.commitShortSha,
            "commitMessage": self.commitMessage,
            "committedAt": self.committedAt,
            "mergedAt": self.mergedAt,
            "mergeCommitSha": self.mergeCommitSha,
            "mergeShortSha": self.mergeShortSha,
            "mergeTargetBranch": self.mergeTargetBranch,
            "mergeSourceBranch": self.mergeSourceBranch,
            "headDriftSha": self.headDriftSha,
            "reviewedRound": self.reviewedRound,
            "reviewedHeadSha": self.reviewedHeadSha,
            "reviewedStatusHash": self.reviewedStatusHash,
            "reviewedDiffHash": self.reviewedDiffHash,
            "reviewedTreeSha": self.reviewedTreeSha,
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
