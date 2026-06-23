"""Controlled Git operations triggered by explicit GUI buttons.

The default Claude/Codex development loop never touches Git's mutating
commands.  This module is the only place where the backend is allowed to run
``git commit``, ``git merge-tree``, ``git commit-tree``, ``git worktree add``
etc., and only behind the strict validation required by the task acceptance
criteria:

* Worktree creation validates branch names, target paths and main worktree
  cleanliness, then delegates to ``git worktree add -b <branch> <path>
  <start_sha>`` using a caller-captured start SHA so a concurrent
  controlled merge cannot advance main between the checks and the
  checkout (Codex P1-2 round 19).
* Commits are rejected when the worktree is clean (no empty commits), when a
  ``.env`` file is touched, or when the user message is empty.  Commits are
  authored via ``git commit-tree`` from the pinned staged tree SHA and
  advanced atomically via ``git update-ref HEAD <new> <old>``.
* Merges use a validated ``git merge-tree --write-tree`` → ``commit-tree``
  → CAS ref update → guarded ``read-tree -m -u`` sequence with a durable
  recovery journal (Codex P1-1 round 19).  When ``read-tree`` fails the
  HEAD is reverse-CAS'd to the pre-merge commit; if the journal still
  exists at the next lifecycle / Git operation, recovery is attempted
  under the same per-task and per-resource locks.

No function here ever calls ``git push``, ``git branch -D``, ``git reset``,
``git clean``, or ``git merge`` (the legacy in-place merge command).  The
controlled merge flow computes the merge result via ``merge-tree`` and
``commit-tree`` directly so conflicts surface before any ref mutation and
no half-merged state is left on the repository.  Worktrees and branches
are never deleted by this module.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .git_tools import (
    GitError,
    _run_git,
    compute_review_snapshot,
    enumerate_env_violations,
    find_custom_merge_driver_paths,
    find_smudge_filtered_paths,
    get_branch_head,
    get_commit_parents,
    get_current_branch,
    get_git_common_dir,
    get_in_progress_operations,
    get_index_tree_sha,
    git_status,
    is_ancestor,
    list_tracked_paths,
)
from .path_safety import path_has_env_segment


class WorktreeCreationError(RuntimeError):
    pass


class CommitError(RuntimeError):
    pass


class MergeError(RuntimeError):
    pass


_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]*$")
_RESERVED_BRANCH_NAMES = {"head", "master", "main", "trunk", "develop"}


_EMPTY_HOOKS_DIR: Path | None = None

# Git commands whose hooks can mutate the worktree, index, or commit object
# after the GUI backend has already validated state.  ``commit`` triggers
# pre-commit / prepare-commit-msg / commit-msg / post-commit; ``merge``
# triggers prepare-commit-msg / post-merge; ``commit-tree`` (used as a
# pre-validated alternative to ``commit`` — Codex P1-3 round 13) triggers
# prepare-commit-msg when the ``-m`` flag is not the only message source.
# ``worktree add`` triggers the repo's ``post-checkout`` hook in the new
# worktree (Codex P1-3 round 15), which can run arbitrary commands or
# write extra files while the controlled creation flow reports success.
# Any of these can stage extra files (including ``.env``), rewrite the
# commit message, or mutate the working tree after our checks pass.
_HOOKED_COMMANDS = {"commit", "merge", "commit-tree", "worktree"}


def _empty_hooks_dir() -> Path:
    """Lazily create and reuse an empty directory used to disable Git hooks.

    Returns a stable, empty directory path that is passed to Git via
    ``core.hooksPath`` so the controlled commit, merge, and worktree-add
    invocations do not run any repository-defined hooks (pre-commit /
    commit-msg / post-commit / post-merge / post-checkout / etc.).  This
    prevents a malicious or buggy hook from mutating the working tree,
    index, or commit object after the GUI backend has validated the
    worktree state (Codex P1-1 / P1-2 round 10, P1-3 round 15).

    The directory is created once per process and re-used for every
    subsequent no-hooks invocation.  Best-effort cleanup happens at
    process exit.
    """
    global _EMPTY_HOOKS_DIR
    if _EMPTY_HOOKS_DIR is None or not _EMPTY_HOOKS_DIR.is_dir():
        path = Path(tempfile.mkdtemp(prefix="cdl-no-hooks-"))
        _EMPTY_HOOKS_DIR = path
        atexit.register(lambda p=path: shutil.rmtree(str(p), ignore_errors=True))
    return _EMPTY_HOOKS_DIR


def _run_git_text(project_path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a Git command, disabling hooks for ``commit`` / ``merge`` / ``worktree``.

    For mutating commands that trigger Git hooks (``commit``, ``merge``,
    ``commit-tree``, ``worktree``), the subprocess environment is
    configured to point ``core.hooksPath`` at an empty directory so
    repository-defined hooks cannot stage additional files (e.g.
    ``.env``), rewrite the commit message, or mutate the working tree
    after the GUI backend has validated state (Codex P1-1 / P1-2 round
    10, P1-3 round 15).  Other commands (``status``, ``diff``, ``add``,
    ``rev-parse``, etc.) do not trigger hooks and run unchanged so
    existing tests that patch this function continue to work.

    Hooks are disabled via ``GIT_CONFIG_*`` environment variables instead
    of ``-c core.hooksPath=...`` CLI args so the ``args`` list remains
    identical to the previous signature: tests that inspect
    ``args[0]`` / ``args[-1]`` to detect merge / commit invocations keep
    working without modification.

    Codex P1-1 round 17: when ``args[0] == "merge"`` the env also
    force-clear ``branch.<branch>.mergeOptions`` and ``merge.strategy``
    via two additional ``GIT_CONFIG_*`` entries.  A repository can
    install ``branch.<main>.mergeOptions = -X ours`` (auto-resolve
    conflicts by keeping our side) or ``merge.strategy = ours`` (drop
    the incoming side entirely) in its config; without an override the
    controlled merge button would silently absorb unreviewed content
    into the trunk because the merge "succeeds" with no conflicts even
    though a manual merge would have rejected the diff.  The current
    branch is resolved once and patched into the override so a malicious
    config cannot bypass the per-merge conflict-detection safety
    boundary.  ``controlled_merge_to_main`` separately refuses to start
    when the repo config is detected to carry an unsafe override so
    the audit trail records the rejection.
    """
    if args and args[0] in _HOOKED_COMMANDS:
        hooks_dir = _empty_hooks_dir()
        env = dict(os.environ)
        # Always disable hooks via ``core.hooksPath``.  For ``merge``
        # we additionally add two more ``GIT_CONFIG_*`` entries to
        # force-clear unsafe per-branch / global merge-strategy
        # overrides (Codex P1-1 round 17).
        if args[0] == "merge":
            main_branch = _resolve_merge_branch_for_override(project_path)
            env["GIT_CONFIG_COUNT"] = "3"
            env["GIT_CONFIG_KEY_0"] = "core.hooksPath"
            env["GIT_CONFIG_VALUE_0"] = str(hooks_dir)
            env["GIT_CONFIG_KEY_1"] = "merge.strategy"
            env["GIT_CONFIG_VALUE_1"] = ""
            if main_branch:
                env["GIT_CONFIG_KEY_2"] = f"branch.{main_branch}.mergeOptions"
                env["GIT_CONFIG_VALUE_2"] = ""
            else:
                # No branch resolution: emit an empty dummy entry so the
                # ``GIT_CONFIG_COUNT`` tally stays consistent.
                env["GIT_CONFIG_KEY_2"] = "merge.guaranteedNoOpKey"
                env["GIT_CONFIG_VALUE_2"] = ""
        else:
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "core.hooksPath"
            env["GIT_CONFIG_VALUE_0"] = str(hooks_dir)
        command = ["git", "-C", str(project_path), *args]
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=env,
        )
    return _run_git(project_path, args)


def _resolve_merge_branch_for_override(project_path: Path) -> str | None:
    """Return the current branch name for the per-branch mergeOptions override.

    Codex P1-1 round 17: ``branch.<branch>.mergeOptions`` is keyed by
    branch name, so we need to resolve the current branch to know which
    config entry to clear.  Uses ``git rev-parse --abbrev-ref HEAD``
    which returns ``HEAD`` for detached HEAD state; in that case we
    return ``None`` so the caller emits a dummy ``GIT_CONFIG_*`` entry
    instead of ``branch.HEAD.mergeOptions`` (which is not a real key).
    """
    result = _run_git(project_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def _detect_unsafe_merge_config(project_path: Path, main_branch: str) -> str | None:
    """Return a reason string when the repo config carries an unsafe merge override.

    Codex P1-1 round 17: ``branch.<main>.mergeOptions`` can carry flags
    like ``-X ours`` / ``-X theirs`` / ``-s ours`` that silently
    auto-resolve what should have been a conflict.  ``merge.strategy``
    can do the same globally.  Refuse to merge when either is set to a
    non-empty value so the controlled merge safety boundary (reject on
    conflict) is preserved; the env override in ``_run_git_text`` clears
    them defensively for the merge invocation but we still refuse the
    request up front so the user sees a clear error and the audit
    trail records the rejection.

    ``git config --get <key>`` returns exit code 1 when the key is
    absent; any other non-zero return is a real config error and is
    surfaced as ``MergeError`` so the caller records ``MERGE_BLOCKED``
    rather than silently proceeding.
    """
    if main_branch:
        merge_opts_result = _run_git(
            project_path,
            ["config", "--get", f"branch.{main_branch}.mergeOptions"],
        )
        if merge_opts_result.returncode == 0:
            value = merge_opts_result.stdout.strip()
            if value:
                return (
                    f"branch.{main_branch}.mergeOptions is set to '{value}'; "
                    "this can auto-resolve merge conflicts and bypass the "
                    "controlled-merge safety boundary. Remove the config "
                    "entry before merging."
                )
        elif merge_opts_result.returncode != 1:
            raise MergeError(
                (merge_opts_result.stderr or merge_opts_result.stdout or "").strip()
                or f"git config --get branch.{main_branch}.mergeOptions failed unexpectedly."
            )
    strategy_result = _run_git(
        project_path,
        ["config", "--get", "merge.strategy"],
    )
    if strategy_result.returncode == 0:
        value = strategy_result.stdout.strip()
        if value:
            return (
                f"merge.strategy is set to '{value}'; this can auto-resolve "
                "merge conflicts and bypass the controlled-merge safety "
                "boundary. Remove the config entry before merging."
            )
    elif strategy_result.returncode != 1:
        raise MergeError(
            (strategy_result.stderr or strategy_result.stdout or "").strip()
            or "git config --get merge.strategy failed unexpectedly."
        )
    return None


def validate_branch_name(branch: str) -> str:
    branch = (branch or "").strip()
    if not branch:
        raise WorktreeCreationError("Branch name is required.")
    if "/" in branch and branch.endswith("/"):
        raise WorktreeCreationError("Branch name must not end with a slash.")
    if "//" in branch or ".." in branch:
        raise WorktreeCreationError("Branch name contains invalid sequences.")
    if not _BRANCH_NAME_RE.match(branch):
        raise WorktreeCreationError(
            "Branch name must start with a letter or digit and only contain "
            "letters, digits, dots, dashes, and slashes."
        )
    if path_has_env_segment(branch):
        raise WorktreeCreationError("Branch name must not reference .env files.")
    if branch.lower() in _RESERVED_BRANCH_NAMES:
        raise WorktreeCreationError(f"Branch name '{branch}' is reserved.")
    return branch


def validate_target_path(main_path: Path, target_path: Path) -> Path:
    if target_path is None:
        raise WorktreeCreationError("Target path is required.")
    raw = str(target_path).strip()
    if not raw:
        raise WorktreeCreationError("Target path is required.")
    try:
        resolved = Path(raw).expanduser().resolve(strict=False)
    except (OSError, ValueError) as exc:
        raise WorktreeCreationError(f"Invalid target path: {exc}") from exc
    if path_has_env_segment(str(resolved)):
        raise WorktreeCreationError("Target path must not contain .env segments.")
    try:
        main_resolved = main_path.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise WorktreeCreationError(f"Main worktree path is not available: {exc}") from exc
    if resolved == main_resolved:
        raise WorktreeCreationError("Target path must not equal the main worktree path.")
    if main_resolved in resolved.parents:
        raise WorktreeCreationError(
            "Target path must not live inside the main worktree directory."
        )
    if resolved.exists():
        raise WorktreeCreationError("Target path already exists.")
    if not resolved.parent.exists():
        raise WorktreeCreationError("Target parent directory does not exist.")
    return resolved


def create_worktree(
    main_path: Path,
    branch: str,
    target_path: Path,
    start_sha: str | None = None,
) -> dict:
    """Create a development worktree at ``target_path`` on a new ``branch``.

    Returns metadata describing the new worktree.  Never deletes anything on
    failure: if ``git worktree add`` refuses, the target directory is left
    untouched (Git itself does not create it on failure).

    Codex P1-2 round 19: when ``start_sha`` is supplied, the new
    worktree is created from that exact commit rather than the implicit
    ``HEAD``.  The caller (``create_project_worktree`` in ``gui.server``)
    captures ``start_sha`` *inside* the primary worktree's resource lock
    after the clean / filter checks pass, then passes the captured SHA
    here so the actual ``git worktree add`` invocation operates on the
    validated commit.  This closes a TOCTOU window where a concurrent
    controlled merge could advance ``HEAD`` between the checks and the
    worktree creation, causing the new worktree to be checked out from a
    SHA that was never validated.  When ``start_sha`` is ``None``
    (legacy callers / direct invocations) the implicit ``HEAD`` flow is
    preserved so existing tests continue to work.
    """
    if not main_path.exists() or not main_path.is_dir():
        raise WorktreeCreationError("Main worktree path does not exist.")
    branch = validate_branch_name(branch)
    target = validate_target_path(main_path, target_path)

    status = git_status(main_path)
    if status.strip():
        raise WorktreeCreationError(
            "Main worktree is dirty; commit or stash changes before creating a worktree."
        )

    branch_check = _run_git_text(main_path, ["rev-parse", "--verify", f"refs/heads/{branch}"])
    if branch_check.returncode == 0:
        raise WorktreeCreationError(f"Branch '{branch}' already exists.")

    # Codex P1-2 round 19: validate the caller-supplied ``start_sha``
    # before any further work.  When supplied, the SHA must resolve to a
    # real commit (fail-closed on a probe error rather than silently
    # falling through to the implicit HEAD).  An invalid SHA at this
    # point means the caller's lock-protected capture failed — surfacing
    # it here keeps the worktree from being created from a SHA that was
    # never validated.
    if start_sha is not None:
        start_sha = start_sha.strip()
        if not start_sha:
            raise WorktreeCreationError(
                "Worktree creation requires a non-empty start SHA when one is provided."
            )
        start_check = _run_git_text(
            main_path, ["rev-parse", "--verify", f"{start_sha}^{{commit}}"]
        )
        if start_check.returncode != 0:
            stderr = (start_check.stderr or start_check.stdout or "").strip()
            raise WorktreeCreationError(
                "Captured start SHA could not be verified; refusing to create "
                "worktree from an unvalidated commit. "
                + stderr
            )
        # Normalise to the resolved commit SHA so the ``git worktree add``
        # invocation below uses a deterministic reference rather than
        # whatever string the caller happened to pass (e.g. a short SHA
        # or a symbolic ref).
        start_sha = start_check.stdout.strip()

    # Codex P1-2 round 18: ``git worktree add`` performs an initial
    # checkout of every tracked path from HEAD into the new worktree.
    # Smudge / process Git filters fire during that checkout, so a
    # filter configured on any tracked path would transform the
    # materialised bytes after the user pressed the worktree-create
    # button — the new worktree's files would not match the reviewed
    # HEAD tree.  Enumerate every tracked path and refuse up front
    # when any path has an active smudge / process filter.  ``git
    # worktree add`` is in ``_HOOKED_COMMANDS`` so post-checkout hooks
    # are disabled (round 15); this guard closes the analogous
    # smudge-filter window.
    try:
        tracked_paths = list_tracked_paths(main_path)
    except GitError as exc:
        raise WorktreeCreationError(
            "Failed to enumerate tracked paths for smudge-filter guard; "
            "refusing to create worktree without verifying filter coverage. "
            + str(exc)
        ) from exc
    if tracked_paths:
        try:
            smudge_paths = find_smudge_filtered_paths(main_path, tracked_paths)
        except GitError as exc:
            raise WorktreeCreationError(
                "Failed to probe smudge / process filters on tracked paths; "
                "refusing to create worktree. " + str(exc)
            ) from exc
        if smudge_paths:
            raise WorktreeCreationError(
                "Refusing to create worktree: tracked paths have a "
                "configured smudge / process Git filter, which can "
                "transform content during the initial checkout so the "
                "new worktree's files would not match the reviewed HEAD "
                "tree. Remove the filter configuration for these paths "
                "before creating a worktree: "
                + ", ".join(smudge_paths)
            )

    # Codex P1-2 round 19: pass the captured start SHA explicitly as
    # the final ``git worktree add`` start-point argument so the new
    # worktree is checked out from the validated commit rather than the
    # implicit HEAD (which may have moved between the caller's lock-
    # protected capture and this invocation).
    if start_sha is not None:
        worktree_args = ["worktree", "add", "-b", branch, str(target), start_sha]
    else:
        worktree_args = ["worktree", "add", "-b", branch, str(target)]
    result = _run_git_text(main_path, worktree_args)
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "git worktree add failed.").strip()
        raise WorktreeCreationError(stderr)

    branch_check_after = _run_git_text(target, ["rev-parse", "--abbrev-ref", "HEAD"])
    new_branch = branch
    if branch_check_after.returncode == 0:
        new_branch = branch_check_after.stdout.strip() or branch

    # Codex P1-2 round 19: when a start SHA was supplied, verify the
    # new worktree's HEAD actually points at the validated commit.  If
    # it does not (e.g. a smudge filter or hook managed to perturb the
    # checkout despite the upfront guards), refuse to return success so
    # the caller does not register a worktree that does not match the
    # validated baseline.  The branch / directory are already created
    # at this point, but the explicit raise surfaces the inconsistency
    # rather than silently consuming an unvalidated worktree.
    if start_sha is not None:
        new_head_check = _run_git_text(target, ["rev-parse", "HEAD"])
        if new_head_check.returncode != 0:
            raise WorktreeCreationError(
                "Created worktree's HEAD could not be resolved; refusing to "
                "register a worktree whose baseline cannot be verified. "
                + (new_head_check.stderr or new_head_check.stdout or "").strip()
            )
        new_head = new_head_check.stdout.strip().lower()
        if new_head != start_sha.lower():
            raise WorktreeCreationError(
                f"Created worktree's HEAD ({new_head[:10]}) does not match "
                f"the validated start SHA ({start_sha[:10]}); the new "
                "worktree's baseline was perturbed after the upfront guards. "
                "Refusing to register an unvalidated worktree."
            )

    return {
        "path": str(target),
        "branch": new_branch,
        "worktreeType": "worktree",
    }


def controlled_commit(
    project_path: Path,
    message: str,
    expected_snapshot: dict[str, str | None] | None = None,
) -> dict:
    """Stage all changes in ``project_path`` and create a single commit.

    Refuses to run when the worktree is clean or when a ``.env`` file
    appears in the staged paths.  When ``expected_snapshot`` is supplied
    (captured at artifact-collection time so it mirrors what Codex
    reviewed), the worktree is re-snapshotted and compared against it
    BEFORE staging: any drift in HEAD, file-content hash, or staged
    tree SHA rejects the commit so unreviewed changes cannot be smuggled
    into the reviewed commit.

    After ``git add -A`` runs and BEFORE the commit is created, the
    staged index is re-validated (Codex P1-2 round 8 + P1-3 round 13):

    * The set of paths that would be committed is re-enumerated and a
      ``.env`` segment check is run again, so a forbidden file added
      between the pre-stage guard and ``git add -A`` cannot slip into
      the commit.
    * ``compute_review_snapshot`` is recomputed on the staged worktree
      and compared against ``expected_snapshot``.  Because the snapshot
      hashes file bytes from disk (rather than index state), it is
      stable across staging: a mutation between the pre-stage drift
      check and ``git add -A`` is detected here even when the resulting
      content was staged by ``git add -A`` itself.
    * The staged index tree SHA (``get_index_tree_sha``) is compared
      against the reviewed ``treeSha`` (also captured by
      ``compute_review_snapshot``).  Unlike ``diffHash`` — which hashes
      worktree bytes — ``treeSha`` reflects what the *index* actually
      contains after staging.  A clean filter installed between artifact
      collection and commit time, or any concurrent index mutation,
      perturbs ``treeSha`` even when the worktree bytes are unchanged,
      so the resulting commit cannot carry content that was never
      reviewed (Codex P1-3 round 13).

    Codex P1-2 round 13: before staging, the repository must not be in
    an in-progress operation state (merge / cherry-pick / revert /
    rebase / bisect / sequencer).  ``git commit`` would otherwise
    finalize that operation and the resulting commit could carry
    unreviewed parents (e.g. an in-progress merge) yet still pass every
    HEAD / content drift check.

    Codex P1-3 round 13: the commit is created from the **pinned staged
    tree SHA** via ``git commit-tree`` rather than ``git commit``, so
    no clean filter or post-stage hook can perturb what is recorded.
    HEAD is then advanced atomically via ``git update-ref HEAD <new>
    <expected_head>`` — a Git-native compare-and-swap that fails if
    HEAD moved between the pre-stage drift check and the ref update.

    Returns the new commit SHA and the (cleaned) message.

    The ``.env`` path-safety guard runs BEFORE the drift check so the
    backend never reads, hashes, or diffs ``.env`` content (the drift
    check calls ``compute_review_snapshot`` which would otherwise read
    file bytes / diff content).
    """
    if not project_path.exists() or not project_path.is_dir():
        raise CommitError("Worktree path does not exist.")
    message = (message or "").strip()
    if not message:
        raise CommitError("Commit message is required.")
    if len(message) > 1024:
        raise CommitError("Commit message is too long (max 1024 characters).")

    # Codex P1-2 round 13: refuse to commit when the repository is in
    # an in-progress operation state.  ``git commit`` would otherwise
    # finalize the operation and the resulting commit could carry
    # unreviewed parents (e.g. an in-progress merge) yet pass every
    # HEAD / content drift check below.
    in_progress = get_in_progress_operations(project_path)
    if in_progress:
        raise CommitError(
            "Refusing to commit: repository has an in-progress '"
            + ", ".join(in_progress)
            + "' operation. Abort or finish it before committing reviewed changes."
        )

    # Pre-stage path-safety guard: enumerate every path ``git add -A``
    # would stage (tracked + untracked, including untracked files nested
    # inside untracked directories) and refuse to inspect (let alone
    # stage) any ``.env`` content.  Symlink-aware (Codex P1-1 round 9):
    # a benign-named symlink whose stored target string references
    # ``.env`` is also rejected so the backend never reads the
    # destination's bytes through the link.  ``compute_review_snapshot``
    # enforces this defensively, but running it here first produces a
    # clearer error message, does not depend on the caller passing
    # ``expected_snapshot``, and is the explicit safety boundary Codex
    # requires.
    env_paths = enumerate_env_violations(project_path)
    if env_paths:
        raise CommitError(
            "Refusing to commit: changes include .env or .env.* files: "
            + ", ".join(env_paths)
        )

    expected_head: str | None = None
    if expected_snapshot is not None:
        # Pre-stage drift check runs before the empty-worktree check so a
        # manual commit (which clears the worktree but advances HEAD) is
        # still rejected as drift rather than misclassified as "no
        # changes to commit".
        expected_head = expected_snapshot.get("headSha") or None
        current = compute_review_snapshot(project_path)
        drifts: list[str] = []
        if expected_head and current.get("headSha") != expected_head:
            drifts.append("HEAD moved since review")
        if current.get("statusHash") != expected_snapshot.get("statusHash"):
            drifts.append("git status changed since review")
        if current.get("diffHash") != expected_snapshot.get("diffHash"):
            drifts.append("file content changed since review")
        if current.get("treeSha") != expected_snapshot.get("treeSha"):
            drifts.append("staged tree differs from reviewed tree")
        if drifts:
            raise CommitError(
                "Worktree has drifted from the reviewed PASS snapshot: "
                + "; ".join(drifts)
                + ". Re-review the changes before committing."
            )

    status = git_status(project_path)
    if not status.strip():
        raise CommitError("No changes to commit; refusing to create an empty commit.")

    add_result = _run_git_text(project_path, ["add", "-A"])
    if add_result.returncode != 0:
        raise CommitError((add_result.stderr or "git add failed.").strip())

    # Codex P1-3 round 13: pin the staged tree SHA immediately after
    # ``git add -A`` so a clean filter configured between snapshot time
    # and now (or any concurrent index mutation) is detected before the
    # commit is created.  ``get_index_tree_sha`` runs ``git write-tree``
    # which materializes the index into an immutable tree object
    # without touching HEAD.
    actual_tree_sha = get_index_tree_sha(project_path)

    if expected_snapshot is not None:
        # POST-STAGE VERIFICATION (Codex P1-2 round 8 + P1-3 round 13):
        # close the check-to-stage TOCTOU gap.  ``git add -A`` stages
        # whatever is present at this later moment, so a file edit, new
        # untracked file, or ``.env`` dropped between the pre-stage drift
        # check and ``git add -A`` would otherwise sail into the commit
        # unreviewed.  Re-validate the staged index before the commit
        # gets a chance to run.
        staged_env_paths = enumerate_env_violations(project_path)
        if staged_env_paths:
            raise CommitError(
                "Refusing to commit: staged changes include .env or .env.* files: "
                + ", ".join(staged_env_paths)
            )

        # Recompute the snapshot on the staged worktree.  Because the
        # content hash reads file bytes from disk (not the index), it is
        # stable across staging; the only way the digest can differ from
        # the reviewed snapshot is if a file's bytes, path set, or HEAD
        # actually changed between the pre-stage check and now.
        post_stage = compute_review_snapshot(project_path)
        staged_drifts: list[str] = []
        if expected_head and post_stage.get("headSha") != expected_head:
            staged_drifts.append("HEAD moved since review")
        if post_stage.get("diffHash") != expected_snapshot.get("diffHash"):
            staged_drifts.append("staged content differs from reviewed content")
        if post_stage.get("treeSha") != expected_snapshot.get("treeSha"):
            staged_drifts.append("staged tree differs from reviewed tree")
        if staged_drifts:
            raise CommitError(
                "Staged index has drifted from the reviewed PASS snapshot: "
                + "; ".join(staged_drifts)
                + ". Re-review the changes before committing."
            )

        # P1-3 final guard: the actual staged tree (captured above) must
        # equal the reviewed tree SHA.  ``post_stage['treeSha']`` was
        # computed against a *temp* index seeded from HEAD, which can
        # differ from the live index when a clean filter or concurrent
        # mutation perturbed the live index between ``git add -A`` and
        # ``compute_review_snapshot``.  Comparing the live-index tree
        # SHA directly is the strongest check that what we are about to
        # commit is exactly what was reviewed.
        expected_tree = expected_snapshot.get("treeSha")
        if expected_tree and actual_tree_sha != expected_tree:
            raise CommitError(
                "Staged tree SHA has drifted from the reviewed PASS snapshot: "
                f"expected {expected_tree[:10]}, got {actual_tree_sha[:10]}. "
                "A clean filter or concurrent index mutation may have altered "
                "staged content; re-review the changes before committing."
            )

    # Codex P1-3 round 13: build the commit from the **pinned staged
    # tree** via ``git commit-tree`` rather than ``git commit``.  This
    # removes the final TOCTOU window: ``git commit`` would re-read the
    # index, giving a clean filter or hook one more chance to mutate
    # the recorded content after our last check.  ``commit-tree``
    # records an immutable tree object as the commit's tree without
    # touching the index, and the parent is pinned to ``expected_head``
    # so an external HEAD movement between the pre-stage drift check
    # and now cannot smuggle in unreviewed history.
    #
    # ``commit-tree`` is in ``_HOOKED_COMMANDS`` so ``_run_git_text``
    # disables ``prepare-commit-msg`` hooks for this invocation.
    if expected_head:
        parent_args: list[str] = ["-p", expected_head]
    else:
        # No expected HEAD supplied: fall back to using the current HEAD
        # as the parent so the resulting commit still has history.  This
        # path is exercised by callers that did not capture a review
        # snapshot (e.g. the legacy one-shot commit).
        head_res = _run_git_text(project_path, ["rev-parse", "HEAD"])
        if head_res.returncode != 0 or not head_res.stdout.strip():
            raise CommitError(
                "Failed to resolve current HEAD for commit parent. "
                + (head_res.stderr or "").strip()
            )
        parent_args = ["-p", head_res.stdout.strip()]

    commit_tree_result = _run_git_text(
        project_path,
        ["commit-tree", actual_tree_sha, *parent_args, "-m", message],
    )
    if commit_tree_result.returncode != 0:
        raise CommitError(
            (commit_tree_result.stderr or commit_tree_result.stdout or "git commit-tree failed.").strip()
        )
    new_commit_sha = commit_tree_result.stdout.strip()
    if not new_commit_sha:
        raise CommitError("git commit-tree returned an empty commit SHA.")

    # Codex P1-3 round 13: atomic compare-and-swap ref update.  ``git
    # update-ref HEAD <new> <old>`` only succeeds when HEAD still points
    # at ``expected_head``; an external HEAD movement between the
    # pre-stage drift check and now causes the update to fail and the
    # commit (now unreferenced) is left for GC.  When no expected HEAD
    # was supplied, fall back to a non-CAS update.
    if expected_head:
        update_args = ["update-ref", "HEAD", new_commit_sha, expected_head]
    else:
        update_args = ["update-ref", "HEAD", new_commit_sha]
    update_result = _run_git_text(project_path, update_args)
    if update_result.returncode != 0:
        raise CommitError(
            "Atomic HEAD update failed; the reviewed commit was not applied. "
            + (update_result.stderr or update_result.stdout or "").strip()
        )

    # Codex P1-5 round 16: persist ``new_commit_sha`` (the immutable
    # object created by ``commit-tree``) as the controlled commit
    # identity rather than rereading HEAD.  Previously the code did
    # ``git rev-parse HEAD`` after the CAS update and recorded whatever
    # SHA Git returned; if HEAD moved externally between the CAS update
    # and the rev-parse, the task record would trust an unrelated
    # commit SHA.  The commit object ``new_commit_sha`` is immutable
    # and was created by this invocation, so it is the authoritative
    # identity regardless of subsequent ref movement.
    commit_sha = new_commit_sha
    short_result = _run_git_text(
        project_path, ["rev-parse", "--short", new_commit_sha]
    )
    if short_result.returncode == 0 and short_result.stdout.strip():
        short_sha = short_result.stdout.strip()
    else:
        short_sha = commit_sha[:10]

    # Separately detect / report subsequent branch movement so the
    # caller can surface the drift in its audit trail without
    # misattributing the controlled commit.  An external ``update-ref``
    # / ``commit`` / ``reset`` on HEAD between the CAS update above and
    # this check is informational only — the controlled commit itself
    # succeeded and ``commit_sha`` remains the object we authored.
    head_drift_sha: str | None = None
    head_check = _run_git_text(project_path, ["rev-parse", "HEAD"])
    if head_check.returncode == 0:
        head_now = head_check.stdout.strip()
        if head_now and head_now.lower() != new_commit_sha.lower():
            head_drift_sha = head_now

    return {
        "commitSha": commit_sha,
        "commitShortSha": short_sha,
        "commitMessage": message,
        "headDriftSha": head_drift_sha,
    }


def _enumerate_merge_affected_paths(
    main_path: Path,
    merge_base: str,
    merge_ref: str,
) -> list[str]:
    """Return the relative paths the merge between ``merge_base`` and ``merge_ref`` would touch.

    Uses ``git diff --name-only -z <base> <target>`` so paths containing
    non-ASCII or otherwise quoted components are emitted verbatim.  The
    path set is later checked against ``find_custom_merge_driver_paths``
    so a repository-installed merge driver cannot auto-resolve a conflict
    and absorb unreviewed content into the trunk (Codex P1-2 round 15).

    Fails closed (Codex P1-1 round 16): a non-zero exit from
    ``git diff`` raises ``GitError`` instead of returning an empty list.
    Previously the function returned ``[]`` on failure, which silently
    skipped the merge-driver validation step in ``controlled_merge_to_main``
    — the merge then proceeded even though the safety boundary could not
    be verified.  Raising forces the caller to surface the failure as a
    ``MERGE_BLOCKED`` event.
    """
    result = _run_git(
        main_path,
        ["diff", "--name-only", "-z", merge_base, merge_ref],
    )
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise GitError(
            stderr
            or f"git diff --name-only failed while enumerating merge-affected paths "
            f"(base={merge_base[:10]}, ref={merge_ref[:10]})."
        )
    return [path for path in result.stdout.split("\0") if path]


# ---------------------------------------------------------------------------
# Durable merge recovery journal (Codex P1-1 round 19).
#
# A successful controlled merge advances HEAD *before* materialising the
# merge result into the index / working tree.  If the process is
# interrupted between the forward CAS and the materialisation (power
# loss, SIGKILL, OS crash, Python traceback), HEAD is left pointing at
# the new merge commit while the index / working tree still reflect the
# pre-merge commit.  On restart the repository appears "merged" by ref
# but work-in-progress files were never updated.
#
# The recovery journal records every durable state transition required
# to either complete or reverse-CAS the merge after a crash.  Recovery
# inspects the actual ref / index / worktree state (never trusts the
# journal alone), and only acts when the recorded identities match the
# live state.  When identity cannot be proven, the journal is retained
# and an audit record is appended so an operator can reconcile manually.
# ---------------------------------------------------------------------------

MERGE_RECOVERY_VERSION = 1
# Phases:
#   "pre_cas"      — journal written, about to advance HEAD
#   "post_cas"     — HEAD advanced, about to materialise index / worktree
#   "materialised" — index / worktree synced; task metadata still pending
#   "task_persisted" / "audit_persisted" — server durability boundaries
#   "rolled_back"  — materialisation failed and HEAD was reverse-CAS'd
MERGE_RECOVERY_PHASES = (
    "pre_cas",
    "post_cas",
    "materialised",
    "task_persisted",
    "audit_persisted",
    "rolled_back",
    "rollback_task_persisted",
    "rollback_audit_persisted",
)


class MergeRecoveryJournal:
    """Durable, atomically-written journal of a single controlled merge.

    Codex P1-1 round 19.  Lives in application state storage (``.gui/
    merge_recovery/`` by default) — NOT inside ``.git`` (no hidden global
    state, no dependency on repository-internal layout) and NOT as an
    untracked file in the target worktree (so the merge cannot "lose"
    its own journal by advancing to a tree that drops the file).

    Each journal entry is a single JSON document written via an atomic
    ``tempfile + os.replace`` so a crash mid-write leaves either the old
    version or the new version on disk — never a partial document.

    The journal is passed from the server (which owns the application
    state directory) into the controlled merge service rather than
    reached via global state, so the merge service remains testable in
    isolation and the journal location is explicit at every call site.
    """

    def __init__(self, journal_dir: Path, operation_id: str):
        if not journal_path_safe(journal_dir, operation_id):
            raise ValueError(
                f"Unsafe merge-recovery journal path: {journal_dir} / {operation_id}"
            )
        self.journal_dir = Path(journal_dir)
        self.operation_id = str(operation_id)
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.journal_dir / f"{self.operation_id}.json"

    def exists(self) -> bool:
        """Return True when the journal file currently exists on disk."""
        try:
            return self.path.is_file()
        except OSError:
            return False

    def read(self) -> dict | None:
        """Return the parsed journal document, or ``None`` when absent.

        Never raises: a corrupted / partially-written journal returns
        ``None`` so the caller can treat the absence as "manual
        reconciliation required" (the journal file is left in place for
        forensic inspection).
        """
        if not self.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    def write(
        self,
        *,
        phase: str,
        task_id: str | None,
        primary_path: str,
        expected_old_head: str,
        new_merge_commit_sha: str,
        source_branch: str | None,
        target_branch: str | None,
        task_round: int | None = None,
        primary_identity: str | None = None,
        source_commit_sha: str | None = None,
        reviewed_base_sha: str | None = None,
    ) -> None:
        """Atomically write the journal entry for ``phase``.

        The journal always records every field — the phase field is the
        only thing that changes between writes — so recovery can inspect
        the full operation context regardless of where the crash
        happened.  ``tempfile + os.replace`` is used so a crash mid-write
        leaves the previous phase's document intact.
        """
        if phase not in MERGE_RECOVERY_PHASES:
            raise ValueError(f"Unknown merge-recovery phase: {phase}")
        existing = self.read() or {}
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        payload = {
            "version": MERGE_RECOVERY_VERSION,
            "operationId": self.operation_id,
            "taskId": task_id,
            "taskRound": task_round,
            "primaryPath": str(primary_path),
            "primaryIdentity": primary_identity,
            "expectedOldHead": str(expected_old_head),
            "newMergeCommitSha": str(new_merge_commit_sha),
            "sourceCommitSha": source_commit_sha,
            "reviewedBaseSha": reviewed_base_sha,
            "sourceBranch": source_branch,
            "targetBranch": target_branch,
            "phase": phase,
            "createdAt": existing.get("createdAt") or now,
            "updatedAt": now,
        }
        self._replace(payload)

    def advance(self, phase: str) -> None:
        """Atomically advance an existing journal without changing identity."""
        if phase not in MERGE_RECOVERY_PHASES:
            raise ValueError(f"Unknown merge-recovery phase: {phase}")
        payload = self.read()
        if payload is None:
            raise MergeError(
                f"Cannot advance missing or unreadable recovery journal {self.path}."
            )
        payload["phase"] = phase
        payload["updatedAt"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        self._replace(payload)

    def _replace(self, payload: dict) -> None:
        """Durably replace the journal document with ``payload``."""
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=f"{self.operation_id}.",
            suffix=".tmp",
            dir=str(self.journal_dir),
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, str(self.path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def delete(self) -> None:
        """Delete the journal file after a successful / rolled-back operation.

        Best-effort: a failure to delete (e.g. filesystem read-only)
        surfaces as an exception so the caller can record the
        inconsistency rather than silently leaving the journal behind.
        """
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise MergeError(
                f"Merge completed but the recovery journal could not be "
                f"deleted ({exc}). Manual cleanup of {self.path} is "
                f"required so the next lifecycle does not attempt "
                f"recovery on an already-completed operation."
            ) from exc


def journal_path_safe(journal_dir: Path, operation_id: str) -> bool:
    """Return True when ``operation_id`` is a safe filename component.

    The journal directory is application state, but the operation id is
    derived from caller input — validate it so a caller cannot escape
    the journal directory or overwrite an unrelated file.
    """
    if not operation_id:
        return False
    if "/" in operation_id or "\\" in operation_id:
        return False
    if operation_id in {".", ".."}:
        return False
    if not re.fullmatch(r"[A-Za-z0-9._\-]+", operation_id):
        return False
    try:
        candidate = (Path(journal_dir) / f"{operation_id}.json").resolve(strict=False)
        parent = Path(journal_dir).resolve(strict=False)
    except (OSError, ValueError):
        return False
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def _canonical_path_identity(path: Path) -> str:
    try:
        return str(path.expanduser().resolve(strict=False)).lower()
    except (OSError, ValueError):
        return str(path).lower()


def _commit_tree_sha(main_path: Path, commit_sha: str) -> str:
    result = _run_git_text(main_path, ["rev-parse", f"{commit_sha}^{{tree}}"])
    if result.returncode != 0 or not result.stdout.strip():
        raise GitError(
            (result.stderr or result.stdout or "").strip()
            or f"Unable to resolve tree for commit {commit_sha[:10]}."
        )
    return result.stdout.strip().lower()


def _repository_matches_commit(main_path: Path, commit_sha: str) -> tuple[bool, str]:
    """Prove index and worktree exactly match ``commit_sha`` without mutation."""
    try:
        expected_tree = _commit_tree_sha(main_path, commit_sha)
        index_tree = get_index_tree_sha(main_path).lower()
    except GitError as exc:
        return False, f"Failed to classify index tree: {exc}"
    if index_tree != expected_tree:
        return False, (
            f"Index tree {index_tree[:10]} does not match recorded commit tree "
            f"{expected_tree[:10]}."
        )
    diff_result = _run_git_text(main_path, ["diff", "--quiet", "--no-ext-diff"])
    if diff_result.returncode == 1:
        return False, "Working tree contains concurrent tracked edits."
    if diff_result.returncode != 0:
        return False, (
            "Unable to classify working-tree edits: "
            + (diff_result.stderr or diff_result.stdout or "").strip()
        )
    untracked_result = _run_git_text(
        main_path, ["ls-files", "--others", "--exclude-standard", "-z"]
    )
    if untracked_result.returncode != 0:
        return False, (
            "Unable to classify untracked files: "
            + (untracked_result.stderr or untracked_result.stdout or "").strip()
        )
    if untracked_result.stdout:
        return False, "Working tree contains concurrent untracked files."
    return True, ""


def _validate_recovery_identity(
    main_path: Path, journal: MergeRecoveryJournal, data: dict
) -> str | None:
    """Return a blocking reason unless every immutable operation identity matches."""
    required = {
        "version",
        "operationId",
        "taskId",
        "taskRound",
        "primaryPath",
        "primaryIdentity",
        "expectedOldHead",
        "newMergeCommitSha",
        "sourceCommitSha",
        "sourceBranch",
        "targetBranch",
        "phase",
    }
    missing = sorted(key for key in required if data.get(key) in (None, ""))
    if missing:
        return "Recovery journal is missing required fields: " + ", ".join(missing)
    if data.get("version") != MERGE_RECOVERY_VERSION:
        return f"Unsupported recovery journal version {data.get('version')!r}."
    if str(data.get("operationId")) != journal.operation_id:
        return "Journal filename and operationId do not match."
    if str(data.get("phase")) not in MERGE_RECOVERY_PHASES:
        return f"Unknown recovery phase {data.get('phase')!r}."
    if _canonical_path_identity(Path(str(data["primaryPath"]))) != _canonical_path_identity(main_path):
        return "Recorded primary path does not match the recovery target."
    live_identity = get_git_common_dir(main_path)
    if not live_identity:
        return "Unable to resolve the live repository identity."
    if _canonical_path_identity(Path(str(data["primaryIdentity"]))) != _canonical_path_identity(Path(live_identity)):
        return "Recorded repository identity does not match the live repository."
    live_branch = get_current_branch(main_path)
    if live_branch != str(data["targetBranch"]):
        return (
            f"Recorded target branch {data['targetBranch']!r} does not match "
            f"live branch {live_branch!r}."
        )
    parents = [parent.lower() for parent in get_commit_parents(
        main_path, str(data["newMergeCommitSha"])
    )]
    expected_parents = [
        str(data["expectedOldHead"]).lower(),
        str(data["sourceCommitSha"]).lower(),
    ]
    if parents != expected_parents:
        return (
            "Recorded merge commit parents do not match the immutable old/source "
            "SHAs; refusing recovery."
        )
    return None


def recover_pending_merge(
    main_path: Path,
    journal: MergeRecoveryJournal,
) -> dict | None:
    """Inspect / complete / reverse a pending merge after a crash.

    Codex P1-1 round 19.  Called under the per-task and per-resource
    locks on startup or before the next relevant Git operation.  The
    recovery inspects the actual ref / index / worktree state (never
    trusts the journal alone) and only acts when the recorded
    identities match the live state.

    Returns a dict describing the recovery outcome (``action`` ∈
    ``{"none", "completed", "rolled_back", "blocked"}``) so the caller
    can surface the result in the audit trail.  ``None`` is returned
    when no journal exists.

    Recovery rules:

    * ``phase == "pre_cas"``   — HEAD was never advanced; the operation
      is incomplete but no ref mutation happened.  Prove the old
      ref/index/worktree identity, mark ``rolled_back``, and leave final
      task/audit reconciliation to the server.
    * ``phase == "post_cas"``  — HEAD was advanced but materialisation
      did not run.  Inspect the live HEAD: when it equals
      ``newMergeCommitSha``, attempt to complete the materialisation
      (``read-tree -m -u <expectedOldHead> <newMergeCommitSha>``), verify
      the resulting state, and mark ``materialised``.  The journal remains
      until task/audit persistence.  When it does NOT match — concurrent
      user / external movement — refuse to act, retain the journal, and
      return ``blocked`` so the operator can reconcile.
    * ``phase == "materialised"`` — the operation completed successfully
      and awaits task/audit reconciliation.  Verify HEAD, index and
      worktree still equal ``newMergeCommitSha``; otherwise ``blocked``.
    * ``phase == "rolled_back"``  — the operation explicitly reverse-CAS'd
      HEAD; verify ref/index/worktree equal ``expectedOldHead`` and return
      it for task/audit reconciliation; otherwise ``blocked``.

    Any unclassifiable state (corrupted journal, index / worktree
    disagreement, ref drift from the recorded identities) leaves the
    journal in place and returns ``blocked`` with a reason the caller
    can write into the audit trail.
    """
    data = journal.read()
    if data is None:
        if journal.exists():
            return {
                "action": "blocked",
                "reason": "Recovery journal is unreadable; manual reconciliation is required.",
            }
        return None
    identity_error = _validate_recovery_identity(main_path, journal, data)
    if identity_error:
        return {
            "action": "blocked",
            "reason": identity_error + " Manual reconciliation is required.",
        }
    expected_old_head = str(data.get("expectedOldHead") or "").strip().lower()
    new_commit = str(data.get("newMergeCommitSha") or "").strip().lower()
    phase = str(data.get("phase") or "")
    source_commit = str(data.get("sourceCommitSha") or "").strip().lower()

    head_result = _run_git_text(main_path, ["rev-parse", "HEAD"])
    if head_result.returncode != 0 or not head_result.stdout.strip():
        return {
            "action": "blocked",
            "reason": (
                "Failed to resolve primary worktree HEAD during recovery. "
                + (head_result.stderr or head_result.stdout or "").strip()
            ),
        }
    live_head = head_result.stdout.strip().lower()

    if phase == "pre_cas":
        matches_old, reason = _repository_matches_commit(main_path, expected_old_head)
        if live_head != expected_old_head or not matches_old:
            return {
                "action": "blocked",
                "reason": (
                    "Recovery cannot prove the pre-CAS repository still matches "
                    f"the recorded old HEAD. {reason} Manual reconciliation is required."
                ),
                "liveHead": live_head,
            }
        journal.advance("rolled_back")
        return {
            "action": "rolled_back",
            "reason": (
                "Recovery: journal was at phase=pre_cas (HEAD was never "
                "advanced); Git state remains at the recorded old commit."
            ),
            "liveHead": live_head,
        }

    if phase == "post_cas":
        if live_head != new_commit:
            return {
                "action": "blocked",
                "reason": (
                    f"Journal expects HEAD={new_commit[:10]} after forward "
                    f"CAS, but live HEAD={live_head[:10]}. Ref was moved "
                    f"concurrently; refusing to materialise. Manual "
                    f"reconciliation is required."
                ),
                "liveHead": live_head,
            }
        matches_old, reason = _repository_matches_commit(main_path, expected_old_head)
        if not matches_old:
            return {
                "action": "blocked",
                "reason": (
                    "HEAD matches the merge commit, but index/worktree do not "
                    f"exactly match the recorded pre-merge state. {reason} "
                    "Manual reconciliation is required."
                ),
                "liveHead": live_head,
            }
        read_result = _run_git_text(
            main_path, ["read-tree", "-m", "-u", expected_old_head, new_commit]
        )
        if read_result.returncode != 0:
            still_old, old_reason = _repository_matches_commit(main_path, expected_old_head)
            if still_old:
                rollback = _run_git_text(
                    main_path, ["update-ref", "HEAD", expected_old_head, new_commit]
                )
                if rollback.returncode == 0:
                    journal.advance("rolled_back")
                    return {
                        "action": "rolled_back",
                        "reason": (
                            "Recovery materialisation failed while index/worktree "
                            "still matched the old commit; HEAD was safely reverse-CAS'd."
                        ),
                        "liveHead": expected_old_head,
                    }
            return {
                "action": "blocked",
                "reason": (
                    "Recovery: HEAD matches the merge commit but the working "
                    "tree could not be synced with the merge result. Refusing "
                    "to overwrite concurrent user edits. Manual reconciliation "
                    "is required. "
                    + (read_result.stderr or read_result.stdout or "").strip()
                    + (f" {old_reason}" if old_reason else "")
                ),
                "liveHead": live_head,
            }
        matches_new, reason = _repository_matches_commit(main_path, new_commit)
        if not matches_new:
            return {
                "action": "blocked",
                "reason": (
                    "Materialisation returned success but the resulting index/worktree "
                    f"could not be proven equal to the merge commit. {reason}"
                ),
                "liveHead": live_head,
            }
        journal.advance("materialised")
        return {
            "action": "completed",
            "reason": (
                "Recovery: HEAD was advanced and the working tree was "
                "synced with the merge result; task/audit persistence is pending."
            ),
            "liveHead": live_head,
        }

    if phase in {"materialised", "task_persisted", "audit_persisted"}:
        if live_head != new_commit:
            return {
                "action": "blocked",
                "reason": (
                    f"Journal expects HEAD={new_commit[:10]} after "
                    f"materialisation, but live HEAD={live_head[:10]}. "
                    f"Ref was moved concurrently; refusing to delete the "
                    f"journal. Manual reconciliation is required."
                ),
                "liveHead": live_head,
            }
        matches_new, reason = _repository_matches_commit(main_path, new_commit)
        if not matches_new:
            return {
                "action": "blocked",
                "reason": (
                    "Recorded merge was materialised, but live index/worktree no "
                    f"longer match it. {reason} Manual reconciliation is required."
                ),
                "liveHead": live_head,
            }
        return {
            "action": "completed",
            "reason": (
                "Recovery: Git state is fully materialised; task/audit state "
                "will be reconciled before journal deletion."
            ),
            "liveHead": live_head,
        }

    # rolled-back phases
    if live_head != expected_old_head:
        return {
            "action": "blocked",
            "reason": (
                f"Journal expects HEAD={expected_old_head[:10]} after "
                f"rollback, but live HEAD={live_head[:10]}. Ref was moved "
                f"concurrently; refusing to delete the journal. Manual "
                f"reconciliation is required."
            ),
            "liveHead": live_head,
        }
    matches_old, reason = _repository_matches_commit(main_path, expected_old_head)
    if not matches_old:
        return {
            "action": "blocked",
            "reason": (
                "HEAD matches the old commit after rollback, but index/worktree "
                f"do not. {reason} Manual reconciliation is required."
            ),
            "liveHead": live_head,
        }
    return {
        "action": "rolled_back",
        "reason": (
            "Recovery: operation had already reverse-CAS'd HEAD to the "
            "pre-merge commit; task/audit reconciliation is pending."
        ),
        "liveHead": live_head,
    }


def controlled_merge_to_main(
    main_path: Path,
    source_branch: str,
    expected_commit_sha: str | None = None,
    expected_base_sha: str | None = None,
    recovery_journal: "MergeRecoveryJournal | None" = None,
    operation_id: str | None = None,
    task_id: str | None = None,
    task_round: int | None = None,
    primary_identity: str | None = None,
) -> dict:
    """Merge ``source_branch`` into the main worktree's current branch.

    Refuses to merge when the main worktree is dirty, when the source branch
    does not exist, or when ``git merge-tree`` reports a conflict.  When
    ``expected_commit_sha`` is supplied, ``refs/heads/<source_branch>`` must
    resolve exactly to that SHA; if the branch moved externally (e.g. the
    user added more commits to it) the merge is rejected so unreviewed commits
    cannot be smuggled into the primary worktree.

    The merge command itself merges the **immutable reviewed SHA**, not the
    mutable branch name (Codex P1-1 round 8).  ``get_branch_head`` resolves
    ``refs/heads/<source_branch>`` to a SHA and verifies it matches
    ``expected_commit_sha``; if the branch advances between that verification
    and the ``git merge-tree`` invocation, the merge still operates on the
    originally-verified SHA.  ``source_branch`` is recorded only as metadata
    (merge message + ``mergeSourceBranch`` return field).

    When ``expected_base_sha`` is supplied (Codex P1-1 round 12), it must
    equal the worktree's reviewed HEAD — i.e. the parent commit the
    controlled commit landed on top of.  Before merging, the primary
    worktree's current HEAD must already have ``expected_base_sha`` as an
    ancestor.  If it does not, the worktree branch must have carried
    unreviewed commits that pre-date the task (e.g. an imported worktree
    whose branch was advanced manually before the task started); merging
    would sweep those unreviewed commits into the trunk alongside the
    reviewed one, so the merge is refused.  Callers that omit
    ``expected_base_sha`` skip this check (backwards-compatible default).

    Codex P1-2 round 13: when both ``expected_commit_sha`` and
    ``expected_base_sha`` are supplied, the merge is additionally refused
    unless ``expected_commit_sha`` has *exactly one* parent and that
    parent equals ``expected_base_sha``.  Without this check, a
    ``controlled_commit`` that ran while a merge / cherry-pick / revert
    was in progress (now rejected earlier, but historically possible)
    would produce a multi-parent commit; the existing reachability check
    on ``expected_base_sha`` would still pass (because the reviewed base
    is reachable from the multi-parent commit), and the merge would
    import every unreviewed parent's history into the trunk.  Pinning
    the parent set to ``[expected_base_sha]`` ensures only the reviewed
    linear history can be merged.

    Codex P1-1 round 19: when ``recovery_journal`` is supplied (GUI
    callers must always supply one), the journal is advanced through
    the durable state transitions of the merge so a crash between
    forward CAS and index / worktree materialisation can be recovered
    deterministically.  The journal is written *before* the forward CAS
    (``phase=pre_cas``), advanced to ``phase=post_cas`` immediately
    after the CAS succeeds, then to ``materialised`` once ref / index /
    worktree are consistent.  The server retains it through
    ``task_persisted`` and ``audit_persisted`` and deletes it only after
    every durable layer agrees.  If materialisation fails, the journal is
    advanced to ``phase=rolled_back`` after the reverse CAS so the
    recovery code on the next lifecycle understands the operation was
    unwound.  Recovery (``recover_pending_merge``) inspects actual
    ref / index / worktree state and never trusts the journal alone.

    Conflicts are surfaced by ``git merge-tree --write-tree`` BEFORE any
    ref mutation, so the repository is never left in a half-merged state
    and recovery never depends on in-place merge state.  When
    ``read-tree -m -u`` fails after the forward CAS, HEAD is reverse-CAS'd back to the
    pre-merge commit so ref / index / worktree remain in the pre-merge
    state.  The function never pushes, never deletes the worktree, never
    deletes the branch, and never runs ``git checkout``.
    """
    if not main_path.exists() or not main_path.is_dir():
        raise MergeError("Main worktree path does not exist.")
    source_branch = (source_branch or "").strip()
    if not source_branch:
        raise MergeError("Source branch is required.")
    if recovery_journal is not None:
        if not operation_id or operation_id != recovery_journal.operation_id:
            raise MergeError("Recovery journal operation identity is missing or mismatched.")
        if not task_id or task_round is None or not primary_identity:
            raise MergeError(
                "Recovery journal context must include task id, task round, and "
                "primary repository identity."
            )

    status = git_status(main_path)
    if status.strip():
        raise MergeError("Main worktree is dirty; cannot merge.")

    main_branch = get_current_branch(main_path)
    if not main_branch:
        raise MergeError("Main worktree is in detached HEAD state; cannot merge.")

    if source_branch == main_branch:
        raise MergeError("Source branch equals main branch; nothing to merge.")

    # Codex P1-3 round 18: capture the primary worktree HEAD **once** as
    # ``main_head`` and use it consistently for every HEAD-dependent
    # operation in this function:
    #
    # * the reviewed-base reachability check (``is_ancestor``);
    # * the affected-paths enumeration base when no reviewed base was
    #   supplied (legacy callers);
    # * ``git merge-tree --write-tree <main_head> <merge_ref>`` to
    #   compute the merge result tree without writing any commit object;
    # * ``git commit-tree <tree> -p <main_head> -p <merge_ref> -m`` to
    #   author the merge commit object directly;
    # * the post-CAS two-tree ``read-tree -m -u <main_head> <new_commit>``
    #   materialisation step;
    # * ``git update-ref HEAD <new> <main_head>`` as the CAS expected
    #   value so an external HEAD movement between merge-tree and the
    #   ref update fails atomically rather than silently recording an
    #   unrelated SHA.
    #
    # Previously this function resolved HEAD twice — once at the
    # reachability check and again at the merge-tree / CAS site.  An
    # external HEAD movement between the two resolutions let the
    # reachability check pass against the OLD HEAD while the merge-tree
    # and CAS used the NEW HEAD.  Because the NEW HEAD already had the
    # reviewed base as an ancestor (it was a descendant of the OLD
    # HEAD), the CAS succeeded — but the resulting merge commit had
    # the NEW HEAD as its first parent, silently importing whatever
    # unreviewed commits the external movement had added.  Capturing
    # main_head once and using it for every operation closes that
    # window: any external HEAD movement between capture and CAS now
    # causes the CAS to fail atomically with no worktree mutation.
    main_head_result = _run_git_text(main_path, ["rev-parse", "HEAD"])
    if main_head_result.returncode != 0 or not main_head_result.stdout.strip():
        raise MergeError(
            "Failed to resolve primary worktree HEAD at the start of the merge. "
            + (main_head_result.stderr or "").strip()
        )
    main_head = main_head_result.stdout.strip()

    # Codex P1-1 round 17: refuse to merge when the repo config carries
    # an unsafe merge override (``branch.<main>.mergeOptions`` or
    # ``merge.strategy``) that could silently auto-resolve conflicts.
    # ``_run_git_text`` additionally force-clears these entries via
    # ``GIT_CONFIG_*`` env vars for the merge invocation itself, but
    # surfacing the rejection up front produces a clearer error and
    # records the rejection in the audit trail.
    unsafe_config = _detect_unsafe_merge_config(main_path, main_branch)
    if unsafe_config:
        raise MergeError(unsafe_config)

    branch_head = get_branch_head(main_path, source_branch)
    if not branch_head:
        raise MergeError(f"Source branch '{source_branch}' does not exist.")

    if expected_commit_sha:
        expected = expected_commit_sha.strip().lower()
        actual = branch_head.strip().lower()
        if actual != expected:
            raise MergeError(
                f"Source branch '{source_branch}' no longer points at the reviewed "
                f"commit {expected[:10]}; refusing to merge unreviewed commits."
            )
        # Merge the immutable SHA instead of the mutable branch name so a
        # branch advance between ``get_branch_head`` and ``git merge-tree``
        # cannot pull unreviewed commits into the primary worktree.
        merge_ref = expected_commit_sha.strip()
    else:
        # Even compatibility callers use the SHA resolved above.  Recording
        # a branch name in the recovery journal would not match the immutable
        # second parent authored by commit-tree and would make crash recovery
        # unclassifiable if the branch moved later.
        merge_ref = branch_head.strip()

    if expected_base_sha:
        # Codex P1-1 round 12: the reviewed HEAD captured at artifact time
        # is the parent of the controlled commit.  If that base commit is
        # NOT already reachable from the primary worktree's HEAD, the
        # branch had unreviewed commits before the task started (e.g. an
        # imported or manually-advanced worktree branch).  Merging the
        # controlled commit would sweep those unreviewed ancestors into
        # the trunk, so refuse and surface the offending base SHA.
        #
        # Codex P1-3 round 18: the comparison uses the single immutable
        # ``main_head`` captured once at the top of the function (see
        # below).  Previously this block re-resolved HEAD with a second
        # ``git rev-parse HEAD``; an external HEAD movement between the
        # two resolutions let the reachability check pass against the
        # OLD HEAD while merge-tree / commit-tree / CAS used the NEW
        # HEAD — silently importing unreviewed commits that pre-date
        # the task into the trunk.  Using the same captured SHA for
        # every HEAD-dependent operation closes that window.
        if not is_ancestor(main_path, expected_base_sha.strip(), main_head):
            raise MergeError(
                f"Reviewed base commit {expected_base_sha.strip()[:10]} is not "
                f"reachable from the primary worktree HEAD {main_head[:10]}; "
                f"refusing to merge unreviewed commits that pre-date the task "
                f"into '{main_branch}'. Rebase the worktree branch onto the "
                f"current trunk and re-review before merging."
            )

    if expected_commit_sha and expected_base_sha:
        # Codex P1-2 round 13: the controlled commit must be a linear
        # child of the reviewed base.  ``get_commit_parents`` resolves
        # every parent (including merge commits with multiple parents),
        # and we require the parent set to be exactly ``[expected_base]``.
        # Without this check, a ``controlled_commit`` that ran while an
        # in-progress merge / cherry-pick / revert was active (now
        # rejected earlier, but historically possible) would have
        # produced a multi-parent commit; ``expected_base_sha`` would
        # still be reachable from that commit, and the merge would
        # import every unreviewed parent's history into the trunk.
        commit_sha_resolved = expected_commit_sha.strip()
        base_sha_resolved = expected_base_sha.strip()
        parents = get_commit_parents(main_path, commit_sha_resolved)
        if len(parents) != 1 or parents[0].lower() != base_sha_resolved.lower():
            raise MergeError(
                f"Reviewed commit {commit_sha_resolved[:10]} has parents "
                f"{[p[:10] for p in parents]} but must have exactly one "
                f"parent equal to the reviewed base {base_sha_resolved[:10]}; "
                f"refusing to merge a commit with unreviewed parent history "
                f"into '{main_branch}'."
            )

    # Codex P1-2 round 15: refuse to merge when any path affected by the
    # merge has an active custom merge driver configured.  A repository
    # can install ``merge.<name>.driver`` (plus a ``.gitattributes`` line)
    # that runs an arbitrary external command instead of Git's default
    # 3-way merge.  A driver that runs ``true`` / ``exit 0`` would
    # auto-succeed regardless of conflicts, so the controlled merge
    # button would absorb unreviewed content into the trunk despite the
    # "reject on conflict" safety boundary.
    #
    # The affected path set is derived from the diff between the merge
    # base (the primary worktree HEAD when ``expected_base_sha`` is not
    # supplied, otherwise the reviewed base) and the merge target
    # (``merge_ref`` — typically the reviewed commit SHA).  These are
    # the paths the merge will need to resolve, so any custom driver on
    # them is in scope.
    #
    # Codex P1-3 round 18: ``merge_base`` defaults to the single
    # captured ``main_head`` rather than re-resolving HEAD.  This keeps
    # the affected-path enumeration consistent with the merge-tree /
    # commit-tree / CAS operations that all use ``main_head``.
    merge_base = expected_base_sha.strip() if expected_base_sha else main_head
    try:
        affected_paths = _enumerate_merge_affected_paths(main_path, merge_base, merge_ref)
    except GitError as exc:
        # Codex P1-1 round 16: surface enumeration failures as a merge
        # error so the caller records a ``MERGE_BLOCKED`` event rather
        # than letting the GitError propagate as a different status code.
        raise MergeError(
            "Failed to enumerate merge-affected paths; refusing to merge "
            "without verifying custom merge driver coverage. " + str(exc)
        ) from exc
    if affected_paths:
        driver_paths = find_custom_merge_driver_paths(main_path, affected_paths)
        if driver_paths:
            raise MergeError(
                "Refusing to merge: paths affected by the merge have a "
                "custom merge driver configured, which can auto-resolve "
                "conflicts or otherwise produce unreviewed merge content. "
                "Remove the merge driver configuration (or the matching "
                ".gitattributes entries) before merging: "
                + ", ".join(driver_paths)
            )

        # Codex P1-2 round 18: refuse to merge when any affected path
        # has an active smudge / process Git filter configured.  The
        # post-CAS ``read-tree -m -u <main_head> <new_commit>`` step
        # materialises the merge result into the index and working
        # tree; smudge filters fire on checkout, so a configured smudge
        # filter would transform the materialised bytes after the merge
        # tree was reviewed.  Reject up front so the safety boundary
        # fails closed instead of allowing the filter to silently
        # transform content the user never reviewed.
        smudge_paths = find_smudge_filtered_paths(main_path, affected_paths)
        if smudge_paths:
            raise MergeError(
                "Refusing to merge: paths affected by the merge have a "
                "configured smudge / process Git filter, which can "
                "transform content during materialisation so the "
                "materialised worktree no longer matches the reviewed "
                "merge tree. Remove the filter configuration for these "
                "paths before merging: "
                + ", ".join(smudge_paths)
            )

    # Codex P1-4 round 16: refuse to merge when the repository is already
    # in an in-progress operation state.  Without this up-front check,
    # the controlled merge would either finalize the pre-existing
    # operation (e.g. complete an in-progress merge with the new merge
    # content as a parent) or silently destroy someone else's in-flight
    # work.  Checking markers now establishes a clean "no one else is
    # mid-operation" invariant before ``merge-tree`` / ``commit-tree``
    # / CAS / ``read-tree`` run.
    pre_existing_ops = get_in_progress_operations(main_path)
    if pre_existing_ops:
        raise MergeError(
            "Refusing to merge: repository has an in-progress '"
            + ", ".join(pre_existing_ops)
            + "' operation. Abort or finish it before merging reviewed changes."
        )

    # Compute the merge tree via ``git merge-tree --write-tree`` (Git
    # 2.38+).  Non-zero exit indicates either conflicts or a Git error;
    # either case blocks the merge and surfaces a ``MERGE_BLOCKED`` event
    # without leaving any in-progress state on the repository (the
    # command writes no commit object, no MERGE_HEAD, no index changes).
    merge_tree_result = _run_git_text(
        main_path,
        ["merge-tree", "--write-tree", main_head, merge_ref],
    )
    if merge_tree_result.returncode != 0:
        stderr = (merge_tree_result.stderr or merge_tree_result.stdout or "").strip()
        if not stderr:
            # ``git merge-tree`` writes conflict information to stdout
            # (one conflicted file per line) on exit code 1; surface a
            # short summary so the user knows the merge was refused due
            # to a conflict rather than a Git error.
            stdout_summary = merge_tree_result.stdout.strip()
            if stdout_summary:
                first_conflicts = stdout_summary.splitlines()[:5]
                stderr = (
                    "git merge-tree reported conflicts; refusing to merge "
                    "unreviewed conflicting content. Conflicting paths: "
                    + ", ".join(first_conflicts)
                )
            else:
                stderr = "git merge-tree failed (likely due to conflicts)."
        raise MergeError(stderr)

    # ``git merge-tree --write-tree`` emits the tree SHA on the first
    # line of stdout on success.  Trailing NUL / newline separators are
    # stripped.  An empty result is treated as a Git error.
    tree_sha = merge_tree_result.stdout.strip().splitlines()[0].strip() if merge_tree_result.stdout.strip() else ""
    if not tree_sha:
        raise MergeError(
            "git merge-tree --write-tree returned an empty tree SHA; refusing to merge."
        )

    # Create the merge commit object directly from the merge tree.
    # ``commit-tree`` records an immutable commit object whose tree is
    # the merge result and whose parents are exactly ``[main_head,
    # merge_ref]`` (in that order, so ``main_head`` is the first parent
    # — the branch tip — and ``merge_ref`` is the second parent — the
    # incoming side).  No hooks can mutate the recorded content because
    # ``commit-tree`` is in ``_HOOKED_COMMANDS``.
    commit_tree_result = _run_git_text(
        main_path,
        [
            "commit-tree",
            tree_sha,
            "-p",
            main_head,
            "-p",
            merge_ref,
            "-m",
            f"Merge branch '{source_branch}' into {main_branch}",
        ],
    )
    if commit_tree_result.returncode != 0:
        raise MergeError(
            (commit_tree_result.stderr or commit_tree_result.stdout or "git commit-tree failed.").strip()
        )
    new_commit_sha = commit_tree_result.stdout.strip()
    if not new_commit_sha:
        raise MergeError("git commit-tree returned an empty merge commit SHA.")

    # Codex P1-1 round 18: advance HEAD via atomic compare-and-swap
    # BEFORE mutating the index or working tree.  The previous flow
    # ran ``read-tree --reset -u`` first and then attempted the CAS
    # ref update; if the CAS failed (HEAD moved externally between
    # merge-tree and update-ref), the index and worktree had already
    # been overwritten with the merge tree even though HEAD still
    # pointed at the pre-merge commit.  Concurrent user edits were
    # lost and the repository was left in an inconsistent state.
    #
    # The new flow:
    #
    # 1. CAS ref update first.  ``update-ref HEAD <new> <main_head>``
    #    only succeeds when HEAD still points at the captured
    #    ``main_head``.  If HEAD moved externally, the CAS fails
    #    atomically without touching the index or working tree.
    # 2. Two-tree materialisation via ``read-tree -m -u <main_head>
    #    <new_commit>``.  Unlike ``--reset``, the two-tree form refuses
    #    to overwrite uncommitted local modifications — so a concurrent
    #    editor change to the worktree is detected and preserved rather
    #    than silently clobbered.  The two-tree form requires the index
    #    to start at ``main_head`` (verified by the initial
    #    ``git_status`` check at the top of the function); when the
    #    worktree is clean, the materialisation succeeds and produces
    #    the same final state as ``--reset`` would have.
    # 3. Atomic rollback on materialisation failure.  If the CAS
    #    succeeded but ``read-tree -m -u`` failed (e.g. concurrent
    #    local modification blocked the two-tree update), roll HEAD
    #    back to ``main_head`` via reverse CAS so the ref / index /
    #    worktree remain in the pre-merge state the user started from.
    #
    # Codex P1-1 round 19: persist a durable recovery journal through
    # every state transition so a crash between forward CAS and
    # materialisation can be recovered deterministically.  The journal
    # is written BEFORE the forward CAS (phase=pre_cas), advanced to
    # phase=post_cas immediately after the CAS succeeds, advanced to
    # phase=materialised after ``read-tree`` succeeds, and finally
    # deleted.  On rollback the journal is advanced to
    # phase=rolled_back so recovery understands the operation was
    # unwound.  See ``recover_pending_merge`` for the recovery rules.
    if recovery_journal is not None:
        recovery_journal.write(
            phase="pre_cas",
            task_id=task_id,
            primary_path=str(main_path),
            expected_old_head=main_head,
            new_merge_commit_sha=new_commit_sha,
            source_branch=source_branch,
            target_branch=main_branch,
            task_round=task_round,
            primary_identity=primary_identity,
            source_commit_sha=merge_ref,
            reviewed_base_sha=expected_base_sha,
        )
    update_result = _run_git_text(
        main_path,
        ["update-ref", "HEAD", new_commit_sha, main_head],
    )
    if update_result.returncode != 0:
        # CAS failed before any ref mutation.  The journal (if any)
        # is still at phase=pre_cas; recover_pending_merge will treat
        # that as "nothing happened" and discard it on the next
        # lifecycle.  No further journal mutation is needed here.
        raise MergeError(
            "Atomic HEAD update failed; the reviewed merge was not applied. "
            "The index and working tree were not mutated, so the "
            "repository remains at the pre-merge state. "
            + (update_result.stderr or update_result.stdout or "").strip()
        )
    if recovery_journal is not None:
        recovery_journal.advance("post_cas")

    # Materialise the merge result into the index and working tree via
    # two-tree ``read-tree``.  ``-m`` performs a two-tree merge
    # (refusing to overwrite local modifications); ``-u`` updates the
    # working tree to match.  ``main_head`` is the "old" tree,
    # ``new_commit_sha`` is the "new" tree — Git computes the diff and
    # applies it to the index and working tree without running clean
    # filters (smudge filters on checkout still apply, but those are
    # pre-checked above via ``find_smudge_filtered_paths``).
    #
    # The two-tree form preserves concurrent user edits: any path that
    # differs from ``main_head`` in the working tree but is unchanged
    # between ``main_head`` and ``new_commit_sha`` is left alone, and
    # any path that differs in both is refused rather than silently
    # overwritten.
    read_tree_result = _run_git_text(
        main_path,
        ["read-tree", "-m", "-u", main_head, new_commit_sha],
    )
    if read_tree_result.returncode != 0:
        # Atomic rollback: HEAD has already advanced to
        # ``new_commit_sha``; restore it to ``main_head`` via reverse
        # CAS so the ref matches the (unchanged) index / worktree
        # state.  The reverse CAS uses ``new_commit_sha`` as the
        # expected value, which is guaranteed to still be HEAD because
        # no other code path has run since the forward CAS above.
        rollback_result = _run_git_text(
            main_path,
            ["update-ref", "HEAD", main_head, new_commit_sha],
        )
        rollback_note = ""
        if rollback_result.returncode != 0:
            # The reverse CAS itself failed — extremely unlikely (would
            # require an external HEAD movement between the forward CAS
            # and now).  Surface both errors so the operator can
            # reconcile manually.
            rollback_note = (
                " Additionally, the rollback CAS failed: "
                + (rollback_result.stderr or rollback_result.stdout or "").strip()
                + ". The repository is in an inconsistent state (HEAD advanced "
                "to the merge commit but the index / worktree could not be "
                "synced). Manual reconciliation is required."
            )
        # Codex P1-1 round 19: advance the journal to phase=rolled_back
        # only when the reverse CAS actually succeeded.  When the
        # reverse CAS failed, leave the journal at phase=post_cas so
        # recovery observes HEAD=new_commit_sha and surfaces a blocked
        # recovery rather than silently discarding the journal.
        if recovery_journal is not None and rollback_result.returncode == 0:
            recovery_journal.advance("rolled_back")
        raise MergeError(
            "Atomic HEAD update succeeded but the working tree could not be "
            "synced with the merge result. HEAD has been rolled back to the "
            "pre-merge commit. The controlled merge was not applied; the "
            "durable journal is retained until index/worktree state and the "
            "blocked task/audit outcome are verified. "
            + (read_tree_result.stderr or read_tree_result.stdout or "").strip()
            + rollback_note
        )

    # Codex P1-1 round 19: materialisation succeeded.  Advance the
    # journal to phase=materialised first, then delete it.  Writing
    # phase=materialised before delete lets a crash between the two
    # operations be resolved as "operation had already materialised;
    # only the journal delete was interrupted" — recovery will verify
    # HEAD still equals ``new_commit_sha`` and discard the journal.
    if recovery_journal is not None:
        recovery_journal.advance("materialised")
        # The server owns task metadata and the durable audit log.  Keep
        # the journal until both have been persisted under the same
        # task -> resource lock span; deleting here would create a crash
        # window where Git says "merged" but the task still says otherwise.

    # ``new_commit_sha`` is the immutable merge commit object we just
    # authored via ``commit-tree``; record it directly rather than
    # rereading HEAD so a subsequent external HEAD movement cannot
    # misattribute the recorded merge SHA.  Compute the short SHA from
    # the immutable object so the recorded short form is also stable.
    merge_sha = new_commit_sha
    short_result = _run_git_text(
        main_path, ["rev-parse", "--short", new_commit_sha]
    )
    short_sha = short_result.stdout.strip() if short_result.returncode == 0 else merge_sha[:10]

    # Separately detect subsequent HEAD movement so the caller can
    # surface the drift in its audit trail without misattributing the
    # controlled merge.  An external ``update-ref`` / ``commit`` /
    # ``reset`` on HEAD between the CAS update above and this check is
    # informational only — the controlled merge itself succeeded and
    # ``merge_sha`` remains the object we authored.
    head_drift_sha: str | None = None
    head_check = _run_git_text(main_path, ["rev-parse", "HEAD"])
    if head_check.returncode == 0:
        head_now = head_check.stdout.strip()
        if head_now and head_now.lower() != new_commit_sha.lower():
            head_drift_sha = head_now

    return {
        "mergeCommitSha": merge_sha,
        "mergeShortSha": short_sha,
        "mergeTargetBranch": main_branch,
        "mergeSourceBranch": source_branch,
        "headDriftSha": head_drift_sha,
    }


def assert_worktree_clean_or_raise(project_path: Path) -> None:
    """Raise ``GitError`` if the worktree has uncommitted changes."""
    status = git_status(project_path)
    if status.strip():
        raise GitError("Worktree is dirty.")
