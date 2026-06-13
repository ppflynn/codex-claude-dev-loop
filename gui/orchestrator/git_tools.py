from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .path_safety import path_has_env_segment

MAX_UNTRACKED_DIFF_BYTES = 200_000
MAX_UNTRACKED_FILE_BYTES = 50_000


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


def _untracked_paths_from_status(status: str) -> list[str]:
    paths: list[str] = []
    for line in status.splitlines():
        if line.startswith("?? "):
            path = line[3:].strip()
            if path:
                paths.append(path)
    return paths


def status_mentions_env(status: str) -> bool:
    return any(path_has_env_segment(path) for path in _changed_paths_from_status(status))


def _is_child_path(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _untracked_file_diff(project_path: Path, relative_path: str) -> tuple[str, str]:
    root = project_path.resolve()
    file_path = (project_path / relative_path).resolve()
    if not _is_child_path(root, file_path) or not file_path.is_file():
        return "", f" {relative_path} | skipped\n"
    try:
        data = file_path.read_bytes()
    except OSError as exc:
        return "", f" {relative_path} | unreadable ({exc})\n"
    if len(data) > MAX_UNTRACKED_FILE_BYTES:
        return "", f" {relative_path} | skipped, file exceeds {MAX_UNTRACKED_FILE_BYTES} bytes\n"
    if b"\0" in data:
        return "", f" {relative_path} | skipped, binary file\n"
    text = data.decode("utf-8", errors="replace")
    escaped_path = relative_path.replace("\\", "/")
    lines = text.splitlines(keepends=True)
    if text and not text.endswith(("\n", "\r")):
        lines[-1] = lines[-1] + "\n"
    body = "".join("+" + line for line in lines)
    diff = (
        f"diff --git a/{escaped_path} b/{escaped_path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{escaped_path}\n"
        "@@\n"
        f"{body}"
    )
    line_count = len(lines)
    stat = f" {escaped_path} | {line_count} {'+' * min(line_count, 80)}\n"
    return diff, stat


def _untracked_files_diff(project_path: Path, status: str) -> tuple[str, str]:
    snippets: list[str] = []
    stats: list[str] = []
    total_bytes = 0
    for relative_path in _untracked_paths_from_status(status):
        diff, stat = _untracked_file_diff(project_path, relative_path)
        stats.append(stat)
        if not diff:
            continue
        encoded_size = len(diff.encode("utf-8", errors="replace"))
        if total_bytes + encoded_size > MAX_UNTRACKED_DIFF_BYTES:
            stats.append(f" {relative_path} | skipped, untracked diff budget exceeded\n")
            continue
        snippets.append(diff)
        total_bytes += encoded_size
    if not snippets and not stats:
        return "", ""
    stat_text = "\n# Untracked files included for review\n" + "".join(stats)
    diff_text = "\n# Untracked files included for review\n" + "\n".join(snippets)
    if diff_text and not diff_text.endswith("\n"):
        diff_text += "\n"
    return diff_text, stat_text


def get_git_common_dir(project_path: Path) -> str | None:
    result = _run_git(project_path, ["rev-parse", "--git-common-dir"])
    if result.returncode != 0:
        return None
    common_dir = result.stdout.strip()
    if not common_dir:
        return None
    if Path(common_dir).is_absolute():
        return str(Path(common_dir).resolve())
    return str((project_path / common_dir).resolve())


def get_current_branch(project_path: Path) -> str | None:
    result = _run_git(project_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def is_git_worktree(project_path: Path) -> bool:
    git_path = project_path / ".git"
    if git_path.is_file():
        return True
    if git_path.is_dir():
        common = get_git_common_dir(project_path)
        local_git = str(git_path.resolve())
        if common and Path(common).resolve() != Path(local_git):
            return True
    return False


def get_main_worktree_path(project_path: Path) -> str | None:
    result = _run_git(project_path, ["worktree", "list", "--porcelain"])
    if result.returncode != 0:
        return None
    main_path: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            candidate = line[len("worktree "):]
            if main_path is None:
                main_path = candidate
            elif candidate != str(project_path):
                pass
        if line.strip() == "bare":
            pass
    if main_path and Path(main_path) != project_path.resolve():
        return main_path
    return None


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

    untracked_diff, untracked_stat = _untracked_files_diff(project_path, status)
    diff_stat = diff_stat_result.stdout + untracked_stat
    diff = diff_result.stdout + untracked_diff
    diff_stat_path.write_text(diff_stat, encoding="utf-8")
    diff_path.write_text(diff, encoding="utf-8")
    return GitArtifacts(status_path, diff_stat_path, diff_path, status, diff_stat, diff)
