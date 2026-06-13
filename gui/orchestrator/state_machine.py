from __future__ import annotations

from typing import Final


class Status:
    CREATED: Final = "CREATED"
    WAITING_FOR_CLAUDE: Final = "WAITING_FOR_CLAUDE"
    CLAUDE_WINDOW_STARTED: Final = "CLAUDE_WINDOW_STARTED"
    WAITING_FOR_CODEX: Final = "WAITING_FOR_CODEX"
    CODEX_WINDOW_STARTED: Final = "CODEX_WINDOW_STARTED"
    NEEDS_FIX: Final = "NEEDS_FIX"
    PASS: Final = "PASS"
    BLOCKED: Final = "BLOCKED"
    FAILED: Final = "FAILED"
    CANCELLED: Final = "CANCELLED"


TERMINAL_STATUSES: Final = {
    Status.PASS,
    Status.BLOCKED,
    Status.FAILED,
    Status.CANCELLED,
}

ALLOWED_TRANSITIONS: Final = {
    Status.CREATED: {Status.WAITING_FOR_CLAUDE},
    Status.WAITING_FOR_CLAUDE: {Status.CLAUDE_WINDOW_STARTED, Status.CANCELLED},
    Status.CLAUDE_WINDOW_STARTED: {Status.WAITING_FOR_CODEX, Status.FAILED, Status.CANCELLED},
    Status.WAITING_FOR_CODEX: {Status.CODEX_WINDOW_STARTED, Status.CANCELLED},
    Status.CODEX_WINDOW_STARTED: {
        Status.WAITING_FOR_CODEX,
        Status.PASS,
        Status.NEEDS_FIX,
        Status.BLOCKED,
        Status.FAILED,
        Status.CANCELLED,
    },
    Status.NEEDS_FIX: {Status.WAITING_FOR_CLAUDE, Status.FAILED, Status.CANCELLED},
    Status.PASS: set(),
    Status.BLOCKED: set(),
    Status.FAILED: set(),
    Status.CANCELLED: set(),
}


class StateTransitionError(ValueError):
    pass


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def transition(current: str, target: str) -> str:
    if current not in ALLOWED_TRANSITIONS:
        raise StateTransitionError(f"Unknown task status: {current}")
    if target not in ALLOWED_TRANSITIONS:
        raise StateTransitionError(f"Unknown task status: {target}")
    if is_terminal(current):
        raise StateTransitionError(f"Terminal task status cannot transition: {current}")
    if target not in ALLOWED_TRANSITIONS[current]:
        raise StateTransitionError(f"Invalid task status transition: {current} -> {target}")
    return target


def cancel(current: str) -> str:
    return transition(current, Status.CANCELLED)


def created_to_waiting() -> str:
    return transition(Status.CREATED, Status.WAITING_FOR_CLAUDE)


def review_status_to_task_status(review_status: str) -> str:
    if review_status not in {Status.PASS, Status.NEEDS_FIX, Status.BLOCKED, Status.FAILED}:
        raise StateTransitionError(f"Invalid review status: {review_status}")
    return review_status
