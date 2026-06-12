"""Local task orchestration for the web collaboration loop."""

from .models import Task
from .state_machine import (
    Status,
    StateTransitionError,
    is_terminal,
    transition,
)
from .store import TaskStore

__all__ = [
    "Status",
    "StateTransitionError",
    "Task",
    "TaskStore",
    "is_terminal",
    "transition",
]
