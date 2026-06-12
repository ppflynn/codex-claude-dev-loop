from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from .cli_window import launch_cli_window
from .models import Task


class ClaudeAdapter(Protocol):
    def launch(self, task: Task, task_dir: Path, prompt_path: Path) -> dict[str, Any]:
        ...


class CodexAdapter(Protocol):
    def launch(self, task: Task, task_dir: Path, prompt_path: Path, output_path: Path) -> dict[str, Any]:
        ...


class ClaudeCliWindowAdapter:
    """Generate prompt and launch visible Claude CLI window."""

    def __init__(self, command: list[str]):
        self.command = command

    def launch(self, task: Task, task_dir: Path, prompt_path: Path) -> dict[str, Any]:
        return launch_cli_window(
            task=task,
            task_dir=task_dir,
            kind="claude",
            command=self.command,
            prompt_path=prompt_path,
        )


class CodexCliWindowAdapter:
    """Generate review prompt and launch visible Codex CLI window."""

    def __init__(self, command: list[str]):
        self.command = command

    def launch(self, task: Task, task_dir: Path, prompt_path: Path, output_path: Path) -> dict[str, Any]:
        return launch_cli_window(
            task=task,
            task_dir=task_dir,
            kind="codex",
            command=self.command,
            prompt_path=prompt_path,
            output_path=output_path,
        )


class ClaudeHeadlessCliAdapter:
    def launch(self, task: Task, task_dir: Path, prompt_path: Path) -> dict[str, Any]:
        raise NotImplementedError("Headless Claude execution is intentionally not implemented in phase 1.")


class CodexHeadlessCliAdapter:
    def launch(self, task: Task, task_dir: Path, prompt_path: Path, output_path: Path) -> dict[str, Any]:
        raise NotImplementedError("Headless Codex execution is intentionally not implemented in phase 1.")
