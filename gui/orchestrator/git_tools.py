from __future__ import annotations

import hashlib
import os
import stat
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
    raw_path = project_path / relative_path
    # ``lstat`` first so symlinks are detected without being followed.
    # A benign-named untracked symlink whose stored target points at a
    # forbidden ``.env`` must not have its destination's bytes rendered
    # into the review diff (Codex P1-1 round 9).  ``has_env_changes``
    # runs before this helper and raises first when the symlink target
    # has an env segment, so reaching this branch means the target is
    # benign — but Git still stages the *link itself*, not the
    # destination's content, so we render the target string the same
    # way (mode 120000) instead of reading through the link.
    try:
        link_stat = raw_path.lstat()
    except OSError:
        return "", f" {relative_path} | skipped\n"
    escaped_path = relative_path.replace("\\", "/")
    if stat.S_ISLNK(link_stat.st_mode):
        try:
            target = os.readlink(raw_path)
        except OSError as exc:
            return "", f" {relative_path} | unreadable ({exc})\n"
        diff = (
            f"diff --git a/{escaped_path} b/{escaped_path}\n"
            "new file mode 120000\n"
            "--- /dev/null\n"
            f"+++ b/{escaped_path}\n"
            "@@\n"
            f"+{target}\n"
        )
        return diff, f" {escaped_path} | 1 +\n"
    file_path = raw_path.resolve()
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
    stat_line = f" {escaped_path} | {line_count} {'+' * min(line_count, 80)}\n"
    return diff, stat_line


def _list_untracked_paths(project_path: Path) -> list[str]:
    """Return untracked paths via NUL-terminated ``git ls-files``.

    Uses ``-z`` so paths containing non-ASCII or otherwise quoted
    components are emitted verbatim (no C-quoting).  Every safety-sensitive
    path enumeration — the ``.env`` guard, the untracked review diff, and
    the drift snapshot's per-file hash — goes through this helper so a
    ``dir-é/.env`` cannot slip past any of them.  Pure read-only.

    Fails closed (Codex P1-1 round 13): any failure from
    ``git ls-files --others`` raises ``GitError`` so the ``.env`` guard,
    the snapshot hash, and the artifact collection cannot silently drop
    protected or unreviewed paths while later artifact / commit commands
    succeed.
    """
    result = _run_git(
        project_path,
        ["ls-files", "--others", "--exclude-standard", "-z"],
    )
    if result.returncode != 0:
        raise GitError(
            (result.stderr or result.stdout or "").strip()
            or "git ls-files --others failed."
        )
    return [path for path in result.stdout.split("\0") if path]


def _untracked_files_diff(project_path: Path) -> tuple[str, str]:
    snippets: list[str] = []
    stats: list[str] = []
    total_bytes = 0
    for relative_path in _list_untracked_paths(project_path):
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


@dataclass
class WorktreeInfo:
    path: str
    branch: str | None = None
    head: str | None = None
    type: str = "worktree"  # "primary", "worktree", or "bare"
    bare: bool = False
    detached: bool = False


def list_worktrees(project_path: Path) -> list[WorktreeInfo]:
    """List all worktrees in the same Git repository as ``project_path``.

    The first non-bare worktree reported by Git is treated as the primary
    worktree (matching ``git worktree list`` ordering). Bare worktrees are
    skipped from the result so the frontend can focus on usable checkouts.
    """
    result = _run_git(project_path, ["worktree", "list", "--porcelain"])
    if result.returncode != 0:
        return []
    entries: list[WorktreeInfo] = []
    current: WorktreeInfo | None = None
    for raw_line in result.stdout.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            current = None
            continue
        if line.startswith("worktree "):
            current = WorktreeInfo(path=line[len("worktree "):].strip())
            entries.append(current)
            continue
        if current is None:
            continue
        if line.startswith("HEAD "):
            current.head = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            current.branch = ref.split("refs/heads/", 1)[-1] if ref.startswith("refs/heads/") else ref
        elif line.strip() == "bare":
            current.bare = True
        elif line.strip() == "detached":
            current.detached = True
    primary_seen = False
    cleaned: list[WorktreeInfo] = []
    for entry in entries:
        if entry.bare:
            continue
        if not primary_seen:
            entry.type = "primary"
            primary_seen = True
        else:
            entry.type = "worktree"
        cleaned.append(entry)
    return cleaned


def compute_repo_id(common_dir: str | None) -> str | None:
    """Stable identifier for the repository that owns a worktree.

    Derived from ``git rev-parse --git-common-dir`` so that every worktree of
    the same repository resolves to the same ``repoId`` regardless of which
    worktree path the user imported.
    """
    if not common_dir:
        return None
    normalized = str(common_dir).strip().lower().replace("\\", "/").rstrip("/")
    if not normalized:
        return None
    return "repo_" + hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]


def collect_git_artifacts(project_path: Path, task_dir: Path, round_number: int) -> GitArtifacts:
    assert_git_work_tree(project_path)
    status = git_status(project_path)
    status_path = task_dir / f"git_status_round_{round_number}.txt"
    diff_stat_path = task_dir / f"git_diff_stat_round_{round_number}.txt"
    diff_path = task_dir / f"git_diff_round_{round_number}.diff"
    task_dir.mkdir(parents=True, exist_ok=True)
    status_path.write_text(status, encoding="utf-8")

    if has_env_changes(project_path):
        redacted = "ENV_FILE_CHANGED: .env diff content omitted.\n"
        diff_stat_path.write_text(redacted, encoding="utf-8")
        diff_path.write_text(redacted, encoding="utf-8")
        raise EnvFileChangedError("A .env file is changed; diff collection was blocked.")

    # ``git diff HEAD`` captures both staged and unstaged tracked changes so
    # Codex review sees exactly what a subsequent ``git add -A && git commit``
    # would land.  Untracked files are folded in via ``_untracked_files_diff``.
    diff_stat_result = _run_git(project_path, ["diff", "HEAD", "--stat"])
    if diff_stat_result.returncode != 0:
        raise GitError(diff_stat_result.stderr.strip() or "git diff --stat failed.")
    diff_result = _run_git(project_path, ["diff", "HEAD"])
    if diff_result.returncode != 0:
        raise GitError(diff_result.stderr.strip() or "git diff failed.")

    untracked_diff, untracked_stat = _untracked_files_diff(project_path)
    diff_stat = diff_stat_result.stdout + untracked_stat
    diff = diff_result.stdout + untracked_diff
    diff_stat_path.write_text(diff_stat, encoding="utf-8")
    diff_path.write_text(diff, encoding="utf-8")
    return GitArtifacts(status_path, diff_stat_path, diff_path, status, diff_stat, diff)


def _parse_diff_name_status_z(output: str) -> list[str]:
    """Parse NUL-terminated ``git diff --name-status -z`` output.

    Each record is ``STATUS\\0PATH1[\\0PATH2]\\0`` where ``STATUS`` is the
    change-type letter (optionally followed by a similarity score for
    renames/copies, e.g. ``R100``).  ``R`` and ``C`` records carry two
    paths (source and destination); all others carry one.  Returns a flat
    list of every path field so the caller can run safety checks against
    each one.
    """
    if not output:
        return []
    parts = output.split("\0")
    # Trailing NUL produces an empty final element.
    while parts and parts[-1] == "":
        parts.pop()
    paths: list[str] = []
    i = 0
    n = len(parts)
    while i < n:
        status = parts[i]
        i += 1
        if not status:
            continue
        if i < n:
            path1 = parts[i]
            i += 1
            if path1:
                paths.append(path1)
        if status[0] in ("R", "C") and i < n:
            path2 = parts[i]
            i += 1
            if path2:
                paths.append(path2)
    return paths


def enumerate_changed_paths(project_path: Path) -> list[str]:
    """Enumerate every file path that would be staged by ``git add -A``.

    Uses ``git diff HEAD --name-status -z`` for tracked changes (capturing
    both staged and unstaged modifications in one call) and
    ``git ls-files --others --exclude-standard -z`` for untracked files.
    The ``-z`` flag disables Git's default C-quoting (``core.quotePath``
    and friends) so paths containing non-ASCII or otherwise quoted
    components are emitted verbatim.  Without ``-z``, a changed
    ``.env`` under a quoted parent directory (e.g. ``secrets-é/.env``)
    would arrive wrapped in double quotes with octal escapes, would no
    longer match the ``.env`` segment check, and could slip past the
    safety guard into the reviewed commit.  ``-z`` also reports untracked
    files nested inside untracked directories individually, unlike
    ``git status --short`` which on some Git versions collapses them to a
    single ``?? dir/`` entry.

    Pure read-only — never runs any mutating Git command.

    Fails closed (Codex P1-1 round 13): any failure from either
    ``git diff HEAD --name-status`` or ``git ls-files --others`` raises
    ``GitError`` so the ``.env`` guard and the snapshot hash cannot
    silently drop protected or unreviewed paths while later artifact /
    commit commands succeed.
    """
    paths: set[str] = set()

    tracked_diff = _run_git(
        project_path,
        ["diff", "HEAD", "--name-status", "-z"],
    )
    if tracked_diff.returncode != 0:
        raise GitError(
            (tracked_diff.stderr or tracked_diff.stdout or "").strip()
            or "git diff HEAD --name-status failed."
        )
    for path in _parse_diff_name_status_z(tracked_diff.stdout):
        if path:
            paths.add(path)

    others = _run_git(
        project_path,
        ["ls-files", "--others", "--exclude-standard", "-z"],
    )
    if others.returncode != 0:
        raise GitError(
            (others.stderr or others.stdout or "").strip()
            or "git ls-files --others failed."
        )
    for path in others.stdout.split("\0"):
        if path:
            paths.add(path)

    return sorted(paths)


def _path_or_symlink_env_violation(project_path: Path, relative_path: str) -> str | None:
    """Return a description when ``relative_path`` or its symlink target references ``.env``.

    A symlink with a benign name can point at a tracked ``.env`` file.  The
    Git-reported path (``link.txt``) passes the ``.env``-segment check, but
    when ``git add -A`` stages the symlink Git stores the *target string*
    (``.env``) as the blob content, and the previous hasher / diff renderer
    followed the link and silently read the secret bytes (Codex P1-1 round 9).

    This helper inspects the link itself via ``lstat`` / ``os.readlink``
    (without following it) and reports a violation when either:

    * ``relative_path`` has an ``.env`` segment, or
    * the link's *immediate* target string has an ``.env`` segment, or
    * the fully resolved target path has an ``.env`` segment (covers
      symlink chains whose final hop is ``.env``).

    Returns ``None`` when there is no violation.  Otherwise returns a
    short description: the bare path for direct violations, or
    ``"<path> -> <target>"`` when the violation is via a symlink target.
    Read-only — never follows symlinks into byte reads.
    """
    if path_has_env_segment(relative_path):
        return relative_path
    raw_path = project_path / relative_path
    try:
        if not raw_path.is_symlink():
            return None
    except OSError:
        return None
    try:
        immediate_target = os.readlink(raw_path)
    except OSError:
        immediate_target = None
    if immediate_target and path_has_env_segment(immediate_target):
        return f"{relative_path} -> {immediate_target}"
    try:
        resolved = raw_path.resolve(strict=False)
        resolved_str = str(resolved)
        if path_has_env_segment(resolved_str):
            return f"{relative_path} -> {resolved_str}"
    except OSError:
        pass
    return None


def enumerate_env_violations(project_path: Path) -> list[str]:
    """Return every path whose name or symlink target references ``.env``.

    Used by ``has_env_changes``, ``compute_review_snapshot``, and
    ``controlled_commit`` so the ``.env`` guard cannot be bypassed via a
    benign-named symlink pointing at a tracked ``.env`` file.  Each
    returned string is suitable for an error message: the bare path for
    direct violations, or ``"<path> -> <target>"`` for symlink-target
    violations.
    """
    violations: list[str] = []
    for relative_path in enumerate_changed_paths(project_path):
        violation = _path_or_symlink_env_violation(project_path, relative_path)
        if violation:
            violations.append(violation)
    return violations


def has_env_changes(project_path: Path) -> bool:
    """Return True when any path that ``git add -A`` would stage references ``.env``.

    Defensive replacement for ``status_mentions_env`` that does not rely on
    ``git status --short`` directory collapsing: individual untracked files
    nested inside untracked directories are inspected via
    ``git ls-files --others``.  Symlink-aware: a benign-named link whose
    stored target string (immediate or resolved) references ``.env`` is
    also reported so the backend never reads, hashes, or diffs the
    secret bytes through the link.  Used by both the Codex artifact
    collection and the controlled commit.
    """
    return bool(enumerate_env_violations(project_path))


def find_clean_filtered_paths(project_path: Path) -> list[str]:
    """Return every changed path that has an active clean/process Git filter.

    A Git filter (``filter.<name>.clean`` or ``filter.<name>.process``)
    configured on a path transforms that path's content during
    ``git add``.  The Codex review artifacts are built from raw worktree
    bytes (``git diff HEAD`` + ``_untracked_file_diff`` reading file bytes
    directly from disk), so they show the *unfiltered* content.  A
    deterministic clean filter can therefore make a malicious or buggy
    transformation that Codex never saw, while the final committed tree
    carries the transformed content (Codex P1-2 round 14).

    Detection strategy:

    1. ``git check-attr -z filter -- <changed paths>`` reveals which paths
       have a non-``unspecified`` ``filter`` attribute (typically assigned
       via ``.gitattributes``).  The ``-z`` flag disables Git's default
       C-quoting so paths containing non-ASCII or otherwise quoted
       components are emitted verbatim; without it a changed ``dir-é/.env``
       path arrives wrapped in double quotes with octal escapes and no
       longer matches the file on disk, so a clean filter configured on
       that path would silently escape detection (Codex P1-1 round 15).
    2. For each distinct filter name, ``git config --get filter.<name>.clean``
       and ``git config --get filter.<name>.process`` determine whether
       the filter actually has an active driver.  An attribute without a
       matching config has no effect (Git treats it as identity) and is
       not reported.
    3. Only paths whose filter attribute maps to an active driver are
       returned, so users see actionable paths rather than every file
       inside an attribute pattern.

    Pure read-only — never runs any mutating Git command.  Fails closed
    on any Git failure so the safety boundary does not silently degrade.
    """
    changed_paths = enumerate_changed_paths(project_path)
    if not changed_paths:
        return []

    # ``git check-attr -z`` accepts paths after ``--`` and emits one
    # ``<path>\0<attribute>\0<info>\0`` record per input path.  Paths are
    # passed as argv (no shell) so paths containing spaces / special
    # characters survive verbatim, and ``-z`` ensures paths containing
    # non-ASCII characters are emitted as their raw UTF-8 bytes rather
    # than C-quoted octal escapes.  ``_run_git`` decodes stdout using
    # ``encoding="utf-8"`` so the NUL-separated output is directly
    # parseable as a Python string.
    check_result = _run_git(
        project_path,
        ["check-attr", "-z", "filter", "--", *changed_paths],
    )
    if check_result.returncode != 0:
        raise GitError(
            (check_result.stderr or check_result.stdout or "").strip()
            or "git check-attr filter failed."
        )

    # Parse the NUL-separated triples.  Each record is exactly three
    # fields: ``<path>, <attribute>, <info>``, terminated by a NUL.
    # The trailing NUL after the final record produces a trailing
    # empty token that ``split("\0")`` turns into an empty final
    # element; that element is discarded by the ``i + 3`` loop bound.
    path_to_filter: dict[str, str | None] = {}
    tokens = check_result.stdout.split("\0")
    i = 0
    n = len(tokens)
    while i + 2 < n:
        path = tokens[i]
        attr = tokens[i + 1]
        info = tokens[i + 2]
        i += 3
        if not path or attr != "filter":
            # ``check-attr`` only emits records for the attributes we
            # asked about (``filter``); a mismatched attribute name
            # would indicate a Git version skew we do not expect, but
            # be defensive rather than misattributing a value to the
            # wrong path.
            continue
        if info in ("unspecified", "unset", ""):
            path_to_filter[path] = None
        else:
            path_to_filter[path] = info

    # Collect distinct filter names referenced by changed paths.
    filter_names: set[str] = {
        name for name in path_to_filter.values() if name
    }
    if not filter_names:
        return []

    # Keep only filters that actually have an active driver configured.
    # A filter attribute without a matching ``clean`` or ``process`` driver
    # is a no-op (Git applies identity) and is therefore not a security
    # concern.
    #
    # Codex P1-5 round 17: ``git config --get <key>`` returns exit code 1
    # when the key is absent and any other non-zero code on a real error
    # (e.g. corrupt config file, malformed key, permission denied).  The
    # previous implementation treated *every* non-zero return as "not
    # configured", so a corrupt config silently degraded the safety
    # boundary: the filter would be reported as inactive even when the
    # config could not be read, and the controlled commit would proceed
    # without verifying the active-filter set.  Only ``returncode == 1``
    # is "absent"; any other non-zero raises ``GitError``.
    active_filters: set[str] = set()
    for name in filter_names:
        clean_result = _run_git(
            project_path,
            ["config", "--get", f"filter.{name}.clean"],
        )
        if clean_result.returncode == 0:
            if clean_result.stdout.strip():
                active_filters.add(name)
                continue
        elif clean_result.returncode == 1:
            pass  # key absent — fall through to process probe below
        else:
            raise GitError(
                (clean_result.stderr or clean_result.stdout or "").strip()
                or f"git config --get filter.{name}.clean failed unexpectedly."
            )
        process_result = _run_git(
            project_path,
            ["config", "--get", f"filter.{name}.process"],
        )
        if process_result.returncode == 0:
            if process_result.stdout.strip():
                active_filters.add(name)
        elif process_result.returncode == 1:
            pass  # key absent — filter is inactive
        else:
            raise GitError(
                (process_result.stderr or process_result.stdout or "").strip()
                or f"git config --get filter.{name}.process failed unexpectedly."
            )

    if not active_filters:
        return []

    # Re-derive the violating path set so the returned list contains
    # every path whose filter attribute maps to an active driver.  We
    # use the changed-paths list (not the ``check-attr`` output) so the
    # ordering is deterministic and matches what ``enumerate_changed_paths``
    # emits.
    path_filter_lookup = {p: f for p, f in path_to_filter.items() if f}
    violating: list[str] = []
    for changed_path in changed_paths:
        filter_name = path_filter_lookup.get(changed_path)
        if filter_name and filter_name in active_filters:
            violating.append(changed_path)
    return violating


def find_custom_merge_driver_paths(
    project_path: Path,
    candidate_paths: list[str],
) -> list[str]:
    """Return every candidate path that has an active custom merge driver.

    A Git merge driver (``merge.<name>.driver``) configured on a path
    overrides Git's default 3-way merge behaviour.  A driver that runs
    ``true`` or ``exit 0`` would auto-succeed regardless of conflicts,
    letting an attacker stage unreviewed content into the trunk via the
    controlled merge button even when a manual merge would have
    conflicted (Codex P1-2 round 15).

    Codex P1-2 round 16: Git also ships **built-in** merge drivers that
    need no ``merge.<name>.driver`` config entry — ``union`` (concatenate
    both sides) and ``ours`` (drop the other side entirely).  Either can
    silently auto-resolve what should have been a conflict and let
    unreviewed content reach the trunk.  The previous implementation only
    inspected configured drivers, so ``merge=union`` set via
    ``.gitattributes`` was treated as "no driver" and the merge proceeded.
    Now ``union`` and ``ours`` are treated as unsafe by name, so a
    ``.gitattributes`` line referencing them blocks the controlled merge
    without relying on a config probe.  Other attribute names without a
    matching ``merge.<name>.driver`` config remain no-ops (Git falls back
    to its default 3-way merge), so a typo or stale attribute does not
    block legitimate merges.

    Detection strategy mirrors ``find_clean_filtered_paths``:

    1. ``git check-attr -z merge -- <candidate paths>`` reveals which
       paths have a non-``unspecified`` ``merge`` attribute (typically
       assigned via ``.gitattributes``).  The ``-z`` flag preserves
       non-ASCII paths verbatim.
    2. For each distinct merge-driver name, the driver is treated as
       unsafe when it is one of Git's built-in auto-resolving drivers
       (``union``, ``ours``) **or** has an active ``merge.<name>.driver``
       command.  An attribute name that is neither built-in nor
       configured is a no-op (Git falls back to its default merge) and
       is not reported.
    3. Only candidate paths whose merge attribute maps to an unsafe
       driver are returned.

    Pure read-only — never runs any mutating Git command.  Fails closed
    on any Git failure so the safety boundary does not silently degrade.
    """
    if not candidate_paths:
        return []

    check_result = _run_git(
        project_path,
        ["check-attr", "-z", "merge", "--", *candidate_paths],
    )
    if check_result.returncode != 0:
        raise GitError(
            (check_result.stderr or check_result.stdout or "").strip()
            or "git check-attr merge failed."
        )

    # Parse the NUL-separated triples (same shape as
    # ``find_clean_filtered_paths``): ``<path>\0<attribute>\0<info>\0``.
    path_to_driver: dict[str, str | None] = {}
    tokens = check_result.stdout.split("\0")
    i = 0
    n = len(tokens)
    while i + 2 < n:
        path = tokens[i]
        attr = tokens[i + 1]
        info = tokens[i + 2]
        i += 3
        if not path or attr != "merge":
            continue
        if info in ("unspecified", "unset", ""):
            path_to_driver[path] = None
        else:
            path_to_driver[path] = info

    driver_names: set[str] = {
        name for name in path_to_driver.values() if name
    }
    if not driver_names:
        return []

    # Codex P1-2 round 16: Git ships built-in merge drivers that need no
    # ``merge.<name>.driver`` config entry.  ``union`` concatenates both
    # sides and ``ours`` drops the incoming side entirely — both can
    # silently auto-resolve what should have been a conflict and let
    # unreviewed content reach the trunk.  Treat them as unsafe by name
    # so a ``.gitattributes`` line like ``*.txt merge=union`` blocks the
    # controlled merge without relying on a config probe.
    unsafe_builtin_drivers = {"union", "ours"}

    active_drivers: set[str] = set()
    for name in driver_names:
        if name in unsafe_builtin_drivers:
            active_drivers.add(name)
            continue
        driver_result = _run_git(
            project_path,
            ["config", "--get", f"merge.{name}.driver"],
        )
        # Codex P1-5 round 17: only ``returncode == 1`` means "absent".
        # Any other non-zero indicates a real config error (corrupt
        # file, malformed key, permission denied) and must surface as a
        # ``GitError`` so the merge safety boundary does not silently
        # degrade.  See ``find_clean_filtered_paths`` for the same
        # invariant.
        if driver_result.returncode == 0:
            if driver_result.stdout.strip():
                active_drivers.add(name)
        elif driver_result.returncode == 1:
            pass  # key absent — driver is inactive
        else:
            raise GitError(
                (driver_result.stderr or driver_result.stdout or "").strip()
                or f"git config --get merge.{name}.driver failed unexpectedly."
            )

    if not active_drivers:
        return []

    path_driver_lookup = {p: d for p, d in path_to_driver.items() if d}
    violating: list[str] = []
    for candidate_path in candidate_paths:
        driver_name = path_driver_lookup.get(candidate_path)
        if driver_name and driver_name in active_drivers:
            violating.append(candidate_path)
    return violating


def list_tracked_paths(project_path: Path) -> list[str]:
    """Return every path tracked in ``HEAD`` via NUL-terminated ``git ls-tree``.

    Used by the worktree-creation flow (Codex P1-2 round 18) to
    enumerate every path ``git worktree add`` will check out into the
    new worktree, so the smudge-filter guard can reject a worktree
    creation that would otherwise let a configured smudge / process
    filter transform content during the initial checkout.

    ``-z`` disables Git's default C-quoting so paths containing
    non-ASCII or otherwise quoted components are emitted verbatim;
    without it a tracked ``dir-é/feature.py`` path would arrive wrapped
    in double quotes with octal escapes, would no longer match the
    file on disk, and the smudge filter configured on it via
    ``.gitattributes`` would silently escape detection.

    Pure read-only.  Fails closed on any Git failure (Codex P1-1
    round 13) so the smudge-filter guard cannot silently degrade.
    """
    result = _run_git(
        project_path,
        ["ls-tree", "-r", "--name-only", "-z", "HEAD"],
    )
    if result.returncode != 0:
        raise GitError(
            (result.stderr or result.stdout or "").strip()
            or "git ls-tree -r --name-only HEAD failed."
        )
    return [path for path in result.stdout.split("\0") if path]


def _find_filter_active_paths(
    project_path: Path,
    candidate_paths: list[str] | None,
    side_keys: tuple[str, ...],
) -> list[str]:
    """Shared implementation for ``find_clean_filtered_paths`` and
    ``find_smudge_filtered_paths``.

    ``side_keys`` selects which ``filter.<name>.<key>`` config entries
    count as "active".  For the commit side use the ``clean`` and
    ``process`` keys; for the checkout side use the ``smudge`` and
    ``process`` keys.  The ``process`` filter is bidirectional, so it
    appears in both tuples.

    Mirrors the fail-closed handling of ``find_custom_merge_driver_paths``:
    ``returncode == 1`` means the key is absent; any other non-zero
    raises ``GitError`` so a corrupt config file cannot silently
    degrade the safety boundary (Codex P1-5 round 17).
    """
    if candidate_paths is None:
        candidate_paths = enumerate_changed_paths(project_path)
    if not candidate_paths:
        return []

    check_result = _run_git(
        project_path,
        ["check-attr", "-z", "filter", "--", *candidate_paths],
    )
    if check_result.returncode != 0:
        raise GitError(
            (check_result.stderr or check_result.stdout or "").strip()
            or "git check-attr filter failed."
        )

    path_to_filter: dict[str, str | None] = {}
    tokens = check_result.stdout.split("\0")
    i = 0
    n = len(tokens)
    while i + 2 < n:
        path = tokens[i]
        attr = tokens[i + 1]
        info = tokens[i + 2]
        i += 3
        if not path or attr != "filter":
            continue
        if info in ("unspecified", "unset", ""):
            path_to_filter[path] = None
        else:
            path_to_filter[path] = info

    filter_names: set[str] = {
        name for name in path_to_filter.values() if name
    }
    if not filter_names:
        return []

    active_filters: set[str] = set()
    for name in filter_names:
        for side_key in side_keys:
            probe_result = _run_git(
                project_path,
                ["config", "--get", f"filter.{name}.{side_key}"],
            )
            if probe_result.returncode == 0:
                if probe_result.stdout.strip():
                    active_filters.add(name)
                    break
            elif probe_result.returncode == 1:
                # Key absent — fall through to the next side_key or
                # the next filter name.
                continue
            else:
                raise GitError(
                    (probe_result.stderr or probe_result.stdout or "").strip()
                    or f"git config --get filter.{name}.{side_key} failed unexpectedly."
                )

    if not active_filters:
        return []

    path_filter_lookup = {p: f for p, f in path_to_filter.items() if f}
    violating: list[str] = []
    for candidate_path in candidate_paths:
        filter_name = path_filter_lookup.get(candidate_path)
        if filter_name and filter_name in active_filters:
            violating.append(candidate_path)
    return violating


def find_smudge_filtered_paths(
    project_path: Path,
    candidate_paths: list[str] | None = None,
) -> list[str]:
    """Return every changed path that has an active smudge/process Git filter.

    A Git filter (``filter.<name>.smudge`` or
    ``filter.<name>.process``) configured on a path transforms that
    path's content during checkout (``git worktree add``,
    ``git read-tree -u``).  The Codex review artifacts are built from
    raw worktree bytes, so they show the *pre-smudge* content.  A
    deterministic smudge filter can therefore make a malicious or
    buggy transformation that Codex never saw, while the materialised
    worktree carries the transformed content (Codex P1-2 round 18).

    Detection mirrors ``find_clean_filtered_paths`` but probes
    ``filter.<name>.smudge`` and ``filter.<name>.process`` instead of
    ``.clean``.  A clean-only filter is NOT reported here — clean
    filters fire on the commit side (``git add``) and are detected by
    ``find_clean_filtered_paths``.  ``process`` filters are
    bidirectional so they are reported by both helpers.

    When ``candidate_paths`` is supplied (e.g. the merge-affected
    paths in ``controlled_merge_to_main``), only those paths are
    probed.  Otherwise every changed path is probed (used by
    ``compute_review_snapshot`` callers).  For the worktree-creation
    flow, pass ``list_tracked_paths(project_path)`` so every checked-
    out path is covered.

    Pure read-only.  Fails closed on any Git failure.
    """
    return _find_filter_active_paths(project_path, candidate_paths, ("smudge", "process"))


def _hash_changed_paths_bytes(project_path: Path) -> str:
    """Hash ``(path, lstat-mode, size, sha1(content))`` of every file ``git add -A`` would stage.

    Generalizes the earlier untracked-only byte hasher to *every* path
    that ``git add -A`` would commit — tracked modifications, staged
    content, renames (both source and destination), deletes, and
    untracked files.  The hash is **stable across staging operations**:
    it reads file contents from disk rather than consulting the index,
    so it produces the same value whether the worktree's changes are
    staged or not.  This stability is what lets ``controlled_commit``
    close the check-to-stage TOCTOU gap (Codex P1-2 round 8): the same
    helper is invoked before ``git add -A`` and again after staging, and
    any file mutation in between is detected as drift.

    The hash also covers the per-path ``lstat`` mode and (for symlinks)
    the link target string, so a post-review ``chmod +x`` or a silent
    symlink-target swap is detected as drift even when ``git status
    --short`` still reports the same XY column and the destination's
    content bytes are unchanged (Codex P2-1 round 9).

    **Gitlink / submodule aware** (Codex P1-2 round 11): when a path is
    tracked in the index as a gitlink (Git mode ``160000``), the hasher
    emits the submodule's working-tree HEAD SHA alongside the path.
    Without this, a submodule pointer change (reviewed commit B →
    unreviewed commit C) would leave the directory's lstat mode, the
    ``git status --short`` line, and the staged set unchanged while
    flipping what ``git add -A`` records as the submodule commit — the
    drift check would pass and an unreviewed submodule pointer would
    land inside the "approved" commit.  ``get_tracked_path_modes`` is
    consulted once per call so the lookup is cheap; the submodule HEAD
    is resolved via ``git -C <submodule> rev-parse HEAD`` so the actual
    commit being staged is what perturbs the hash.

    Symlinks are never followed.  A benign-named symlink whose target
    points at a tracked ``.env`` would otherwise have its destination's
    secret bytes hashed through the link; instead we hash the link
    target string exactly as Git stages it (mode 120000).  The
    path-safety guard in ``compute_review_snapshot`` (and
    ``controlled_commit``) raises ``EnvFileChangedError`` before this
    helper is reached when any symlink target references ``.env``, so
    the hasher itself only ever sees benign-target symlinks (Codex
    P1-1 round 9).

    Paths are enumerated via ``enumerate_changed_paths`` (which uses
    NUL-terminated ``git diff HEAD --name-status -z`` and
    ``git ls-files --others -z``) so paths containing non-ASCII or
    otherwise quoted components are hashed under their real relative
    path; falling back to ``git status`` text would yield a C-quoted
    path that no longer matches the file on disk.

    Returns a sha1 hex digest covering ``(path, lstat-mode, size,
    content)`` of each changed file, sorted for determinism.  Returns
    the literal ``"empty"`` sentinel when no paths would be staged.
    Read-only — never runs any mutating Git command.
    """
    hasher = hashlib.sha1()
    found_any = False
    root = project_path.resolve()
    # ``get_tracked_path_modes`` returns the staged mode for every
    # tracked entry, including gitlinks (``160000``).  We need this to
    # distinguish submodules from regular directories so the hasher can
    # capture the submodule's working-tree HEAD instead of collapsing
    # the path to a constant ``"not-a-regular-file"`` marker (Codex
    # P1-2 round 11).
    tracked_modes = get_tracked_path_modes(project_path)
    for relative_path in sorted(enumerate_changed_paths(project_path)):
        found_any = True
        # Use the raw path (no ``.resolve()``) so we can ``lstat`` the
        # symlink itself instead of following it.  ``Path.resolve()``
        # would walk the link chain before we ever see the link entry.
        raw_path = project_path / relative_path
        hasher.update(b"path:")
        hasher.update(relative_path.encode("utf-8", errors="replace"))
        hasher.update(b"\x00")
        # Gitlink / submodule entry: include the submodule's working-tree
        # HEAD SHA so a pointer change (commit B → commit C) perturbs
        # the hash even though the directory lstat mode and the
        # ``git status --short`` text would otherwise be unchanged.
        # Submodule paths are tracked in the index with mode 160000; we
        # detect them via ``get_tracked_path_modes`` (consulted once
        # per call above) rather than via filesystem probing so a
        # submodule whose checkout directory is missing still hashes
        # deterministically (the recorded gitlink SHA comes from the
        # index entry, but the working-tree HEAD is what ``git add -A``
        # would record, so we resolve that via
        # ``get_submodule_head``).
        if tracked_modes.get(relative_path) == GITLINK_MODE:
            hasher.update(b"gitlink-mode:")
            hasher.update(GITLINK_MODE.encode("ascii"))
            hasher.update(b"\x00")
            sub_sha = get_submodule_head(project_path, relative_path)
            if sub_sha:
                hasher.update(b"submodule-sha:")
                hasher.update(sub_sha.encode("ascii", errors="replace"))
            else:
                hasher.update(b"submodule-uninitialized")
            hasher.update(b"\x00")
            continue
        try:
            link_stat = raw_path.lstat()
        except OSError:
            # Tracked file deleted, or rename source.  Stable across
            # staging: a deleted file is deleted whether or not the
            # deletion has been staged yet.
            hasher.update(b"deleted")
            hasher.update(b"\x00")
            continue
        # Always include the lstat mode so a post-review chmod (or any
        # other type/mode change) perturbs the hash.  This is what lets
        # the drift check refuse a commit that absorbs an unreviewed
        # executable-bit flip (Codex P2-1 round 9).
        hasher.update(b"lstat-mode:")
        hasher.update(f"{link_stat.st_mode:o}".encode("ascii"))
        hasher.update(b"\x00")
        if stat.S_ISLNK(link_stat.st_mode):
            # Do NOT follow the link.  Hash the stored target string as
            # Git stages it (mode 120000, blob = target bytes).  The
            # outer env guard already refused any link whose target
            # references ``.env``; reaching here means the target is
            # benign, but we still hash the target string (not the
            # destination's content) so a target swap after review is
            # detected as drift.
            try:
                target = os.readlink(raw_path)
            except OSError as exc:
                hasher.update(b"readlink-failed:")
                hasher.update(str(exc).encode("utf-8", errors="replace"))
                hasher.update(b"\x00")
                continue
            hasher.update(b"symlink-target:")
            hasher.update(target.encode("utf-8", errors="replace"))
            hasher.update(b"\x00")
            continue
        try:
            file_path = raw_path.resolve()
        except OSError:
            hasher.update(b"resolve-failed")
            hasher.update(b"\x00")
            continue
        if not _is_child_path(root, file_path):
            hasher.update(b"outside-worktree")
            hasher.update(b"\x00")
            continue
        if not file_path.is_file():
            hasher.update(b"not-a-regular-file")
            hasher.update(b"\x00")
            continue
        try:
            data = file_path.read_bytes()
        except OSError as exc:
            hasher.update(b"unreadable:")
            hasher.update(str(exc).encode("utf-8", errors="replace"))
            hasher.update(b"\x00")
            continue
        hasher.update(b"size:")
        hasher.update(str(len(data)).encode("ascii"))
        hasher.update(b"\x00")
        hasher.update(b"sha1:")
        hasher.update(hashlib.sha1(data).hexdigest().encode("ascii"))
        hasher.update(b"\x00")
    if not found_any:
        return "empty"
    return hasher.hexdigest()


def compute_review_snapshot(project_path: Path) -> dict[str, str | None]:
    """Capture a stable snapshot of the worktree for drift detection.

    The snapshot mirrors exactly what Codex reviewed and what a subsequent
    ``git add -A && git commit`` would land.  It contains:

    * ``headSha`` — the current ``HEAD`` commit SHA, so any external
      commit between artifact collection and the controlled commit is
      detected.
    * ``statusHash`` — sha1 of the short ``git status`` output.  Captures
      changes that do not show up in the diff text (e.g. empty files,
      mode-only changes reported by ``git status``).
    * ``diffHash`` — sha1 over ``(path, size, sha1(content))`` of every
      file ``git add -A`` would stage, computed by
      ``_hash_changed_paths_bytes``.  **Stable across staging**: the
      helper reads file bytes from disk rather than consulting the
      index, so it returns the same value before and after
      ``git add -A``.  This invariance is what lets ``controlled_commit``
      re-verify the snapshot AFTER staging and detect a TOCTOU mutation
      between the pre-stage drift check and ``git add -A`` (Codex P1-2
      round 8).
    * ``treeSha`` — the immutable tree SHA ``git add -A && git
      write-tree`` would produce, computed by
      ``compute_worktree_tree_sha`` against a temporary index.  Unlike
      ``diffHash`` (which hashes worktree bytes), ``treeSha`` reflects
      what the *index* would contain after staging — so a clean filter
      installed between artifact collection and commit time, or any
      concurrent index mutation, perturbs ``treeSha`` even when the
      worktree bytes are unchanged (Codex P1-3 round 13).

    Path-safe: enumerates every path ``git add -A`` would stage first
    and raises ``EnvFileChangedError`` when any of them references
    ``.env`` or ``.env.*`` (including untracked files nested inside
    untracked directories).  This guard runs BEFORE any file-byte read,
    diff read, or hash, so the backend never inspects ``.env`` content
    even when a forbidden change slips in between artifact collection and
    PASS-time verification.  ``controlled_commit`` and
    ``_verify_review_snapshot_at_pass`` additionally run their own
    path-safety check before invoking this helper so the error surfaces
    with the most actionable message.

    Pure read-only with respect to the live repo state — the temp index
    used by ``compute_worktree_tree_sha`` lives in a unique file inside
    the git dir and is removed at the end.
    """
    # Path-safety guard FIRST: enumerate every path ``git add -A`` would
    # stage (tracked + untracked, including untracked files nested inside
    # untracked directories) and refuse to read any content when a
    # ``.env`` segment appears.  Symlink-aware (Codex P1-1 round 9): a
    # benign-named symlink whose stored target string or resolved target
    # path references ``.env`` is also reported, so the backend never
    # reads, hashes, or diffs ``.env`` content through a link.
    env_paths = enumerate_env_violations(project_path)
    if env_paths:
        raise EnvFileChangedError(
            "A .env file is present in the worktree; review snapshot "
            "collection was blocked before any content was read. Paths: "
            + ", ".join(env_paths)
        )

    # Clean-filter guard (Codex P1-2 round 14): ``compute_worktree_tree_sha``
    # executes ``git add -A`` against a temporary index, which applies any
    # configured clean/process filter on the changed paths.  The Codex
    # review artifacts, however, are built from raw worktree bytes
    # (``git diff HEAD`` + ``_untracked_file_diff`` reading file bytes
    # directly).  A deterministic clean filter therefore transforms content
    # into something Codex never saw, and the resulting committed tree
    # carries that transformed content.  The snapshot's ``treeSha`` would
    # also reflect the filtered tree, so the drift check would still pass
    # while the review and the commit diverge.  Reject any path with an
    # active clean/process filter so the reviewed content and the staged
    # content are guaranteed identical.
    filtered_paths = find_clean_filtered_paths(project_path)
    if filtered_paths:
        raise GitError(
            "Refusing to compute review snapshot: changes include paths "
            "with a configured clean/process Git filter, which can "
            "transform content during staging so the reviewed bytes no "
            "longer match the staged bytes. Remove the filter "
            "configuration for these paths before committing: "
            + ", ".join(filtered_paths)
        )

    # Codex P1-1 round 14: HEAD resolution must succeed and return a
    # non-empty SHA.  Returning ``headSha=None`` on failure would let
    # downstream callers skip the HEAD drift check, the CAS ref update
    # (which uses ``expected_head``), and the merge base verification —
    # all of which depend on a real HEAD SHA to compare against.
    head_result = _run_git(project_path, ["rev-parse", "HEAD"])
    if head_result.returncode != 0:
        raise GitError(
            (head_result.stderr or head_result.stdout or "").strip()
            or "git rev-parse HEAD failed."
        )
    head_sha = head_result.stdout.strip()
    if not head_sha:
        raise GitError("git rev-parse HEAD returned an empty SHA.")
    status = git_status(project_path)
    content_hash = _hash_changed_paths_bytes(project_path)
    tree_sha = compute_worktree_tree_sha(project_path)
    return {
        "headSha": head_sha,
        "statusHash": hashlib.sha1(status.encode("utf-8", errors="replace")).hexdigest(),
        "diffHash": content_hash,
        "treeSha": tree_sha,
    }


def get_branch_head(project_path: Path, branch: str) -> str | None:
    """Return the commit SHA that ``refs/heads/<branch>`` resolves to.

    Returns ``None`` when the branch does not exist or Git refuses to resolve
    it.  Read-only — used to detect external branch movement before a merge.
    """
    result = _run_git(project_path, ["rev-parse", "--verify", f"refs/heads/{branch}"])
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def is_ancestor(project_path: Path, ancestor: str, descendant: str) -> bool:
    """Return True when ``ancestor`` is reachable from ``descendant``'s history.

    Uses ``git merge-base --is-ancestor`` so the answer reflects Git's own
    graph walk.  Returns ``False`` when either commit is unknown or when
    ``ancestor`` is not an ancestor of ``descendant``.  Read-only — used to
    ensure the reviewed base of a controlled commit is already in the
    primary worktree's history before a one-click merge, so merging the
    controlled commit cannot sweep unreviewed pre-task commits into the
    trunk (Codex P1-1 round 12).

    Codex P2-1 round 19: fail-closed semantics.  ``git merge-base
    --is-ancestor`` documents exactly two exit codes:

    * 0 — ``ancestor`` IS an ancestor of ``descendant``;
    * 1 — ``ancestor`` is NOT an ancestor of ``descendant`` (including the
      case where either commit does not exist).

    Every other return code indicates a Git-level failure (corrupted
    object database, unreadable refs, subprocess crash, etc.).  Previously
    any non-zero exit was collapsed to ``False``, which silently let the
    merge reachability check pass through to the "not an ancestor" branch
    even when the real cause was that the probe could not run at all —
    then the audit trail recorded ``head_unreachable`` for what was in
    fact an unclassifiable probe failure.  Now ``True`` and ``False`` are
    returned only for the two documented success exits and every other
    case raises ``GitError`` with the underlying command output so the
    caller can record a probe-failure audit event and block rather than
    silently treating a broken probe as a clean reachability decision.
    """
    if not ancestor or not descendant:
        return False
    try:
        result = _run_git(
            project_path,
            ["merge-base", "--is-ancestor", ancestor, descendant],
        )
    except GitError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(
            f"git merge-base --is-ancestor could not execute: {exc}"
        ) from exc
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    stderr = (result.stderr or result.stdout or "").strip()
    raise GitError(
        stderr
        or (
            f"git merge-base --is-ancestor exited with code "
            f"{result.returncode} (ancestor={ancestor[:10]}, "
            f"descendant={descendant[:10]}); unable to classify reachability."
        )
    )


# Git mode (as reported by ``git ls-files --stage`` / ``git ls-tree``) for a
# gitlink/submodule entry.  Used by ``_hash_changed_paths_bytes`` to detect
# submodule pointer drift (Codex P1-2 round 11).
GITLINK_MODE = "160000"


def get_tracked_path_modes(project_path: Path) -> dict[str, str]:
    """Return a map of relative path → Git mode string for tracked entries.

    Uses ``git ls-files --stage -z`` so paths containing non-ASCII or
    otherwise quoted components are emitted verbatim.  Modes are the
    canonical Git object modes: ``100644`` (regular file), ``100755``
    (executable regular file), ``120000`` (symlink), ``160000``
    (gitlink/submodule).  Read-only — never runs any mutating Git command.

    Fails closed (Codex P1-1 round 13): any failure from
    ``git ls-files --stage`` raises ``GitError`` so the snapshot hash
    cannot silently collapse submodule entries to a constant marker
    while later commit commands succeed.
    """
    result = _run_git(project_path, ["ls-files", "--stage", "-z"])
    if result.returncode != 0:
        raise GitError(
            (result.stderr or result.stdout or "").strip()
            or "git ls-files --stage failed."
        )
    modes: dict[str, str] = {}
    for record in result.stdout.split("\0"):
        if not record:
            continue
        # ``git ls-files --stage -z`` record format:
        # ``<mode> <sha> <stage>\t<path>``
        try:
            meta, path = record.split("\t", 1)
        except ValueError:
            continue
        parts = meta.split()
        if len(parts) >= 1 and path:
            modes[path] = parts[0]
    return modes


def get_submodule_head(project_path: Path, relative_path: str) -> str | None:
    """Return the working-tree HEAD SHA of a submodule.

    Runs ``git -C <submodule> rev-parse HEAD`` against the submodule's
    checkout directory.  Returns ``None`` when the submodule directory is
    missing (uninitialized checkout), not a directory, or Git refuses to
    resolve HEAD.  Read-only — used to detect submodule pointer drift
    (Codex P1-2 round 11).
    """
    sub_path = project_path / relative_path
    try:
        if not sub_path.is_dir():
            return None
    except OSError:
        return None
    result = _run_git(sub_path, ["rev-parse", "HEAD"])
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def get_commit_parents(project_path: Path, commit_sha: str) -> list[str]:
    """Return the parent commit SHAs of ``commit_sha``.

    Uses ``git show -s --format=%P`` so the answer reflects Git's own
    graph walk (including merge commits with multiple parents).  Returns
    an empty list when the commit is unknown or Git refuses to resolve it.
    Read-only — used to verify the controlled commit has the reviewed base
    as its sole parent before a one-click merge (Codex P1-2 round 13).
    """
    if not commit_sha:
        return []
    result = _run_git(
        project_path,
        ["show", "-s", "--format=%P", commit_sha],
    )
    if result.returncode != 0:
        return []
    return [sha for sha in result.stdout.split() if sha]


def get_in_progress_operations(project_path: Path) -> list[str]:
    """Return a list of in-progress Git operation names for ``project_path``.

    Detects the standard marker files / directories Git creates inside the
    git dir when an operation is mid-flight: ``MERGE_HEAD`` (merge),
    ``CHERRY_PICK_HEAD`` (cherry-pick), ``REVERT_HEAD`` (revert),
    ``REBASE_HEAD`` (rebase), ``BISECT_LOG`` (bisect), ``AM_HEAD``
    (``git am`` mailbox apply), and the ``sequencer``, ``rebase-merge``,
    and ``rebase-apply`` directories (ongoing cherry-pick / revert queue
    and the rebase back-ends).  Read-only — used to reject a controlled
    commit when the repository is in an in-progress state, because
    ``git commit`` would then finalize that operation and the resulting
    commit could carry unreviewed parents (Codex P1-2 round 13).

    Codex P1-4 round 17: previously the helper called ``git rev-parse
    --git-path <marker>`` once per marker and silently ``continue``d on
    any non-zero return code.  A non-zero exit from ``rev-parse`` is
    typically a real repository error (corrupt HEAD, unreadable config,
    broken common dir), not "marker absent" — only a successful exit
    with empty stdout means "marker does not exist".  Treating errors as
    "marker absent" silently degraded the safety boundary: the
    controlled commit / merge would proceed even though the in-progress
    state could not be verified.  Additionally, the helper missed three
    standard markers (``rebase-merge/`` and ``rebase-apply/`` directories
    for the two rebase back-ends, and ``AM_HEAD`` for ``git am``); a
    rebase in progress via the ``rebase-merge`` back-end (Git's default
    since 2.6) would not be detected because ``REBASE_HEAD`` only
    exists while the rebase is actively resolving a conflict.

    Fail-closed: a single ``git rev-parse --absolute-git-dir`` call
    resolves the git directory (raises ``GitError`` on failure).  Marker
    files / directories are then checked directly via ``Path.exists`` /
    ``is_dir`` so a marker check never silently degrades on a Git
    subprocess error.  ``OSError`` while probing the filesystem is also
    raised as ``GitError`` so the caller surfaces the failure as a
    ``COMMIT_BLOCKED`` / ``MERGE_BLOCKED`` audit record instead of
    treating it as "marker absent".
    """
    git_dir_result = _run_git(project_path, ["rev-parse", "--absolute-git-dir"])
    if git_dir_result.returncode != 0:
        raise GitError(
            (git_dir_result.stderr or git_dir_result.stdout or "").strip()
            or "git rev-parse --absolute-git-dir failed while probing in-progress operations."
        )
    git_dir = Path(git_dir_result.stdout.strip())
    if not git_dir:
        raise GitError("git rev-parse --absolute-git-dir returned an empty path.")

    operations: list[str] = []

    # File markers: existence implies the named operation is mid-flight.
    file_markers = [
        ("MERGE_HEAD", "merge"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
        ("REBASE_HEAD", "rebase"),
        ("BISECT_LOG", "bisect"),
        ("AM_HEAD", "am"),
    ]
    # Directory markers: presence of the directory implies the named
    # operation's bookkeeping queue exists.  ``sequencer`` covers both
    # cherry-pick and revert (the per-record markers above distinguish
    # the current step), and the two ``rebase-*`` directories cover the
    # ``merge`` and ``apply`` back-ends respectively (Git 2.6+ defaults
    # to ``rebase-merge``).
    dir_markers = [
        ("sequencer", "sequencer"),
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
    ]

    try:
        for marker, name in file_markers:
            marker_path = git_dir / marker
            if marker_path.exists():
                operations.append(name)
        for marker, name in dir_markers:
            marker_path = git_dir / marker
            if marker_path.is_dir():
                # Avoid duplicate ``rebase`` entries when both the
                # directory and a per-step file marker exist for the
                # same rebase.
                if name not in operations:
                    operations.append(name)
    except OSError as exc:
        raise GitError(
            f"Filesystem error while probing in-progress operation markers in {git_dir}: {exc}"
        ) from exc
    return operations


def compute_worktree_tree_sha(project_path: Path) -> str:
    """Compute the tree SHA ``git add -A && git write-tree`` would produce.

    Uses a temporary index file (``GIT_INDEX_FILE``) so the main index is
    never disturbed.  The temp index is seeded from HEAD via
    ``git read-tree HEAD``, then ``git add -A`` stages every worktree
    change (tracked modifications + untracked files, with clean filters
    applied just like a real ``git add -A`` would), and finally
    ``git write-tree`` captures the immutable tree object SHA.

    This SHA is the **immutable** representation of "what would be
    committed".  Storing it in the reviewed snapshot and comparing it
    against the actual staged-tree SHA captured after the real
    ``git add -A`` lets ``controlled_commit`` detect:

    * a clean filter configured between artifact collection and commit
      time (the real index would then have filtered content while the
      snapshot was captured without the filter);
    * concurrent index mutations between the snapshot and the commit
      (the real index would have different content than the snapshot
      implies);
    * any other drift that affects the staged set but not the worktree
      bytes on disk (which the existing ``diffHash`` may miss).

    Read-only with respect to the actual repo state — the temp index is
    created in a unique file inside the git dir and removed at the end.

    Raises ``GitError`` on any Git failure so callers fail closed.
    """
    git_dir_result = _run_git(project_path, ["rev-parse", "--absolute-git-dir"])
    if git_dir_result.returncode != 0:
        raise GitError(
            (git_dir_result.stderr or git_dir_result.stdout or "").strip()
            or "git rev-parse --absolute-git-dir failed."
        )
    git_dir = Path(git_dir_result.stdout.strip())
    if not git_dir.is_dir():
        raise GitError(f"Resolved git dir is not a directory: {git_dir}")

    import tempfile

    temp_index_fd, temp_index_str = tempfile.mkstemp(
        prefix="cdl-temp-index-",
        suffix=".idx",
        dir=str(git_dir),
    )
    os.close(temp_index_fd)
    temp_index = Path(temp_index_str)
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = str(temp_index)
    try:
        read_result = subprocess.run(
            ["git", "-C", str(project_path), "read-tree", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=env,
        )
        if read_result.returncode != 0:
            raise GitError(
                (read_result.stderr or read_result.stdout or "").strip()
                or "git read-tree HEAD failed."
            )
        add_result = subprocess.run(
            ["git", "-C", str(project_path), "add", "-A"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=env,
        )
        if add_result.returncode != 0:
            raise GitError(
                (add_result.stderr or add_result.stdout or "").strip()
                or "git add -A failed."
            )
        write_result = subprocess.run(
            ["git", "-C", str(project_path), "write-tree"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=env,
        )
        if write_result.returncode != 0:
            raise GitError(
                (write_result.stderr or write_result.stdout or "").strip()
                or "git write-tree failed."
            )
        tree_sha = write_result.stdout.strip()
        if not tree_sha:
            raise GitError("git write-tree returned an empty tree SHA.")
        return tree_sha
    finally:
        try:
            temp_index.unlink()
        except OSError:
            pass


def get_index_tree_sha(project_path: Path) -> str:
    """Return the tree SHA of the current index (``git write-tree``).

    Used at commit time after ``git add -A`` to capture the actual staged
    tree SHA for comparison against the expected value captured at
    artifact-collection time.  Read-only with respect to the worktree —
    the index already reflects the staged content; ``write-tree``
    materializes it into an immutable tree object without touching HEAD.

    Fails closed on any Git failure (Codex P1-1 round 13).
    """
    result = _run_git(project_path, ["write-tree"])
    if result.returncode != 0:
        raise GitError(
            (result.stderr or result.stdout or "").strip()
            or "git write-tree failed."
        )
    tree_sha = result.stdout.strip()
    if not tree_sha:
        raise GitError("git write-tree returned an empty tree SHA.")
    return tree_sha
