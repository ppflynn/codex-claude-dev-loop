from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


FORBIDDEN_SHELL_TOKENS = ("&&", "||", "|", ";", ">", "<", "`", "$(")


class TestCommandError(ValueError):
    pass


@dataclass
class TestRunResult:
    command: list[str] | None
    exit_code: int | None
    output: str
    path: Path | None = None


def parse_command(command: str) -> list[str]:
    command = command.strip()
    if not command:
        raise TestCommandError("Test command is empty.")
    if any(token in command for token in FORBIDDEN_SHELL_TOKENS):
        raise TestCommandError("Test command contains forbidden shell control syntax.")
    argv = [_strip_outer_quotes(item) for item in shlex.split(command, posix=False)]
    if not argv:
        raise TestCommandError("Test command is empty.")
    return argv


def _strip_outer_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def infer_test_command(project_path: Path) -> list[str] | None:
    package_json = project_path / "package.json"
    if package_json.is_file():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        scripts = data.get("scripts") if isinstance(data, dict) else None
        if isinstance(scripts, dict) and scripts.get("test"):
            return ["npm", "test"]

    ignored_dirs = {".git", ".gui", "node_modules", "__pycache__", ".pytest_cache"}
    for path in project_path.rglob("*.py"):
        if any(part in ignored_dirs for part in path.relative_to(project_path).parts):
            continue
        name = path.name
        if name.startswith("test_") or name.endswith("_test.py"):
            return ["python", "-m", "pytest"]
    return None


def run_tests(project_path: Path, task_dir: Path, round_number: int, command: str = "") -> TestRunResult:
    result_path = task_dir / f"test_results_round_{round_number}.txt"
    task_dir.mkdir(parents=True, exist_ok=True)
    try:
        argv = parse_command(command) if command.strip() else infer_test_command(project_path)
    except TestCommandError as exc:
        output = f"TEST_COMMAND_REJECTED: {exc}\n"
        result_path.write_text(output, encoding="utf-8")
        return TestRunResult(None, None, output, result_path)

    if not argv:
        output = "NO_TEST_COMMAND\n"
        result_path.write_text(output, encoding="utf-8")
        return TestRunResult(None, None, output, result_path)

    try:
        completed = subprocess.run(
            argv,
            cwd=str(project_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=300,
        )
        output = "\n".join(
            [
                f"COMMAND: {subprocess.list2cmdline(argv)}",
                f"EXIT_CODE: {completed.returncode}",
                "",
                "STDOUT:",
                completed.stdout,
                "",
                "STDERR:",
                completed.stderr,
            ]
        )
        result_path.write_text(output, encoding="utf-8")
        return TestRunResult(argv, completed.returncode, output, result_path)
    except FileNotFoundError as exc:
        output = f"COMMAND_NOT_FOUND: {argv[0]}\n{exc}\n"
        result_path.write_text(output, encoding="utf-8")
        return TestRunResult(argv, 127, output, result_path)
    except subprocess.TimeoutExpired as exc:
        output = f"TEST_TIMEOUT: {subprocess.list2cmdline(argv)}\n{exc}\n"
        result_path.write_text(output, encoding="utf-8")
        return TestRunResult(argv, 124, output, result_path)
