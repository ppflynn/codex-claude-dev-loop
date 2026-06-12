from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .path_safety import path_has_env_segment


class GitError(RuntimeError):
    pass


class DirtyWorkTreeError(GitError):
    pass


class EnvFileChangedError(GitError):
    pass


@dataclass
class GitArtifacts:
    status_path: Path
    diff_stat_path: Path
    diff_path: Path
    status: str
    diff_stat: str
    diff: str


def _run_git(project_path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(project_path), *args]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def assert_git_work_tree(project_path: Path) -> None:
    result = _run_git(project_path, ["rev-parse", "--is-inside-work-tree"])
    if result.returncode != 0 or result.stdout.strip().lower() != "true":
        raise GitError("Project path is not inside a Git work tree.")


def git_status(project_path: Path) -> str:
    result = _run_git(project_path, ["status", "--short", "--untracked-files=all"])
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or "git status failed.")
    return result.stdout


def assert_clean_work_tree(project_path: Path) -> None:
    status = git_status(project_path)
    if status.strip():
        raise DirtyWorkTreeError("Project work tree is dirty; clean or commit/stash manually before creating a task.")


def _changed_paths_from_status(status: str) -> list[str]:
    paths: list[str] = []
    for line in status.splitlines():
        if not line.strip():
            continue
        payload = line[3:].strip() if len(line) > 3 else line.strip()
        if " -> " in payload:
            paths.extend(part.strip() for part in payload.split(" -> ", 1))
        else:
            paths.append(payload)
    return paths


def status_mentions_env(status: str) -> bool:
    return any(path_has_env_segment(path) for path in _changed_paths_from_status(status))


def collect_git_artifacts(project_path: Path, task_dir: Path, round_number: int) -> GitArtifacts:
    assert_git_work_tree(project_path)
    status = git_status(project_path)
    status_path = task_dir / f"git_status_round_{round_number}.txt"
    diff_stat_path = task_dir / f"git_diff_stat_round_{round_number}.txt"
    diff_path = task_dir / f"git_diff_round_{round_number}.diff"
    task_dir.mkdir(parents=True, exist_ok=True)
    status_path.write_text(status, encoding="utf-8")

    if status_mentions_env(status):
        redacted = "ENV_FILE_CHANGED: .env diff content omitted.\n"
        diff_stat_path.write_text(redacted, encoding="utf-8")
        diff_path.write_text(redacted, encoding="utf-8")
        raise EnvFileChangedError("A .env file is changed; diff collection was blocked.")

    diff_stat_result = _run_git(project_path, ["diff", "--stat"])
    if diff_stat_result.returncode != 0:
        raise GitError(diff_stat_result.stderr.strip() or "git diff --stat failed.")
    diff_result = _run_git(project_path, ["diff"])
    if diff_result.returncode != 0:
        raise GitError(diff_result.stderr.strip() or "git diff failed.")

    diff_stat = diff_stat_result.stdout
    diff = diff_result.stdout
    diff_stat_path.write_text(diff_stat, encoding="utf-8")
    diff_path.write_text(diff, encoding="utf-8")
    return GitArtifacts(status_path, diff_stat_path, diff_path, status, diff_stat, diff)
