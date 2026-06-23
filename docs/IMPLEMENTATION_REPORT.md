# Implementation Report — Git Worktree Workflow + PASS-commit / Merge

Round 1 of the worktree-flow feature. Adds full worktree discovery, project
tree view, controlled worktree creation, controlled post-PASS commit, and
controlled merge-to-trunk — all without weakening the existing Claude/Codex
development loop.

## What changed

### Backend

- `gui/orchestrator/git_tools.py`
  - Added `WorktreeInfo` dataclass and `list_worktrees(path)` to enumerate
    every worktree of the same repository via `git worktree list --porcelain`.
  - Added `compute_repo_id(common_dir)` so every worktree of a repository
    resolves to the same stable `repo_<sha>` identifier.
  - Kept the module free of mutating Git commands (commit / push / merge /
    reset / clean / checkout / switch / restore) so the existing
    `test_source_does_not_execute_forbidden_git_commands` guarantee still
    holds for this module.

- `gui/orchestrator/git_workflow.py` (new)
  - Single home for the only backend code allowed to run mutating Git
    commands, all gated behind explicit user action.
  - `create_worktree(main_path, branch, target_path)` validates branch names,
    target paths, dirty state, and branch collisions before delegating to
    `git worktree add -b`.
  - `controlled_commit(project_path, message)` refuses empty worktrees, `.env`
    changes, and empty messages before running `git add -A` + `git commit`.
  - `controlled_merge_to_main(main_path, source_branch)` refuses dirty main,
    missing source branch, and conflicts; if `git merge` starts but fails,
    invokes `git merge --abort` so the main worktree is not left half-merged.
  - Never runs `git push`, `git branch -D`, `git reset`, `git clean`,
    `git worktree remove`, `git checkout`, or `git switch`.

- `gui/orchestrator/models.py`
  - Extended `Task` with `repoId`, `worktreeType`, `worktreeBranch`,
    `commitSha`, `commitShortSha`, `commitMessage`, `committedAt`, `mergedAt`,
    `mergeCommitSha`, `mergeShortSha`, `mergeTargetBranch`, `mergeSourceBranch`.
  - `from_dict` / `to_dict` updated; legacy task JSON without these fields
    keeps working.

- `gui/orchestrator/prompts.py`
  - Extended the `SAFETY_RULES` block to explicitly ban `git merge`,
    `git rebase`, `git branch -D`, `git tag -d`, `git stash drop`, and
    `git worktree remove` for the AI, and to spell out that submit / merge /
    worktree lifecycle are only performed by the GUI backend when the user
    clicks the explicit buttons.

- `gui/server.py`
  - `make_project` and `_refresh_project_metadata` now populate `repoId`.
  - `add_project` auto-discovers sibling worktrees via `list_worktrees` and
    registers them, so the project list reflects the full repository layout.
    Discovery only fires when the imported path is a real Git worktree root
    (new `_path_is_worktree_root` helper), preventing unrelated sibling
    pollution when a stray subdirectory lives inside a parent Git repo.
  - `create_task` records `repoId`, `worktreeType`, and `worktreeBranch` on
    the new task so the lifecycle is self-describing.
  - New `create_project_worktree` service + `POST /api/projects/{id}/worktrees`
    endpoint for the worktree creation flow.
  - New `commit_task_changes` service + `POST /api/tasks/{id}/commit`
    endpoint. Validates task state (PASS only, not running, not archived, not
    trashed, not already committed), delegates to `controlled_commit`, and
    records commit metadata on the task.
  - New `merge_task_to_main` service + `POST /api/tasks/{id}/merge` endpoint.
    Locates the same-`repoId` primary worktree, delegates to
    `controlled_merge_to_main`, records merge metadata on the task.
  - Exception handlers in `do_POST` now map `WorktreeCreationError` → 400 and
    `CommitError`/`MergeError` → 409 alongside the existing handlers.

### Frontend

- `gui/static/index.html`
  - Sidebar now has an "Add worktree" form that appears when a primary
    worktree is selected.
  - Task panel has a new "Git 操作" row with `commit-task-button` and
    `merge-task-button`, plus a `commit-form` popover for the commit message.
  - Inspector adds new fields: Git 仓库 / Worktree / 提交 / 合并 to
    surface the new lifecycle metadata.

- `gui/static/app.js`
  - `renderProjects` now groups projects by `repoId` into a tree view with
    primary as the root and worktrees as leaves. Projects without a `repoId`
    fall back to the flat list.
  - `loadSelectedProject` updates the new header actions (worktree form
    visibility).
  - `updateActionStates` enables the commit button only for PASS tasks that
    haven't been committed, and the merge button only for committed tasks
    that haven't been merged.
  - `renderTaskDetails` populates the new Git lifecycle inspector fields.
  - `addWorktreeFromForm`, `openCommitForm`, `confirmCommitTask`,
    `closeCommitForm`, and `mergeSelectedTask` wire up the new buttons.
  - Added `committed` and `merged` to `stageLabels`.

- `gui/static/styles.css`
  - New styles for `.add-worktree`, `.repo-group`, `.repo-group-header`,
    `.project-primary`, `.git-actions`, and `.commit-form`.

### Tests

- `tests/test_git_tools.py`
  - New tests for `compute_repo_id` stability across worktrees and for
    `list_worktrees` enumerating primary + worktree (and returning `[]`
    for non-git when mocked).
  - The existing `test_source_does_not_execute_forbidden_git_commands` is
    preserved unchanged so the original module-level safety guarantee is
    still enforced.

- `tests/test_worktree.py`
  - New tests for `repoId` in `make_project`, auto-discovery on add (both
    forward and reverse), missing-path resilience.
  - New `WorktreeCreationTests` covering success, invalid branch names,
    existing target, target inside main, dirty main, existing branch, and
    no-record-left-on-failure.
  - New `ControlledCommitTests` covering success, empty worktree rejection,
    `.env` rejection, empty message rejection, and missing path rejection.
  - New `ControlledMergeTests` covering clean success, dirty-main rejection,
    missing-branch rejection, and conflict rejection (with `MERGE_HEAD`
    absence verified so `git merge --abort` actually ran).
  - Updated `test_remove_worktree_record_does_not_delete_local_directory`
    assertion: the specific worktree's record must be gone (auto-discovery
    may keep the main repo's record).

- `tests/test_gui_server.py`
  - New tests for `commit_task_changes` covering non-PASS, running,
    archived, already-committed, success (metadata + history), and failure
    (no false-positive commit).
  - New tests for `merge_task_to_main` covering uncommitted, already-merged,
    success (metadata + history), and failure (no false-positive merge).

### Docs

- `README.md`: new "Worktree 工作流与 PASS 后一键提交/合并" section; updated
  安全边界 to spell out the mutating-command bans and the no-push / no-delete
  guarantees.
- `docs/QUICK_START.md`: new section 2.1 (auto worktree discovery) and 2.5
  (worktree creation); new section 7.5 (commit + merge buttons and their
  safety boundaries).

## Safety boundaries (unchanged or strengthened)

- The Claude/Codex prompt now bans an expanded list of mutating Git commands
  including `git merge`, `git rebase`, `git branch -D`, `git tag -d`,
  `git stash drop`, and `git worktree remove`.
- `git_tools.py` still contains zero mutating Git commands. The original
  safety test continues to pass.
- The only mutating Git operations live in `git_workflow.py` and are reachable
  only through the new `/api/projects/{id}/worktrees`, `/api/tasks/{id}/commit`,
  and `/api/tasks/{id}/merge` endpoints, which are in turn only callable from
  the explicit GUI buttons.
- `.env` protection extends to the commit step: a `.env`-touching change
  blocks both Claude completion and the explicit commit.
- Merge failures automatically `git merge --abort` so the main worktree is
  never left half-merged.
- No `git push`, no branch deletion, no worktree deletion, no automatic
  conflict resolution.

## Acceptance criteria coverage

- Importing a normal Git repo still works (existing tests + `test_non_git_project_still_works_normally`).
- Importing the main worktree auto-discovers existing worktrees (`test_add_project_auto_discovers_sibling_worktrees`).
- Importing any worktree reverse-discovers the primary and siblings (`test_add_worktree_reverse_discovers_primary`).
- All worktrees of a repo share a stable `repoId` (`test_repo_id_matches_across_primary_and_worktree`).
- Sidebar renders a primary → worktree tree (`groupProjectsByRepo` + `renderProjects`).
- Clicking a worktree binds PLAN/tasks/terminal to that worktree (existing per-project binding still applies).
- Unavailable paths surface a clear badge and don't crash (`project-unavailable` class + `available: False`).
- Worktrees can be created from the GUI (`test_create_worktree_succeeds` + `addWorktreeFromForm`).
- Creation validates branch, path, repo state (`test_create_worktree_rejects_*`).
- New worktree appears automatically in the tree (auto-discovery re-runs on add).
- Creation failures leave no project record (`test_create_worktree_no_records_left_on_failure`).
- PASS tasks show the commit button (`updateActionStates` gating).
- Non-PASS / running / archived / trashed tasks can't be committed (`test_commit_task_rejects_*`).
- Empty worktrees can't be committed (`test_commit_refuses_empty_worktree`).
- `.env` changes can't be committed (`test_commit_blocks_env_changes`).
- Successful commit records `commitSha` / `commitMessage` / `committedAt` in task JSON + history (`test_commit_task_records_commit_metadata_on_success`).
- Failed commit does not flag the task as committed (`test_commit_task_failure_does_not_mark_committed`).
- Committed tasks show the merge button (`updateActionStates` gating).
- Dirty main blocks merge (`test_merge_blocks_dirty_main`).
- Missing source branch blocks merge (`test_merge_blocks_missing_branch`).
- Conflicts block merge and `git merge --abort` runs (`test_merge_blocks_conflict_and_aborts`).
- Clean merge records `mergedAt` / `mergeTargetBranch` / `mergeCommitSha` (`test_merge_task_records_merge_metadata_on_success`).
- No auto-push, no auto-delete worktree, no auto-delete branch, no auto-resolve (enforced by `git_workflow.py` source).
- Project removal still only removes the registration (`test_remove_worktree_record_does_not_delete_local_directory`, `test_remove_worktree_record_does_not_delete_git_branch`).
- Prompts still ban AI from mutating Git ops (`prompts.py` SAFETY_RULES).
- README + QUICK_START cover the full worktree → commit → merge flow and the safety boundaries.

## Test results

```
py -B -m pytest tests/test_worktree.py tests/test_git_tools.py tests/test_gui_server.py -q
=> 124 passed

py -B -m pytest -q -p no:cacheprovider
=> 177 passed
```

All pre-existing tests continue to pass.

## Round 2 Fix: Codex P1-1 / P1-2

### P1-1 — PASS commit absorbs unreviewed post-review edits (`gui/orchestrator/git_workflow.py`)

**Issue**: `/api/tasks/{id}/commit` only checked that the task was in `PASS`
state before running `git add -A && git commit`. Any file edit, new untracked
file, or manual commit landed between the Codex PASS round and the user's
click was swept into the "approved" commit, defeating the whole point of the
review.

**Fix**: Persist the reviewed worktree snapshot at PASS time and recompute /
compare it inside `controlled_commit` before staging.

- `gui/orchestrator/git_tools.py`
  - Added `compute_review_snapshot(project_path)` that returns
    `{headSha, statusHash, diffHash}`. Mirrors exactly what Codex reviewed:
    `git rev-parse HEAD`, short `git status`, and combined `git diff` plus
    untracked-file diff (same helper used by `collect_git_artifacts`).
    Pure read-only — no mutating Git commands.
  - Added `get_branch_head(project_path, branch)` for resolving
    `refs/heads/<branch>` to a SHA. Used by the merge path's drift check.
- `gui/orchestrator/git_workflow.py`
  - `controlled_commit(project_path, message, expected_snapshot=None)` now
    accepts the reviewed snapshot and verifies `HEAD`, `statusHash`, and
    `diffHash` all match before any `git add`. Drift is reported as a
    `CommitError` listing which dimensions drifted ("HEAD moved since review",
    "git status changed since review", "diff changed since review").
  - The drift check runs *before* the "no changes" check, so a manual commit
    that clears the worktree but advances HEAD is correctly rejected as
    drift rather than misclassified as "nothing to commit".
  - Callers that omit `expected_snapshot` (legacy / direct callers) skip the
    drift check entirely — backwards-compatible default.
- `gui/orchestrator/models.py`
  - Added `reviewedRound`, `reviewedHeadSha`, `reviewedStatusHash`, and
    `reviewedDiffHash` fields to `Task`. `from_dict` / `to_dict` updated;
    legacy task JSON without these fields keeps working.
- `gui/server.py`
  - `complete_codex_task` captures `compute_review_snapshot(project_path)`
    when the review transitions to `PASS` and stores the result on the task
    (`reviewedRound`/`reviewedHeadSha`/`reviewedStatusHash`/`reviewedDiffHash`).
    Non-PASS terminal statuses (`BLOCKED`, `FAILED`) skip the snapshot.
    Failures during snapshot capture are logged as `REVIEW_SNAPSHOT_FAILED`
    in task history and do not block the PASS transition.
  - `commit_task_changes` builds `expected_snapshot` from the captured task
    fields and passes it to `controlled_commit`. A drift failure surfaces as
    a 409 with `COMMIT_BLOCKED` history (existing failure path, no new state).
    The task is never marked committed on drift.

### P1-2 — Merge fast-forwards over unreviewed branch commits (`gui/orchestrator/git_workflow.py`)

**Issue**: `/api/tasks/{id}/merge` resolved the merge by branch name only.
After the controlled commit, the user could `git commit` more changes onto
the same branch and the GUI merge button would fast-forward / merge them into
the primary worktree, bypassing the review entirely.

**Fix**: Verify the branch still points at the recorded commit SHA before
`git merge`.

- `gui/orchestrator/git_workflow.py`
  - `controlled_merge_to_main(main_path, source_branch, expected_commit_sha=None)`
    now accepts the reviewed commit SHA. After confirming the branch exists
    (replacing the inline `rev-parse --verify` with `get_branch_head`), the
    resolved SHA must match `expected_commit_sha` exactly. Any drift is
    reported as a `MergeError` ("Source branch '<name>' no longer points at
    the reviewed commit <sha>; refusing to merge unreviewed commits."). The
    main worktree is not touched.
  - Callers that omit `expected_commit_sha` skip the check —
    backwards-compatible default.
- `gui/server.py`
  - `merge_task_to_main` passes `expected_commit_sha=task.commitSha` so the
    merge button only ever merges the exact reviewed commit. If the branch
    moved, the user sees a 409 with `MERGE_BLOCKED` history and the task is
    not marked merged.

### Round 2 Files Modified

| File | Change |
|---|---|
| `gui/orchestrator/git_tools.py` | Added `compute_review_snapshot` (read-only HEAD + status hash + diff hash) and `get_branch_head` (read-only branch SHA resolver). |
| `gui/orchestrator/git_workflow.py` | `controlled_commit` accepts `expected_snapshot` and refuses drift (HEAD/status/diff) before staging; `controlled_merge_to_main` accepts `expected_commit_sha` and refuses branch movement before merging. |
| `gui/orchestrator/models.py` | `Task` gains `reviewedRound`, `reviewedHeadSha`, `reviewedStatusHash`, `reviewedDiffHash`; `from_dict`/`to_dict` updated. |
| `gui/server.py` | `complete_codex_task` captures the snapshot on PASS; `commit_task_changes` plumbs it through to `controlled_commit`; `merge_task_to_main` plumbs `task.commitSha` through to `controlled_merge_to_main`. |
| `tests/test_worktree.py` | New `ControlledCommitTests` for drift (diff / untracked / HEAD moved) and matching-snapshot success; new `ControlledMergeTests` for branch moved past reviewed commit, matching-commit success, and the no-snapshot backwards-compatible path. |
| `tests/test_gui_server.py` | New server-level tests verifying `complete_codex_task` captures the snapshot on PASS only, `commit_task_changes` forwards the snapshot to `controlled_commit`, drift failures are recorded as `COMMIT_BLOCKED`, and `merge_task_to_main` forwards `task.commitSha` to `controlled_merge_to_main`. |

### Round 2 Test Results

```
py -B -m pytest tests/test_worktree.py tests/test_git_tools.py tests/test_gui_server.py -q
=> 138 passed

py -B -m pytest -q
=> 191 passed
```

### Safety boundaries preserved

- `git_tools.py` still contains zero mutating Git commands (the original
  `test_source_does_not_execute_forbidden_git_commands` still passes).
- `git_workflow.py` is still the only module that runs `git commit`, `git merge`,
  `git worktree add`, etc., and only behind the explicit GUI buttons.
- The drift checks are pure read-only additions — they use
  `git rev-parse`, `git status`, `git diff`, `git diff HEAD`,
  `git diff HEAD --name-status`, and `git ls-files --others` only.
- No new mutating commands were introduced. No `git push`, no branch deletion,
  no worktree deletion, no automatic conflict resolution.

## Round 3 Fix: Codex P1-1 / P1-2

### P1-1 — Nested `.env` slips past commit guard via collapsed untracked directory (`gui/orchestrator/git_workflow.py`)

**Issue**: `controlled_commit` only inspected `git_status` output to detect
``.env`` changes.  When an entirely-untracked directory contains a nested
``.env`` file, ``git status --short`` may collapse the directory to a single
``?? dir/`` entry (depending on Git version / configuration), so the previous
``status_mentions_env`` guard would not see ``dir/.env`` and the controlled
commit would happily stage and commit it via ``git add -A``.

**Fix**: Replace the status-text inspection with explicit path enumeration
that does not depend on directory expansion.

- `gui/orchestrator/git_tools.py`
  - Added `enumerate_changed_paths(project_path)` which lists every path
    that ``git add -A`` would stage:
    `git diff HEAD --name-status` (tracked changes, both staged and
    unstaged, with rename source/target expanded) plus
    `git ls-files --others --exclude-standard` (untracked files,
    individually — even those nested inside untracked directories).
  - Added `has_env_changes(project_path)` as a thin wrapper that returns
    True if any enumerated path has an ``.env`` / ``.env.*`` segment.
  - `collect_git_artifacts` now calls `has_env_changes` instead of
    `status_mentions_env` for the ``.env`` early-block, so Codex review
    is also protected from the same directory-collapsing blind spot.
  - Pure read-only — `enumerate_changed_paths` only runs `git diff` and
    `git ls-files`, never any mutating command. The existing
    `test_source_does_not_execute_forbidden_git_commands` guarantee still
    holds.
- `gui/orchestrator/git_workflow.py`
  - `controlled_commit` now calls `enumerate_changed_paths` and rejects
    the commit if any path has an ``.env`` segment, listing the offending
    paths in the error message. The old `status_mentions_env` import is
    removed; `path_has_env_segment` continues to be the segment checker.

### P1-2 — Review snapshot and Codex artifacts omit staged content (`gui/orchestrator/git_tools.py`)

**Issue**: `compute_review_snapshot` and `collect_git_artifacts` hashed
only ``git diff`` (working tree vs index) plus the untracked-file diff.
Staged content (``git diff --cached``) was invisible, so if Claude or the
user ran ``git add`` before Codex review, Codex could PASS without seeing
the staged content and the controlled commit would later include it
unreviewed. The drift check would also fail to detect subsequent staged
additions.

**Fix**: Use `git diff HEAD` everywhere the previous code used `git diff`
so the reviewed content and the drift hash cover exactly what
``git add -A && git commit`` will land.

- `gui/orchestrator/git_tools.py`
  - `compute_review_snapshot` now runs `git diff HEAD` (captures both
    staged and unstaged tracked changes relative to HEAD) and folds the
    untracked diff on top. `headSha` and `statusHash` are unchanged.
  - `collect_git_artifacts` now runs `git diff HEAD --stat` and
    `git diff HEAD` so the Codex review artifact mirrors exactly what
    will be committed. The untracked-diff fold is preserved.

### Round 3 Files Modified

| File | Change |
|---|---|
| `gui/orchestrator/git_tools.py` | Added `enumerate_changed_paths` + `has_env_changes`; switched `collect_git_artifacts` and `compute_review_snapshot` from `git diff` to `git diff HEAD` so staged content is captured. |
| `gui/orchestrator/git_workflow.py` | `controlled_commit` now uses `enumerate_changed_paths` + `path_has_env_segment` instead of `status_mentions_env` so a nested ``dir/.env`` cannot slip past. |
| `tests/test_git_tools.py` | Updated existing artifact-collection mocks for the new git call sequence; added `test_env_change_in_nested_untracked_directory_blocks_diff_content`, `test_compute_review_snapshot_includes_staged_content`, and `test_enumerate_changed_paths_lists_nested_untracked_env`. |
| `tests/test_worktree.py` | Added `test_commit_blocks_nested_env_in_untracked_directory`, `test_commit_blocks_when_staged_content_drifts_after_review`, and `test_commit_blocks_when_staged_file_added_after_review` to lock in both fixes end-to-end. |
| `tests/test_system_flow.py` | Small adjacent fix: `write_review` now sleeps briefly before writing `CODEX_REVIEW.json` so the file's mtime is strictly newer than the `codex_output_started` marker. This removes a pre-existing flakiness in the staleness guard (verified by repeated runs before this round) where the two writes landed in the same filesystem mtime tick and the freshly written review was incorrectly treated as stale. |

### Round 3 Test Results

```
py -B -m pytest tests/test_worktree.py tests/test_git_tools.py tests/test_gui_server.py -q
=> 141 passed

py -B -m pytest -q
=> 197 passed
```

### Safety boundaries preserved (Round 3)

- `git_tools.py` still contains zero mutating Git commands; the new helpers
  only use `git diff`, `git diff HEAD`, `git diff HEAD --name-status`, and
  `git ls-files --others`.
- `git_workflow.py` is still the only module that runs mutating Git
  commands, and only behind the explicit GUI buttons.
- The `.env` guard now covers staged, unstaged, and untracked paths
  individually — including files nested inside untracked directories that
  `git status --short` may collapse to a single `?? dir/` entry.
- The reviewed snapshot now covers exactly what `git add -A && git commit`
  will land (staged + unstaged + untracked), so the drift check detects
  post-review staging as well as post-review edits.
- No new mutating commands. No `git push`, no branch deletion, no worktree
  deletion, no automatic conflict resolution.

## Round 4 Fix: Codex P1-1 / P1-2

### P1-1 — PASS snapshot absorbs unreviewed post-artifact edits (`gui/server.py`)

**Issue**: The reviewed snapshot was captured in `complete_codex_task` when
the user clicked "Codex completed" — *after* Codex had already reviewed the
artifacts generated in `complete_claude_task`.  If the worktree changed
between artifact collection and that click, the newer unreviewed state was
stored as the reviewed snapshot, and the commit endpoint would happily
accept it.

**Fix**: Capture and persist the reviewed snapshot at the same instant
artifacts are collected (in `complete_claude_task`).  On Codex PASS, do
NOT overwrite the stored snapshot — instead, recompute the current snapshot
and compare it against the stored one.  If it drifted, or if no snapshot
exists for the current round, block the PASS so a fresh review cycle is
required.  The commit endpoint also independently refuses to commit when
the snapshot is missing or stale for the current round.

- `gui/server.py`
  - `complete_claude_task` now calls `compute_review_snapshot(project_path)`
    immediately after `collect_git_artifacts` succeeds and persists the
    result on the task (`reviewedRound`/`reviewedHeadSha`/
    `reviewedStatusHash`/`reviewedDiffHash`).  Failures during snapshot
    capture are logged as `REVIEW_SNAPSHOT_FAILED` in task history and the
    fields are cleared; subsequent PASS/commit will then be blocked.
  - `complete_codex_task` no longer captures a snapshot on PASS.  Instead,
    the new `_verify_review_snapshot_at_pass` helper compares the current
    worktree to the stored snapshot: any drift in HEAD, status, or diff
    returns a blocking reason.  Missing / stale / unreadable snapshots
    also return a blocking reason.  When blocking, the task transitions
    `CODEX_WINDOW_STARTED → FAILED` with a `REVIEW_DRIFT_BLOCKED` history
    entry.  The check runs *before* `set_task_status(PASS)` because the
    state machine forbids transitions out of terminal statuses.
  - `commit_task_changes` now refuses to commit when the reviewed snapshot
    is missing for the current round (`reviewedRound is None`, or
    `reviewedRound != task.round`, or any hash field missing).  This
    covers both legacy PASS tasks created before this fix and tasks where
    the snapshot failed to capture at artifact time.  The drift check in
    `controlled_commit` continues to run when a snapshot is present.

### P1-2 — Snapshot silently allows large/binary untracked file drift (`gui/orchestrator/git_tools.py`)

**Issue**: `compute_review_snapshot` folded the redacted untracked diff
returned by `_untracked_files_diff` into its diff hash.  That helper skips
files larger than `MAX_UNTRACKED_FILE_BYTES`, skips binary files entirely,
and caps the total untracked diff at `MAX_UNTRACKED_DIFF_BYTES` — so
large, binary, or over-budget untracked files could change after review
without changing `statusHash`/`diffHash`, while `controlled_commit` would
still stage them via `git add -A`.

**Fix**: Hash the actual bytes of every untracked file Git would stage,
independent of the review-diff size limits.

- `gui/orchestrator/git_tools.py`
  - Added `_hash_untracked_files_bytes(project_path, status)`.  For every
    untracked path reported by `git status`, it appends the relative path,
    the file size, and a sha1 of the file's raw bytes to a running sha1
    digest.  Files that do not exist or are unreadable contribute a
    stable placeholder so a deleted or permission-changed untracked file
    still perturbs the hash.  Paths are sorted for determinism.  No
    skipping, no size limit, no binary exclusion.  Pure read-only.
  - `compute_review_snapshot` now derives `diffHash` from
    `sha1(sha1(git_diff_HEAD) + ":" + untracked_files_hash)` instead of
    hashing the bounded review diff.  The reviewed diff displayed to Codex
    still uses the bounded diff (so review output stays readable), but
    the drift hash covers every byte that `git add -A` would stage.  HEAD
    SHA and status hash are unchanged.

### Round 4 Files Modified

| File | Change |
|---|---|
| `gui/orchestrator/git_tools.py` | Added `_hash_untracked_files_bytes`; `compute_review_snapshot` now hashes actual untracked-file bytes instead of the bounded review diff so large/binary untracked files cannot drift silently. |
| `gui/server.py` | `complete_claude_task` captures the reviewed snapshot at artifact-collection time (the moment Codex starts reviewing); `complete_codex_task` verifies the snapshot on PASS via the new `_verify_review_snapshot_at_pass` helper and blocks the transition on drift/missing snapshot; `commit_task_changes` independently refuses to commit when no snapshot exists for the current round. |
| `tests/test_git_tools.py` | Added `test_compute_review_snapshot_detects_large_untracked_drift`, `test_compute_review_snapshot_detects_binary_untracked_drift`, and `test_compute_review_snapshot_stable_when_unchanged`. |
| `tests/test_worktree.py` | Added `test_commit_blocks_when_large_untracked_file_changes_after_review` and `test_commit_blocks_when_binary_untracked_file_changes_after_review`. |
| `tests/test_gui_server.py` | Replaced the PASS-time snapshot capture test with `test_claude_completion_captures_review_snapshot_at_artifact_time`; added `test_codex_pass_does_not_overwrite_snapshot_and_blocks_drift`, `test_codex_pass_allows_when_worktree_matches_snapshot`, `test_codex_pass_blocks_when_snapshot_missing_for_round`, `test_commit_task_blocks_when_snapshot_missing_for_round`, `test_commit_task_blocks_when_snapshot_round_is_stale`; updated existing commit/PASS tests to seed the snapshot fields so the new gates allow the happy path. |
| `tests/test_system_flow.py` | `patched_boundaries` now mocks `compute_review_snapshot` with a stable fake so the full HTTP flow still reaches PASS. |

### Round 4 Test Results

```
py -B -m pytest tests/test_worktree.py tests/test_git_tools.py tests/test_gui_server.py tests/test_system_flow.py -q
=> 155 passed

py -B -m pytest -q
=> 206 passed
```

### Safety boundaries preserved (Round 4)

- `git_tools.py` still contains zero mutating Git commands; the new
  helper only uses `git status` and reads untracked file bytes from disk.
- `git_workflow.py` is still the only module that runs mutating Git
  commands, and only behind the explicit GUI buttons.
- The reviewed snapshot is captured exactly when Codex's review input is
  generated, so post-artifact edits cannot be smuggled into the
  "reviewed" state.
- The drift snapshot now hashes the actual bytes of every untracked
  file Git would stage, regardless of the review-diff size/binary
  budget, so a large or binary untracked file cannot change after review
  without being detected.
- The commit endpoint independently refuses to commit when no snapshot
  exists for the current round (covers legacy PASS tasks and capture
  failures), in addition to the drift check inside `controlled_commit`.
- No new mutating commands. No `git push`, no branch deletion, no worktree
  deletion, no automatic conflict resolution.

## Round 6 Fix: Codex P1-1

### P1-1 — `collect_git_artifacts` tests still mock the old `_run_git` call sequence (`tests/test_git_tools.py`)

**Issue**: Four tests in `tests/test_git_tools.py` mocked `_run_git` with
the pre-worktree-flow call sequence and newline-delimited paths. The
current `collect_git_artifacts` performs:

1. `rev-parse --is-inside-work-tree` (via `assert_git_work_tree`)
2. `status --short --untracked-files=all` (via `git_status`)
3. `diff HEAD --name-status -z` (via `enumerate_changed_paths` → `has_env_changes`)
4. `ls-files --others --exclude-standard -z` (same call chain)
5. `diff HEAD --stat` (review artifact)
6. `diff HEAD` (review artifact)
7. `ls-files --others --exclude-standard -z` (via `_untracked_files_diff` → `_list_untracked_paths`)

`test_collect_status_and_diff_artifacts` and
`test_collect_includes_untracked_text_files_in_diff_artifact` only
supplied six responses, so the 7th `ls-files` call raised `StopIteration`.
`test_env_change_blocks_diff_content` and
`test_env_change_in_nested_untracked_directory_blocks_diff_content`
supplied the `.env` path as `.env\n` (newline-terminated), but `git ls-files -z`
emits NUL-terminated output; `path_has_env_segment` checks for an exact
`.env` segment and the trailing newline defeated the match, so
`has_env_changes` returned False and the code fell through to `diff HEAD
--stat`, which again ran out of responses. Result: 4 failures in the full
pytest run, blocking a PASS.

**Fix**: Update the four mocks to match the real call sequence and use
NUL-delimited stdout for every `-z` command, matching what Git actually
emits and what the production parsers (`_list_untracked_paths`,
`_parse_diff_name_status_z`, `path_has_env_segment`) expect.

- `tests/test_git_tools.py`
  - `test_collect_status_and_diff_artifacts`: added the 7th response
    (`ls-files --others -z` returning empty) so `_untracked_files_diff`
    does not run out of mock responses. Added a comment enumerating the
    full 7-step call sequence so future Git call-order changes are
    easier to diagnose.
  - `test_collect_includes_untracked_text_files_in_diff_artifact`: same
    7-response shape, with both `ls-files --others -z` responses now
    returning `vscode-extension/src/extension.ts\0` (NUL-terminated) so
    `_untracked_file_diff` reads the real file from disk and the
    ``.env`` segment check sees a clean path. Existing on-disk fixture
    unchanged.
  - `test_env_change_blocks_diff_content`: replaced `.env\n` with
    `.env\0` so `path_has_env_segment` matches the `.env` segment and
    `has_env_changes` raises `EnvFileChangedError` as expected. Added a
    comment explaining why the path must be NUL-terminated.
  - `test_env_change_in_nested_untracked_directory_blocks_diff_content`:
    same fix — `config/.env\n` → `config/.env\0`. The comment now also
    documents that the `?? config/` status line is what Git collapses an
    untracked directory to, which is exactly the case the
    `ls-files --others -z` enumeration exists to defeat.

### Round 6 Files Modified

| File | Change |
|---|---|
| `tests/test_git_tools.py` | Updated four `collect_git_artifacts` mocks: added the missing 7th `_run_git` response for the non-env path, and switched both `ls-files --others -z` responses (and the nested-env variant) from newline-terminated to NUL-terminated paths so the new `enumerate_changed_paths`/`_list_untracked_paths` parsers and `path_has_env_segment` segment check see clean paths. Added inline comments documenting the full 7-step call sequence. |

### Round 6 Test Results

```
py -B -m pytest tests/test_git_tools.py -q
=> 17 passed

py -B -m pytest -q
=> 206 passed
```

### Safety boundaries preserved (Round 6)

- No production code changed. Only test mocks were updated to reflect
  the real `git ls-files -z` / `git diff -z` output format and the
  current `collect_git_artifacts` call sequence.
- `git_tools.py` still contains zero mutating Git commands.
- The `.env` guard exercised by `test_env_change_blocks_diff_content`
  and `test_env_change_in_nested_untracked_directory_blocks_diff_content`
  is now mocked with realistic NUL-terminated output, so the test
  actually exercises the guard instead of accidentally bypassing it.
- No new mutating commands. No `git push`, no branch deletion, no worktree
  deletion, no automatic conflict resolution.

## Round 7 Fix: Codex P1-1

### P1-1 — `compute_review_snapshot` reads `.env` bytes / diff before env-path guard (`gui/orchestrator/git_tools.py`)

**Issue**: `compute_review_snapshot` ran `git diff HEAD` (which dumps
tracked-file content into the diff text) and `_hash_untracked_files_bytes`
(which reads every untracked file's bytes) before any `.env`-path
enumeration. Because the snapshot is recomputed by `_verify_review_snapshot_at_pass`
on Codex PASS and by `controlled_commit`'s drift check on the commit
button, any `.env` change introduced between artifact collection and
those entry points caused the backend to read / hash / diff `.env`
content before any safety guard fired. The path enumeration that
already powers `has_env_changes` was not being reused at this entry
point.

**Fix**: Make the snapshot path-safe BEFORE reading any content, and
make every entry point that calls it perform the same `.env`-path
check first.

- `gui/orchestrator/git_tools.py`
  - `compute_review_snapshot` now starts by enumerating every path
    `git add -A` would stage (`enumerate_changed_paths`) and raises
    `EnvFileChangedError` listing the offending paths when any of them
    has an `.env` segment. The guard runs BEFORE `git rev-parse HEAD`,
    `git status`, `git diff HEAD`, and `_hash_untracked_files_bytes`,
    so the backend never reads, hashes, or diffs `.env` content.
  - `_hash_untracked_files_bytes` is unchanged (it still hashes every
    untracked file's bytes) — but it is unreachable when `.env` is
    present because the new guard at the top of
    `compute_review_snapshot` short-circuits first.
- `gui/orchestrator/git_workflow.py`
  - `controlled_commit` now runs `enumerate_changed_paths` + the
    `.env`-segment check BEFORE the drift check. Previously the env
    check ran after `compute_review_snapshot`, which would have read
    `.env` content (now blocked at the snapshot helper, but the
    explicit commit-level guard produces a clearer error and does not
    depend on the caller passing `expected_snapshot`).
  - Existing test invariants preserved: `test_commit_blocks_env_changes`,
    `test_commit_blocks_nested_env_in_untracked_directory`, and the
    drift tests still behave identically because the only path-safety
    observable is the error message — the env check fires first when
    `.env` is present, otherwise the drift / empty-worktree / staging
    flow runs as before.
- `gui/server.py`
  - `_verify_review_snapshot_at_pass` now catches
    `EnvFileChangedError` separately from the generic `GitError` /
    `ApiError` branches and surfaces it as a PASS-blocking reason
    that explicitly mentions the `.env` file. The blocking reason is
    recorded in task history via the existing
    `REVIEW_DRIFT_BLOCKED` event, and the PASS is downgraded to
    `FAILED` so the user must remove the `.env` change before
    re-running the review cycle.
  - `complete_claude_task`'s existing `except GitError` handler at
    artifact-collection time already catches `EnvFileChangedError`
    (subclass) and marks the snapshot as failed, so the PASS-time
    check will block with the standard "no reviewed snapshot"
    reason. No code change required there.

### Round 7 Files Modified

| File | Change |
|---|---|
| `gui/orchestrator/git_tools.py` | `compute_review_snapshot` enumerates changed paths first and raises `EnvFileChangedError` listing the offending paths when any of them has a `.env` segment. The guard runs BEFORE any byte / diff read. |
| `gui/orchestrator/git_workflow.py` | `controlled_commit` runs `enumerate_changed_paths` + `.env` segment check BEFORE the drift check so the backend never reads `.env` bytes/diff content while computing drift, and produces a clearer error message independent of `expected_snapshot`. |
| `gui/server.py` | `_verify_review_snapshot_at_pass` catches `EnvFileChangedError` separately and surfaces a PASS-blocking reason that mentions `.env`. |
| `tests/test_git_tools.py` | New `test_compute_review_snapshot_blocks_when_untracked_env_present`, `test_compute_review_snapshot_blocks_when_tracked_env_present`, and `test_compute_review_snapshot_blocks_before_reading_env_bytes` lock in the ordering invariant. |
| `tests/test_worktree.py` | New `test_commit_env_check_runs_before_drift_check` and `test_commit_env_check_runs_before_drift_check_without_snapshot` verify the env check fires before any drift / snapshot computation. |
| `tests/test_gui_server.py` | New `test_codex_pass_blocks_when_env_file_present_at_pass_time` verifies `_verify_review_snapshot_at_pass` surfaces a `.env`-mentioning blocking reason and preserves the stored snapshot. |

### Round 7 Test Results

```
py -B -m pytest tests/test_git_tools.py tests/test_worktree.py tests/test_gui_server.py -q
=> 159 passed

py -B -m pytest -q
=> 212 passed
```

### Safety boundaries preserved (Round 7)

- `git_tools.py` still contains zero mutating Git commands. The new
  guard reuses `enumerate_changed_paths` (already audited as
  read-only) plus the existing `EnvFileChangedError` signal.
- `git_workflow.py` is still the only module that runs mutating Git
  commands, and only behind the explicit GUI buttons.
- The `.env` protection now extends across the full PASS → commit
  lifecycle: artifact collection, snapshot capture (both at artifact
  time and at PASS time), drift check inside `controlled_commit`, and
  the explicit commit-level guard. In every code path the backend
  refuses to read, hash, or diff `.env` content before raising.
- The PASS-blocking path is non-destructive: the task is downgraded
  to `FAILED` with a clear `REVIEW_DRIFT_BLOCKED` history entry;
  nothing is committed, merged, pushed, or deleted.
- No new mutating commands. No `git push`, no branch deletion, no
  worktree deletion, no automatic conflict resolution.

## Round 8 Fix: Codex P1-1 / P1-2

### P1-1 — `controlled_merge_to_main` verifies the SHA but still merges the mutable branch name (`gui/orchestrator/git_workflow.py`)

**Issue**: `get_branch_head` resolved `refs/heads/<source_branch>` to a
SHA and verified it matched `expected_commit_sha`, but the subsequent
`git merge` command still passed the mutable `source_branch` name as
its merge target.  If the branch advanced between the verification and
the merge call (e.g. the user pushed another commit, or another tool
fast-forwarded the ref), the GUI merge button would absorb the
unreviewed commits into the primary worktree despite the SHA check.

**Fix**: When `expected_commit_sha` is supplied, merge the immutable
SHA itself, not the branch name.  The branch is still recorded as
metadata in the merge message and the `mergeSourceBranch` return field,
but it is no longer the merge target.

- `gui/orchestrator/git_workflow.py`
  - `controlled_merge_to_main` now selects `merge_ref`:
    `expected_commit_sha.strip()` when a reviewed SHA was supplied,
    falling back to `source_branch` for backwards compatibility.  The
    `git merge --no-ff --no-stat -m <message> <merge_ref>` invocation
    uses `merge_ref`, so a branch advance between `get_branch_head` and
    the merge cannot pull unreviewed commits into the primary worktree.
  - `source_branch` is still used for the dirty-state check, the
    detached-HEAD check, the `source_branch == main_branch` check, the
    branch existence check (`get_branch_head`), and as the value
    recorded in the merge message and `mergeSourceBranch` return field.
    Only the merge *target* is the SHA.

### P1-2 — `controlled_commit` verifies the snapshot before `git add -A` but stages whatever is present later (`gui/orchestrator/git_workflow.py`)

**Issue**: The pre-stage drift check verified the worktree snapshot
before `git add -A` ran, but `git add -A` stages whatever is on disk
at the moment it executes.  A file edit, new untracked file, or
forbidden `.env` introduced between the pre-stage drift check and the
`git add -A` call would be swept into the "approved" commit without
detection.

**Fix**: Close the check-to-stage gap by validating the staged index
*after* staging and *before* commit.  The post-stage check
re-enumerates the paths that would be committed (catching a `.env`
added during the race) and recomputes the snapshot (catching a content
mutation during the race).

For the post-stage snapshot comparison to work without false positives,
the snapshot's `diffHash` must be **stable across staging operations**
— otherwise every successful commit would look like drift.  The
previous hash was `sha1(sha1(git diff HEAD text) + ":" +
untracked-bytes-hash)`, which is *not* stable: after `git add -A`, the
previously-untracked files appear inside the `git diff HEAD` text as
new file additions, so the hash changes even when no content drifted.

The new hash is computed from file bytes read directly from disk via
the new `_hash_changed_paths_bytes` helper, which generalizes the
previous untracked-only byte hasher to every path `git add -A` would
stage (tracked modifications, staged content, renames, deletes, and
untracked files).  Reading from disk means the digest is invariant to
whether the changes are staged or not — exactly what the post-stage
check needs.

- `gui/orchestrator/git_tools.py`
  - Added `_hash_changed_paths_bytes(project_path)` which hashes
    `(path, size, sha1(content))` for every path in
    `enumerate_changed_paths`.  Skips files that don't exist with a
    `"deleted"` marker (so renames and deletes are stable across
    staging), skips non-files with a `"not-a-regular-file"` marker,
    and skips paths outside the worktree root with an
    `"outside-worktree"` marker.  Returns the literal `"empty"`
    sentinel when no paths would be staged.
  - `compute_review_snapshot` now uses `_hash_changed_paths_bytes`
    directly for `diffHash` instead of
    `sha1(sha1(git diff HEAD text) + ":" + untracked-bytes-hash)`.
    `headSha` and `statusHash` are unchanged.  The path-safety guard
    still runs first so `.env` content is never hashed.
  - Removed the previous `_hash_untracked_files_bytes` helper — its
    only caller (`compute_review_snapshot`) now uses the generalized
    helper, and the old function is unused.
- `gui/orchestrator/git_workflow.py`
  - `controlled_commit` now runs a post-stage verification block
    between `git add -A` and `git commit`:
    1. Re-enumerate staged paths via `enumerate_changed_paths` and
       re-run the `.env` segment check.  A `.env` added between the
       pre-stage guard and `git add -A` is caught here with a
       `.env`-mentioning `CommitError` and the commit is aborted
       before `git commit` runs.
    2. Recompute `compute_review_snapshot` on the staged worktree and
       compare `headSha` + `diffHash` against `expected_snapshot`.
       Because the content hash reads file bytes from disk (not the
       index), it is stable across staging; the only way it can
       differ from the reviewed snapshot is if a file's bytes, path
       set, or HEAD actually changed between the pre-stage check and
       now.  Drift is reported as a `CommitError` listing which
       dimensions drifted, and the commit is aborted.
  - The pre-stage drift check now reports `"file content changed
    since review"` (matching the new semantics) instead of
    `"diff changed since review"`.  The post-stage drift check
    reports `"staged content differs from reviewed content"`.
  - Callers that omit `expected_snapshot` (legacy / direct callers)
    skip both the pre-stage and post-stage drift checks entirely —
    backwards-compatible default.

### Round 8 Files Modified

| File | Change |
|---|---|
| `gui/orchestrator/git_tools.py` | Replaced `_hash_untracked_files_bytes` with `_hash_changed_paths_bytes` (generalizes to every staged path, stable across staging). `compute_review_snapshot` now uses the content-byte hash directly for `diffHash` so the post-stage drift check does not produce false positives. |
| `gui/orchestrator/git_workflow.py` | `controlled_merge_to_main` merges the immutable reviewed SHA when `expected_commit_sha` is supplied (was: mutable branch name). `controlled_commit` adds a post-stage verification block that re-runs the `.env` segment check over the staged index and recomputes the snapshot to detect any mutation between the pre-stage check and `git add -A`. |
| `tests/test_git_tools.py` | Updated `test_compute_review_snapshot_blocks_before_reading_env_bytes` to patch the new `_hash_changed_paths_bytes` helper. Added `test_compute_review_snapshot_stable_across_staging` to lock in the staging-invariance invariant required by the post-stage check. |
| `tests/test_worktree.py` | Added `test_commit_blocks_when_worktree_mutated_between_snapshot_and_staging` (P1-2 regression), `test_commit_post_stage_check_blocks_env_added_during_staging` (post-stage env guard), `test_commit_succeeds_when_snapshot_matches_around_staging` (post-stage false-positive guard). Added `test_merge_command_uses_reviewed_sha_not_branch_name`, `test_merge_uses_branch_name_when_no_expected_sha`, and `test_merge_cannot_pull_commits_added_after_verification` to lock in the P1-1 SHA-pinning behavior end-to-end. |

### Round 8 Test Results

```
py -B -m pytest tests/test_git_tools.py tests/test_worktree.py -q
=> 90 passed

py -B -m pytest -q
=> 219 passed
```

### Safety boundaries preserved (Round 8)

- `git_tools.py` still contains zero mutating Git commands.  The new
  `_hash_changed_paths_bytes` helper only uses `enumerate_changed_paths`
  (already audited as read-only) and reads file bytes from disk via
  `Path.read_bytes()`.
- `git_workflow.py` is still the only module that runs mutating Git
  commands, and only behind the explicit GUI buttons.  No new mutating
  commands were introduced.
- The merge fix is read-equivalent on the happy path (merging a SHA
  produces the same result as merging the branch tip when the branch
  hasn't moved).  The only behavioral change is on the TOCTOU path:
  if the branch advances between verification and merge, the merge
  now operates on the originally-verified SHA instead of the new tip.
- The post-stage check is non-destructive on the happy path (same
  content, same hash, commit proceeds) and only blocks the commit
  when actual drift is detected.  Nothing is left half-staged: the
  index has whatever `git add -A` staged, which the user can inspect
  and reset manually if desired.
- `.env` protection is unchanged at the path-safety layer (env guard
  still runs first everywhere) and is now also re-asserted on the
  staged index post-stage so a `.env` dropped during the staging race
  cannot slip past the guard.
- No `git push`, no branch deletion, no worktree deletion, no
  automatic conflict resolution.

## Round 9 Fix: Codex P1-1 / P2-1

### P1-1 — Symlink-with-benign-name bypasses `.env` guard and follows into secret bytes (`gui/orchestrator/git_tools.py`)

**Issue**: `_hash_changed_paths_bytes` resolved every changed path
through `Path.resolve()` and then called `Path.read_bytes()` on the
result.  `resolve()` follows symlinks, so a benign-named link such as
``link.txt -> .env`` resolved to the worktree's ``.env`` and the
hasher silently hashed the secret bytes through the link.  The
path-safety guard in ``compute_review_snapshot`` (and the matching
guard inside ``controlled_commit``) only inspected the Git-reported
relative path, which was the benign ``link.txt`` — so the guard
passed and the backend proceeded to read / hash / diff ``.env``
content despite the safety rule.  The same follow-symlink behaviour
also affected the untracked-diff renderer
(``_untracked_file_diff``), which used ``Path.resolve().is_file()``
and ``read_bytes()`` to dump the destination's bytes into the
review diff.

**Fix**: Never follow symlinks when collecting review diffs or
snapshot hashes.  Hash or render the symlink target string as Git
stages it (mode 120000), and block before reading when either the
Git path OR the symlink's target string references ``.env``.

- `gui/orchestrator/git_tools.py`
  - Added `_path_or_symlink_env_violation(project_path, relative_path)`
    and `enumerate_env_violations(project_path)`.  The helper inspects
    the link itself via `lstat` + `os.readlink` (no following) and
    reports a violation when:
    (a) the relative path itself has an `.env` segment,
    (b) the link's immediate target string has an `.env` segment, or
    (c) the fully-resolved target path has an `.env` segment
        (covers symlink chains whose final hop is `.env`).
    Returns ``None`` when there is no violation; otherwise returns
    the bare path for direct violations or
    ``"<path> -> <target>"`` for symlink-target violations.
  - `has_env_changes` now delegates to `enumerate_env_violations`
    so the existing entry points (``collect_git_artifacts`` and
    ``controlled_commit``) get the symlink-aware guard without
    further plumbing.
  - `compute_review_snapshot`'s path-safety guard also uses
    `enumerate_env_violations`.  The error message now lists the
    offending paths including the symlink-target format so the user
    can see exactly what was rejected.
  - `_hash_changed_paths_bytes` no longer follows symlinks:
    - Inspects the link entry via `raw_path.lstat()` instead of
      `raw_path.resolve().lstat()`; if `lstat` reports a symlink
      (`stat.S_ISLNK`), the helper hashes the *target string* read
      via `os.readlink` — exactly the bytes Git stages for the link
      blob (mode 120000) — and does not touch the destination's
      content.
    - The `lstat` mode is now always folded into the per-path digest
      (see P2-1 below).
    - A failed `lstat` still maps to the existing ``"deleted"``
      marker so renames and deletes remain stable across staging.
    - `raw_path.resolve()` is now only called for non-symlink paths,
      preserving the worktree-containment check.
  - `_untracked_file_diff` now uses `raw_path.lstat()` to detect
    symlinks and renders the link with ``new file mode 120000`` and
    the target string as the blob content, instead of following the
    link.  The destination's bytes never reach the review diff.
    (When a symlink's target references ``.env``, ``has_env_changes``
    raises first and the renderer is never reached.)
  - Added `import os` and `import stat`.  Renamed the local `stat`
    variable in `_untracked_file_diff` to `stat_line` so the new
    `stat.S_ISLNK` reference resolves to the module.
- `gui/orchestrator/git_workflow.py`
  - `controlled_commit`'s pre-stage and post-stage env-path guards
    now call `enumerate_env_violations` instead of
    `[p for p in changed_paths if path_has_env_segment(p)]`, so a
    benign-named symlink targeting `.env` is rejected at both
    checkpoints (before staging and after staging).  The error
    message format matches `compute_review_snapshot`.
  - The unused `enumerate_changed_paths` import was removed (it is
    no longer referenced after the guards moved to
    `enumerate_env_violations`).

### P2-1 — `diffHash` omits file mode/type metadata, so a post-review chmod is committed unreviewed (`gui/orchestrator/git_tools.py`)

**Issue**: `_hash_changed_paths_bytes` only covered
``(path, size, sha1(content))``.  A file that was already modified
during review could receive a post-review ``chmod +x`` (or any other
mode/type change) while ``git status --short`` still reported the same
short line and the content hash stayed unchanged — so the
pre-stage drift check, the post-stage drift check, and the
PASS-time snapshot verification all passed, and the controlled
commit absorbed the unreviewed mode change.

**Fix**: Fold the exact staged metadata into the snapshot by
including the per-path ``lstat`` mode in every digest entry.

- `gui/orchestrator/git_tools.py`
  - `_hash_changed_paths_bytes` now emits a
    ``lstat-mode:<octal-mode>`` record for every path it visits
    (right after the path record, before any content inspection).
    Because the mode is captured via ``lstat`` rather than through
    Git's index, the value is the same on the worktree side and on
    the staged side, so the existing staging-invariance invariant
    (Round 8 P1-2 fix) is preserved: the digest does not change
    just because ``git add -A`` ran.
  - Symlink entries already include their lstat mode (which carries
    ``S_IFLNK``), so a symlink that loses or gains its link status
    also perturbs the hash.
  - Regular files now have their full Unix mode folded into the
    digest.  A post-review ``chmod`` that flips any permission bit
    (e.g. ``stat.S_IWUSR`` toggling the read-only attribute on
    Windows, or ``stat.S_IXUSR`` toggling the executable bit on
    Unix) is therefore detected as drift by both the pre-stage and
    post-stage checks in ``controlled_commit`` and by the
    PASS-time snapshot verification in ``_verify_review_snapshot_at_pass``.

### Round 9 Files Modified

| File | Change |
|---|---|
| `gui/orchestrator/git_tools.py` | Added `os`/`stat` imports. Added `_path_or_symlink_env_violation` + `enumerate_env_violations` helpers. `has_env_changes` and `compute_review_snapshot`'s path-safety guard now use `enumerate_env_violations` so a benign-named symlink targeting `.env` is rejected. `_hash_changed_paths_bytes` no longer follows symlinks (it hashes the link target string) and now folds the per-path `lstat` mode into the digest so chmod drift is detected. `_untracked_file_diff` renders untracked symlinks as `mode 120000` + target string instead of following them. Renamed the local `stat` variable to `stat_line` so the `stat` module is reachable for `S_ISLNK`. |
| `gui/orchestrator/git_workflow.py` | `controlled_commit`'s pre-stage and post-stage env guards now use `enumerate_env_violations` so a benign-named symlink targeting `.env` is rejected at both checkpoints. Removed the now-unused `enumerate_changed_paths` import. |
| `tests/test_git_tools.py` | New symlink / mode regression tests: `test_compute_review_snapshot_blocks_when_untracked_symlink_targets_env`, `test_compute_review_snapshot_blocks_when_symlink_targets_tracked_env`, `test_compute_review_snapshot_blocks_before_reading_env_through_symlink` (ordering invariant), `test_hash_changed_paths_bytes_does_not_follow_symlink`, `test_hash_changed_paths_bytes_detects_mode_drift`, `test_compute_review_snapshot_detects_mode_drift`, `test_has_env_changes_detects_symlink_target`, `test_untracked_file_diff_renders_symlink_target_without_following`. Each symlink test uses a shared `_skip_if_no_symlinks` helper so the suite still passes on platforms without symlink privileges. |
| `tests/test_worktree.py` | New end-to-end regression tests on `controlled_commit`: `test_commit_blocks_when_untracked_symlink_targets_untracked_env`, `test_commit_blocks_when_symlink_targets_tracked_env`, `test_commit_blocks_when_worktree_mode_drifts_after_review`. The chmod test verifies the commit is refused and nothing is committed, and skips cleanly when `os.chmod` does not actually change the lstat mode on the host platform. |

### Round 9 Test Results

```
py -B -m pytest tests/test_git_tools.py tests/test_worktree.py -q
=> 101 passed

py -B -m pytest -q
=> 230 passed
```

### Safety boundaries preserved (Round 9)

- `git_tools.py` still contains zero mutating Git commands.  The new
  helpers only use `enumerate_changed_paths` (already audited as
  read-only), `Path.lstat()`, `os.readlink`, `Path.read_bytes()`, and
  `Path.is_symlink()`.
- `git_workflow.py` is still the only module that runs mutating Git
  commands, and only behind the explicit GUI buttons.  No new
  mutating commands were introduced.
- The symlink fix is read-equivalent on the happy path: a regular
  file (non-symlink, non-env) produces the same digest as before
  once the lstat mode is added; the only observable change is when
  a chmod or symlink-target swap happens, which is exactly the drift
  the fix is designed to catch.
- `.env` protection now extends across the full PASS → commit
  lifecycle for the symlink-bypass vector: artifact collection,
  snapshot capture (artifact time + PASS time), drift check inside
  `controlled_commit` (pre-stage and post-stage), and the explicit
  commit-level guard.  In every code path the backend refuses to
  read, hash, or diff `.env` content through a benign-named symlink
  before raising.
- The PASS-blocking path is non-destructive: the task is downgraded
  to `FAILED` with a clear `REVIEW_DRIFT_BLOCKED` history entry;
  nothing is committed, merged, pushed, or deleted.
- No `git push`, no branch deletion, no worktree deletion, no
  automatic conflict resolution.

## Round 10 Fix: Codex P1-1 / P1-2

### P1-1 — Controlled commit runs repository hooks that can stage `.env` after validation (`gui/orchestrator/git_workflow.py`)

**Issue**: `controlled_commit` validated the worktree state and the
staged index, then invoked `git commit -m <message>` with repository
hooks enabled.  A pre-commit / prepare-commit-msg / commit-msg hook
can stage additional files (including `.env`), rewrite the commit
message, or otherwise mutate the working tree AFTER the post-stage
validation has run and BEFORE the commit object is created.  The
resulting commit would carry unreviewed content even though the GUI
had marked the validated state as approved.

**Fix**: Disable ALL hooks for the `git commit` invocation by setting
`core.hooksPath` to an empty directory via the `GIT_CONFIG_*` env vars
on the subprocess.  Add `--no-verify` as defense-in-depth.

- `gui/orchestrator/git_workflow.py`
  - Added `_empty_hooks_dir()`: lazily creates an empty directory via
    `tempfile.mkdtemp(prefix="cdl-no-hooks-")` and reuses it for every
    subsequent no-hooks invocation.  Best-effort cleanup at process
    exit via `atexit`.  One directory per process; cheap and stable.
  - Added `_HOOKED_COMMANDS = {"commit", "merge"}` constant — the
    mutating commands whose hooks (pre-commit / commit-msg /
    post-commit / prepare-commit-msg / post-merge) can mutate the
    worktree or commit object.
  - `_run_git_text(project_path, args)` now checks `args[0]`:
    - When `args[0]` is in `_HOOKED_COMMANDS`, runs `git` with the
      subprocess environment extended to include
      `GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=core.hooksPath`,
      `GIT_CONFIG_VALUE_0=<empty-dir>`.  This is the most defensive
      way to ensure NO hook runs (Git treats a non-existent or empty
      `core.hooksPath` as "no hooks configured").
    - Otherwise delegates to `_run_git` unchanged so existing tests
      that patch `_run_git_text` and inspect `args[0]` continue to
      work without modification.
  - `controlled_commit` now passes `["commit", "--no-verify", "-m",
    message]` so the intent is obvious to readers and the operation
    is protected even if a future refactor routes through a different
    runner that doesn't apply the env override.
- Hooks are disabled via env vars instead of `-c core.hooksPath=...`
  CLI args deliberately: the existing `test_merge_command_uses_reviewed_sha_not_branch_name`
  and friends inspect `args[0]` / `args[-1]` to detect merge / commit
  invocations and to assert the merge target is the reviewed SHA.  If
  the override were prepended to args, those tests would silently
  break.  Env vars are transparent to the args list.

### P1-2 — Controlled merge runs repository hooks that can mutate the merge result (`gui/orchestrator/git_workflow.py`)

**Issue**: `controlled_merge_to_main` verified the main worktree was
clean and the source branch pointed at the reviewed SHA, then invoked
`git merge --no-ff ...` with repository hooks enabled.  A
prepare-commit-msg or post-merge hook can run AFTER those checks and
mutate the merge commit message, the merge result, or the working
tree, bypassing the guarantee that only the reviewed branch commit is
merged by the explicit GUI button.

**Fix**: Disable ALL hooks for the `git merge` (and `git merge --abort`)
invocations using the same env-override mechanism as the commit path.

- `gui/orchestrator/git_workflow.py`
  - The same `_run_git_text` change covers both commit AND merge: any
    call whose `args[0]` is `"merge"` now runs with the
    `core.hooksPath=<empty-dir>` env override.  No additional code
    change is required in `controlled_merge_to_main`; the change is
    transparent at the call site.

### Round 10 Files Modified

| File | Change |
|---|---|
| `gui/orchestrator/git_workflow.py` | Added `atexit` / `os` / `shutil` / `tempfile` imports. Added `_EMPTY_HOOKS_DIR`, `_HOOKED_COMMANDS = {"commit", "merge"}`, and `_empty_hooks_dir()` helper. Reworked `_run_git_text` to inject `core.hooksPath=<empty-dir>` env override for commit / merge invocations (other commands delegate to `_run_git` unchanged). Added `--no-verify` to the `git commit` call as defense-in-depth. |
| `tests/test_worktree.py` | New `HookDisablingTests` class with 7 tests: `test_commit_does_not_run_pre_commit_hook` (regression), `test_commit_does_not_run_commit_msg_hook` (regression), `test_commit_does_not_run_post_commit_hook` (regression), `test_commit_hook_recorded_when_hooks_not_disabled` (sanity check that hook installation actually works for plain `git commit`), `test_merge_does_not_run_post_merge_hook` (regression), `test_commit_runs_through_no_hooks_env_override` (locks in env-override implementation), `test_merge_runs_through_no_hooks_env_override` (locks in env-override implementation). |

### Round 10 Test Results

```
py -B -m pytest tests/test_worktree.py -q
=> 79 passed

py -B -m pytest -q
=> 237 passed
```

### Safety boundaries preserved (Round 10)

- `git_tools.py` is unchanged and still contains zero mutating Git
  commands; the original `test_source_does_not_execute_forbidden_git_commands`
  still passes.
- `git_workflow.py` is still the only module that runs mutating Git
  commands, and only behind the explicit GUI buttons.  No new mutating
  commands were introduced.
- Hook disabling is precise: only `commit` and `merge` invocations
  are affected.  `status`, `diff`, `add`, `rev-parse`, `worktree add`,
  `merge --abort` (when called as a separate command — though `merge`
  is still in `_HOOKED_COMMANDS` so abort is also covered), etc. run
  with the user's normal hook configuration so the test setup remains
  meaningful.
- The empty `core.hooksPath` directory is process-local and created
  lazily; it never touches the repository's `.git/hooks` directory.
  User-installed hooks are not modified, only ignored for the
  controlled commit / merge invocations.
- `--no-verify` on the commit is a redundant safeguard: even if a
  future change accidentally removed the env override, the explicit
  flag would still skip pre-commit and commit-msg hooks (though not
  post-commit — which is why the env override is the primary defense).
- No `git push`, no branch deletion, no worktree deletion, no
  automatic conflict resolution.  The `git merge --abort` cleanup
  path is unchanged and still runs whenever `git merge` fails.

## Round 11 Fix: Codex P1-1 / P1-2

### P1-1 — Reviewed snapshot captured after artifacts already written (`gui/server.py`)

**Issue**: `complete_claude_task` called `collect_git_artifacts` first
(writing the `git_status_round_N.txt` / `git_diff_stat_round_N.txt` /
`git_diff_round_N.diff` files Codex reviews) and *then* invoked
`compute_review_snapshot` to record the reviewed baseline.  If the
worktree changed between those two calls, the snapshot reflected a
newer state than the artifacts.  Codex then reviewed the older
artifacts, PASS-time verification compared against the newer snapshot,
and the one-click commit could include edits Codex never saw.

**Fix**: Capture the reviewed snapshot BEFORE artifact collection
starts and again AFTER `collect_git_artifacts` returns.  If the two
snapshots differ, the worktree mutated mid-collection and the recorded
snapshot cannot be trusted to match what Codex reviewed — discard it
so a later PASS is blocked instead of approving unreviewed content.

- `gui/server.py`
  - `complete_claude_task` now captures `pre_snapshot` via
    `compute_review_snapshot(project_path)` BEFORE
    `collect_git_artifacts` runs.  An `EnvFileChangedError` at this
    point transitions the task to `FAILED` with the existing
    `git_collection_failed` stage (matching the previous behaviour for
    env failures surfaced from `collect_git_artifacts`).  A generic
    `GitError` records `REVIEW_SNAPSHOT_FAILED` history and clears
    the snapshot fields, then proceeds with artifact collection.
  - After `collect_git_artifacts` returns successfully, a second
    `compute_review_snapshot` call captures `post_snapshot`.  Any
    `GitError` / `EnvFileChangedError` records
    `REVIEW_SNAPSHOT_FAILED` history.  When the post-snapshot is
    present but differs from the pre-snapshot, the snapshot is also
    discarded and `REVIEW_SNAPSHOT_FAILED` history lists the drift
    reason.  When the two snapshots match, the post-snapshot's
    `headSha` / `statusHash` / `diffHash` are persisted on the task
    (`reviewedRound` / `reviewedHeadSha` / `reviewedStatusHash` /
    `reviewedDiffHash`).
  - On the discard path, all four snapshot fields are cleared so the
    PASS-time verifier `_verify_review_snapshot_at_pass` and the
    commit endpoint's snapshot-presence gate both block.

### P1-2 — `_hash_changed_paths_bytes` collapses submodule paths to a constant marker (`gui/orchestrator/git_tools.py`)

**Issue**: `_hash_changed_paths_bytes` visited every changed path and
emitted a constant `"not-a-regular-file"` marker for anything that
was not a regular file or symlink.  Git submodules are gitlink entries
(mode `160000`) backed by a directory on disk, so when a tracked
submodule's pointer changed from reviewed commit B to unreviewed
commit C the directory's lstat mode, the `git status --short` text,
and the staged set all stayed the same — only the submodule's working
tree HEAD flipped.  The drift check would then pass and the one-click
commit could absorb the unreviewed submodule pointer.

**Fix**: Detect gitlink/submodule paths via `git ls-files --stage -z`
and include the submodule's working-tree HEAD SHA in the per-path
digest.  Submodule SHAs are resolved via `git -C <submodule>
rev-parse HEAD` so the actual commit `git add -A` would record is
what perturbs the hash.

- `gui/orchestrator/git_tools.py`
  - Added module-level constant `GITLINK_MODE = "160000"` (the Git
    object mode `git ls-files --stage` emits for gitlink entries).
  - Added `get_tracked_path_modes(project_path)` which runs
    `git ls-files --stage -z` and returns `{relative_path:
    git_mode_string}`.  NUL-terminated so paths with quoted
    components are emitted verbatim.  Read-only.
  - Added `get_submodule_head(project_path, relative_path)` which
    runs `git -C <submodule> rev-parse HEAD` and returns the
    working-tree HEAD SHA (or `None` when the submodule checkout
    directory is missing / uninitialized / Git refuses to resolve
    HEAD).  Read-only.
  - `_hash_changed_paths_bytes` now consults
    `get_tracked_path_modes` once per call.  For each path whose
    tracked mode is `GITLINK_MODE`, the hasher emits
    `gitlink-mode:160000` followed by either
    `submodule-sha:<HEAD-SHA>` (when the submodule is initialized)
    or `submodule-uninitialized` (when it is not).  No content bytes
    are read for gitlink paths — the submodule's commit SHA is the
    security-relevant signal.  Regular files, symlinks, deleted
    paths, etc. continue to flow through the existing branches.

### Round 11 Files Modified

| File | Change |
|---|---|
| `gui/server.py` | `complete_claude_task` captures the reviewed snapshot before AND after `collect_git_artifacts`. When the two snapshots differ (or either capture fails), the snapshot fields are cleared and `REVIEW_SNAPSHOT_FAILED` history is recorded so subsequent PASS / commit gates block. Pre-artifact `EnvFileChangedError` transitions the task to `FAILED` with the existing `git_collection_failed` stage. |
| `gui/orchestrator/git_tools.py` | Added `GITLINK_MODE` constant, `get_tracked_path_modes(project_path)` (parses `git ls-files --stage -z`), and `get_submodule_head(project_path, relative_path)` (resolves submodule working HEAD via `git -C <sub> rev-parse HEAD`). `_hash_changed_paths_bytes` now emits `gitlink-mode:160000` + `submodule-sha:<SHA>` (or `submodule-uninitialized`) for gitlink paths instead of the constant `"not-a-regular-file"` marker. |
| `tests/test_git_tools.py` | New submodule / gitlink regression tests: `test_get_tracked_path_modes_returns_gitlink_for_submodule`, `test_hash_changed_paths_bytes_detects_submodule_pointer_drift` (P1-2 regression — both snapshots have `sub` in the changed-paths set, only the submodule working HEAD differs), `test_compute_review_snapshot_detects_submodule_pointer_drift` (end-to-end variant), `test_get_submodule_head_returns_none_for_missing_dir`. New `_skip_if_no_submodules` and `_make_repo_with_submodule` / `_advance_submodule` helpers build a real parent + submodule + source-repo fixture so the regression coverage does not depend on external infrastructure. |
| `tests/test_gui_server.py` | Updated `test_claude_completion_captures_review_snapshot_at_artifact_time` to assert `compute_review_snapshot` is called exactly twice (before + after artifact collection). New `test_claude_completion_discards_snapshot_when_worktree_drifts_during_collection` (P1-1 regression — second snapshot returns a different value; the snapshot fields are cleared and `REVIEW_SNAPSHOT_FAILED` history is recorded). New `test_claude_completion_blocks_when_env_present_at_pre_artifact_snapshot` (pre-artifact `EnvFileChangedError` transitions the task to `FAILED`). |

### Round 11 Test Results

```
py -B -m pytest tests/test_git_tools.py tests/test_gui_server.py tests/test_worktree.py -q
=> 184 passed, 5 skipped

py -B -m pytest -q
=> 243 passed, 4 warnings in 61.30s
```

(The 5 skipped tests are all pre-existing symlink tests that require
platform symlink privileges; the new submodule tests run for real on
this host.)

### Safety boundaries preserved (Round 11)

- `git_tools.py` still contains zero mutating Git commands.  The new
  helpers only use `git ls-files --stage -z` and `git -C <submodule>
  rev-parse HEAD` — both read-only.  The original
  `test_source_does_not_execute_forbidden_git_commands` still passes.
- `git_workflow.py` is unchanged in this round; the only module that
  runs mutating Git commands remains the controlled-commit /
  controlled-merge module behind the explicit GUI buttons.
- The P1-1 fix is non-destructive on the happy path: when the
  worktree is quiescent, `pre_snapshot == post_snapshot`, the
  snapshot is persisted exactly as before, and downstream PASS /
  commit / merge behaviour is unchanged.  The only observable
  change is on the TOCTOU path — when the worktree mutates between
  the two captures, the snapshot is discarded and PASS is blocked
  instead of approving unreviewed content.
- The P1-2 fix is read-equivalent on the happy path for non-submodule
  paths: regular files, symlinks, deleted files, etc. continue to
  hash exactly as before.  The only observable change is for
  gitlink-mode paths, which now contribute their submodule HEAD SHA
  to the digest instead of a constant.  Submodules whose working
  tree matches their index pointer continue to behave correctly
  because they don't appear in `enumerate_changed_paths` and the
  hasher never visits them.
- The `.env` protection is unchanged: `compute_review_snapshot`
  still refuses to read `.env` bytes / diff content before any
  hashing runs, and the pre/post-snapshot capture in
  `complete_claude_task` surfaces `EnvFileChangedError` from either
  capture as a hard failure rather than silently swallowing it.
- No new mutating commands.  No `git push`, no branch deletion, no
  worktree deletion, no automatic conflict resolution.

## Round 12 Fix: Codex P1-1

### P1-1 — Merge guard ignores unreviewed commits that pre-date the task (`gui/orchestrator/git_workflow.py`)

**Issue**: `controlled_merge_to_main` verified the source branch still
pointed at the controlled commit SHA (`expected_commit_sha`) and then
merged that SHA.  But if the worktree branch already had commits
*before* the task started — e.g. an imported worktree whose branch was
advanced manually, or a worktree created on top of an existing branch
— Codex only reviewed the uncommitted diff against that branch HEAD.
The controlled commit then landed on top of the pre-task commits, and
`git merge --no-ff <reviewed_sha>` swept the entire chain (pre-task
commits + the reviewed commit) into the trunk.  The unreviewed
pre-task commits bypassed the review entirely.

The reviewed HEAD (`reviewedHeadSha`) is already captured at artifact
time (Round 4 P1-1) and recorded on the task; the merge path simply
wasn't consulting it.

**Fix**: Pass `task.reviewedHeadSha` into the merge as
`expected_base_sha` and refuse to merge unless that base commit is
already reachable from the primary worktree's HEAD.  When the base is
reachable, merging the reviewed SHA only introduces the single
reviewed commit (the one whose parent is the reviewed base).  When
it isn't, the branch had unreviewed pre-task commits and the merge
is rejected with a clear reason.

- `gui/orchestrator/git_tools.py`
  - Added `is_ancestor(project_path, ancestor, descendant)` which
    wraps `git merge-base --is-ancestor`.  Returns ``True`` when
    ``ancestor`` is reachable from ``descendant``'s history.
    Read-only — no mutating Git commands.
- `gui/orchestrator/git_workflow.py`
  - `controlled_merge_to_main(main_path, source_branch,
    expected_commit_sha=None, expected_base_sha=None)` accepts a new
    optional ``expected_base_sha``.  After the existing
    ``expected_commit_sha`` verification (and still before the merge
    invocation), the primary worktree's HEAD is resolved via
    ``rev-parse``.  When ``expected_base_sha`` is provided and not
    reachable from that HEAD, the merge is refused with a
    ``MergeError`` listing both SHAs and instructing the user to
    rebase the worktree branch onto the current trunk and re-review
    before merging.  Callers that omit ``expected_base_sha`` skip the
    new check (backwards-compatible default).
  - ``is_ancestor`` is imported alongside the existing read-only
    helpers.
- `gui/server.py`
  - `merge_task_to_main` passes `expected_base_sha=task.reviewedHeadSha`
    in addition to `expected_commit_sha=task.commitSha`.  Legacy
    tasks where `reviewedHeadSha` is ``None`` skip the new check
    (the parameter is optional inside `controlled_merge_to_main`).

### Round 12 Files Modified

| File | Change |
|---|---|
| `gui/orchestrator/git_tools.py` | Added `is_ancestor(project_path, ancestor, descendant)` wrapping `git merge-base --is-ancestor` (read-only). |
| `gui/orchestrator/git_workflow.py` | `controlled_merge_to_main` accepts `expected_base_sha` and refuses the merge when the reviewed base SHA is not reachable from the primary worktree HEAD. Imported `is_ancestor`. |
| `gui/server.py` | `merge_task_to_main` plumbs `task.reviewedHeadSha` through to `controlled_merge_to_main` as `expected_base_sha`. |
| `tests/test_worktree.py` | New `ControlledMergeTests`: `test_merge_blocks_when_branch_has_unreviewed_pre_task_commits` (regression — a branch with a pre-task commit is refused; neither the pre-task commit nor the reviewed commit lands on main), `test_merge_succeeds_when_reviewed_base_is_reachable_from_main` (happy path), `test_merge_without_expected_base_sha_skips_reachability_check` (backwards compatibility). |
| `tests/test_gui_server.py` | Updated `test_merge_task_passes_reviewed_commit_sha_to_controlled_merge` so the fake merge handler accepts `expected_base_sha`, the task seeds `reviewedHeadSha`, and the test asserts both SHAs are plumbed through. |

### Round 12 Test Results

```
py -B -m pytest tests/test_worktree.py tests/test_git_tools.py tests/test_gui_server.py -q
=> 188 passed, 5 skipped

py -B -m pytest -q
=> 246 passed
```

(The 5 skipped tests are all pre-existing symlink tests that require
platform symlink privileges; the new reachability tests run for real
on this host.)

### Safety boundaries preserved (Round 12)

- `git_tools.py` still contains zero mutating Git commands.  The new
  helper only uses `git merge-base --is-ancestor`, which is read-only.
  The original `test_source_does_not_execute_forbidden_git_commands`
  still passes.
- `git_workflow.py` is unchanged in its set of mutating commands: the
  only mutating invocations are still `git add -A`, `git commit`, and
  `git merge` (plus `git merge --abort` on failure).  No new mutating
  command was introduced.
- The new reachability check is read-equivalent on the happy path:
  when the reviewed base is reachable (the normal case where the
  worktree was branched from main and the task did not add commits
  before the review), the merge proceeds exactly as before.  The only
  observable change is on the unsafe path — when the branch carried
  unreviewed pre-task commits, the merge is now refused instead of
  sweeping them into the trunk.
- The PASS-blocking path is non-destructive: the merge is rejected
  before `git merge` runs, so the primary worktree is untouched and
  the user can rebase / re-review manually.
- No `git push`, no branch deletion, no worktree deletion, no
  automatic conflict resolution.

## Round 13

### Codex Findings Addressed

Three P1 findings, all concentrated in the controlled-commit /
controlled-merge safety boundary:

- **P1-1** — `gui/orchestrator/git_tools.py:425` (changed-path
  enumeration) silently ignored failures from `git diff --name-status`
  and `git ls-files`.  Since this enumeration gates `.env` protection
  and snapshot hashing, a transient failure could omit protected /
  unreviewed paths while later artifact or commit commands succeed.
- **P1-2** — `gui/orchestrator/git_workflow.py:448` (merge guard)
  only verified the reviewed HEAD was reachable from main; it never
  checked that it was the controlled commit's *sole* parent.  If
  `git commit` finalized an existing merge state, the resulting commit
  could have additional unreviewed parents yet still pass this check
  and import their history into main.
- **P1-3** — `gui/orchestrator/git_workflow.py:331` (post-stage
  verification) hashed working-tree bytes rather than the staged
  index.  A clean filter or concurrent index mutation could make the
  index differ from reviewed filesystem content, after which
  `git commit` would record the unreviewed staged blobs.

### Round 13 Fix: fail closed on enumeration failures (P1-1)

**Why**: The `.env` guard, the snapshot hash, and the artifact
collection all depend on `enumerate_changed_paths` / `_list_untracked_paths` /
`get_tracked_path_modes`.  A transient Git failure that returned an
empty list would let later artifact or commit commands succeed while
silently dropping protected or unreviewed paths from the guard.

**Fix**: All three helpers now raise `GitError` on any underlying Git
failure.  Callers that previously tolerated an empty result on failure
now propagate the error so the safety boundary fails closed.

- `gui/orchestrator/git_tools.py`
  - `_list_untracked_paths`: raises `GitError` when `git ls-files
    --others` fails (was: `return []`).
  - `enumerate_changed_paths`: raises `GitError` when *either*
    `git diff HEAD --name-status -z` or `git ls-files --others -z`
    fails (was: silently dropped whatever the failing call returned).
  - `get_tracked_path_modes`: raises `GitError` when `git ls-files
    --stage` fails (was: `return {}`, which collapsed every submodule
    entry to a constant `"not-a-regular-file"` marker and hid
    submodule pointer drift).

### Round 13 Fix: reject in-progress repo operations and verify sole parent (P1-2)

**Why**: `controlled_commit` ran `git add -A` then `git commit`
unconditionally.  If the repository was already in an in-progress
merge / cherry-pick / revert / rebase / bisect state, `git commit`
would finalize that operation; the resulting commit could carry
unreviewed parents yet pass every HEAD / content drift check (the
existing snapshot helpers only verify HEAD and content, not the
parent set).  The merge then swept those unreviewed parents into the
trunk because the reachability check on the reviewed base still
passed.

**Fix**: Reject before staging, then verify before merging.

- `gui/orchestrator/git_tools.py`
  - New `get_in_progress_operations(project_path)` detects the marker
    files / directories Git creates inside the git dir when an
    operation is mid-flight: `MERGE_HEAD`, `CHERRY_PICK_HEAD`,
    `REVERT_HEAD`, `REBASE_HEAD`, `BISECT_LOG`, and the `sequencer`
    directory.  Marker paths from `git rev-parse --git-path` may be
    relative; they are resolved against `project_path` before the
    existence check.  Read-only.
  - New `get_commit_parents(project_path, commit_sha)` returns the
    parent SHAs of a commit via `git show -s --format=%P`.  Read-only.
- `gui/orchestrator/git_workflow.py`
  - `controlled_commit` calls `get_in_progress_operations` first and
    refuses with `CommitError` when any in-progress marker is present.
  - `controlled_merge_to_main` now verifies, when both
    `expected_commit_sha` and `expected_base_sha` are supplied, that
    the reviewed commit has *exactly one* parent and that parent
    equals `expected_base_sha`.  The reachability check on
    `expected_base_sha` would otherwise still pass on a multi-parent
    commit (because the reviewed base IS reachable from it), allowing
    every unreviewed parent's history to be imported into the trunk.
- `gui/server.py` already plumbs both SHAs through, so no caller
  changes were required for the parent-verification check.

### Round 13 Fix: pin staged tree + commit-tree + atomic CAS (P1-3)

**Why**: The post-stage verification recomputed
`compute_review_snapshot` on the staged worktree.  Because the
snapshot's `diffHash` reads file bytes from disk (not the index), it
was stable across staging; a clean filter configured between artifact
collection and commit time, or any concurrent index mutation, would
perturb the index *without* changing `diffHash`.  `git commit` would
then record the filtered / mutated staged blobs even though the
reviewed content was unchanged on disk.

**Fix**: Capture the immutable staged tree SHA after `git add -A`,
compare against the reviewed tree SHA, and create the commit from that
immutable tree via `commit-tree` + atomic `update-ref` so no clean
filter or hook can perturb what is recorded.

- `gui/orchestrator/git_tools.py`
  - New `compute_worktree_tree_sha(project_path)` computes the tree
    SHA `git add -A && git write-tree` would produce, using a
    *temporary* index file (via `GIT_INDEX_FILE`) so the main index
    is never disturbed.  The temp index is created in a unique file
    inside the git dir and removed at the end.  Read-only with
    respect to the actual repo state.
  - New `get_index_tree_sha(project_path)` captures the live index's
    tree SHA via `git write-tree`.  Fails closed on any Git failure.
  - `compute_review_snapshot` now also returns `treeSha` (from
    `compute_worktree_tree_sha`).  Unlike `diffHash` (which hashes
    worktree bytes), `treeSha` reflects what the *index* would
    contain after staging — so a clean filter or concurrent index
    mutation perturbs `treeSha` even when the worktree bytes are
    unchanged.
- `gui/orchestrator/models.py`
  - Added `reviewedTreeSha: str | None` to the `Task` dataclass,
    persisted through `from_dict` / `to_dict`.
- `gui/orchestrator/git_workflow.py`
  - `_HOOKED_COMMANDS` now includes `"commit-tree"` so hooks are
    disabled for that invocation too.
  - `controlled_commit`:
    - Adds the reviewed `treeSha` to the pre-stage drift comparison
      and to the post-stage drift comparison.
    - Immediately after `git add -A`, captures
      `actual_tree_sha = get_index_tree_sha(project_path)` and
      compares against the reviewed `treeSha`.  When they differ,
      the commit is refused (clean filter or concurrent index
      mutation detected).
    - Builds the commit via `git commit-tree <tree> -p <expected_head>
      -m <message>` rather than `git commit`, so the recorded content
      is the pinned immutable tree — not whatever a clean filter or
      hook would have staged at commit time.
    - Advances HEAD via `git update-ref HEAD <new> <expected_head>`,
      a Git-native compare-and-swap that fails when an external HEAD
      movement happened between the pre-stage drift check and the
      ref update.  When no `expected_head` was supplied (legacy
      callers), falls back to a non-CAS `update-ref`.
- `gui/server.py`
  - `complete_claude_task` persists `reviewedTreeSha` from the
    post-collection snapshot, alongside the existing reviewed fields.
    Drift during snapshot capture (pre vs. post artifact collection)
    now also clears `reviewedTreeSha`.
  - `_verify_review_snapshot_at_pass` includes `treeSha` in the drift
    comparison, and refuses PASS when `reviewedTreeSha` is missing.
  - `commit_task_changes` requires `reviewedTreeSha` to be present
    (otherwise refuses with `COMMIT_BLOCKED`) and passes it through
    to `controlled_commit` as part of `expected_snapshot`.

### Round 13 Files Modified

| File | Change |
|---|---|
| `gui/orchestrator/git_tools.py` | `_list_untracked_paths`, `enumerate_changed_paths`, `get_tracked_path_modes` now raise `GitError` on Git failure (P1-1). Added `get_commit_parents`, `get_in_progress_operations`, `compute_worktree_tree_sha`, `get_index_tree_sha`. `compute_review_snapshot` now also returns `treeSha` (P1-3). |
| `gui/orchestrator/git_workflow.py` | `_HOOKED_COMMANDS` adds `"commit-tree"`. `controlled_commit` rejects in-progress operations (P1-2), pins the staged tree SHA, drift-checks against `treeSha`, builds the commit via `commit-tree`, and atomically advances HEAD via `update-ref` compare-and-swap (P1-3). `controlled_merge_to_main` verifies the reviewed commit has exactly one parent equal to the reviewed base (P1-2). |
| `gui/orchestrator/models.py` | Added `reviewedTreeSha` to `Task` with full `from_dict` / `to_dict` support. |
| `gui/server.py` | `complete_claude_task` persists `reviewedTreeSha`; snapshot-discard paths now clear it. `_verify_review_snapshot_at_pass` adds `treeSha` drift detection. `commit_task_changes` requires `reviewedTreeSha` and passes it through to `controlled_commit`. |
| `tests/test_git_tools.py` | New `FailClosedEnumerationTests`: `_list_untracked_paths`, `enumerate_changed_paths` (both command failures), `get_tracked_path_modes`, and the snapshot propagation path each verify the helper raises `GitError` on Git failure. |
| `tests/test_worktree.py` | New P1-2 in-progress rejection tests (merge + cherry-pick markers). New P1-2 parent-verification tests (multi-parent rejection, single-parent-but-wrong-base rejection, happy path). New P1-3 tests: clean filter drift, `commit-tree` + `update-ref` CAS contract, atomic HEAD update rejecting concurrent HEAD movement. |
| `tests/test_gui_server.py` | Updated existing snapshot / PASS tests to seed `reviewedTreeSha` and assert `treeSha` propagation. Mocked `compute_review_snapshot` returns now include `treeSha`. |
| `tests/test_system_flow.py` | `fake_compute_review_snapshot` now returns `treeSha` alongside the existing fields so the synthetic PASS flow still verifies. |

### Round 13 Test Results

```
py -B -m pytest -q
=> 242 passed
```

### Safety boundaries preserved (Round 13)

- `git_tools.py` still contains zero mutating Git commands.  The new
  helpers (`get_commit_parents`, `get_in_progress_operations`,
  `compute_worktree_tree_sha`, `get_index_tree_sha`) use only
  `git show`, `git rev-parse`, `git read-tree`, `git add -A`,
  `git write-tree`.  The `read-tree` / `add -A` / `write-tree` calls
  operate against a *temp* index (via `GIT_INDEX_FILE`); the live
  repo index is never touched.  The original
  `test_source_does_not_execute_forbidden_git_commands` still passes.
- `git_workflow.py` adds two new mutating invocations —
  `git commit-tree` and `git update-ref` — both behind the existing
  hook-disabling boundary.  `commit-tree` is added to
  `_HOOKED_COMMANDS` so `prepare-commit-msg` hooks cannot run.  No
  destructive commands (`git push`, `git reset`, `git branch -D`,
  `git worktree remove`, `git clean`, etc.) were introduced.  The
  old `git commit` invocation is gone — replaced by the safer
  `commit-tree` + `update-ref` pair — so the mutating surface area
  is unchanged in size.
- The new checks are no-ops on the happy path: when the repository is
  quiescent (no in-progress operations), the reviewed tree matches
  the live staged tree, and HEAD has not moved, the commit lands
  exactly as before.  The only observable change is on the unsafe
  path — clean filter attacks, in-progress finalization, and parent
  smuggling are now rejected instead of absorbed.
- All PASS-blocking paths are non-destructive: the commit / merge is
  rejected before any mutating Git command runs, so the worktree is
  untouched and the user can resolve the underlying issue manually.

## Round 14

### Codex Findings Addressed

Two P1 findings and one P2 finding, all in the controlled-commit /
controlled-merge safety boundary:

- **P1-1** — `gui/orchestrator/git_tools.py` `compute_review_snapshot`
  silently returned `headSha=None` when `git rev-parse HEAD` failed
  (returncode != 0) or returned an empty SHA (e.g. unborn branch in a
  freshly-init'd repo).  The server's PASS / commit gates did not
  require `reviewedHeadSha`, so the HEAD drift check, the CAS ref
  update (which uses `expected_head`), and the merge-base
  reachability check would all silently no-op — allowing unreviewed
  history to slip into the trunk.
- **P1-2** — `gui/orchestrator/git_tools.py`
  `compute_worktree_tree_sha` ran `git add -A` against a temp index,
  which applies any configured clean/process filter on the changed
  paths.  Codex artifacts are built from raw worktree bytes (`git
  diff HEAD` + `_untracked_file_diff` reading file content directly
  from disk), so a deterministic clean filter could transform
  content during staging in a way Codex never reviewed.  The
  snapshot's `treeSha` would also reflect the filtered tree, so the
  drift check would pass while review and commit diverged.
- **P2-1** — `gui/server.py` `commit_task_changes` only caught
  `CommitError` (and `merge_task_to_main` only caught `MergeError`).
  The new `GitError` raised by snapshot / path enumeration /
  `write-tree` therefore bypassed the `COMMIT_BLOCKED` /
  `MERGE_BLOCKED` history records and returned a different status
  code through the generic handler.

### Round 14 Fix: HEAD resolution must succeed and reviewedHeadSha is required (P1-1)

**Why**: `compute_review_snapshot` quietly returned `headSha=None`
when `git rev-parse HEAD` failed or yielded an empty SHA.  The server
gates (`_verify_review_snapshot_at_pass`, `commit_task_changes`)
treated a missing `reviewedHeadSha` as a soft skip rather than a hard
refusal, so the HEAD drift check, the `git update-ref HEAD <new>
<expected>` CAS, and the `git merge-base --is-ancestor` reachability
check all became no-ops.  The result: unreviewed history could land
on the trunk.

**Fix**: Fail closed at the source, then require the field at every
entry point.

- `gui/orchestrator/git_tools.py` `compute_review_snapshot`:
  - When `git rev-parse HEAD` returns a non-zero exit code, raises
    `GitError` with the underlying stderr (or a fallback message).
  - When `git rev-parse HEAD` succeeds but the stdout is empty (or
    whitespace-only), raises `GitError("git rev-parse HEAD returned
    an empty SHA.")`.
- `gui/server.py`:
  - `_verify_review_snapshot_at_pass` now treats a missing
    `reviewedHeadSha` as a hard refusal (`REVIEW_DRIFT_BLOCKED`)
    alongside the existing reviewedRound / statusHash / diffHash /
    treeSha requirements.
  - `commit_task_changes` likewise requires `reviewedHeadSha` before
    invoking `controlled_commit`; otherwise records
    `COMMIT_BLOCKED` history and returns 409.

### Round 14 Fix: reject clean/process Git filters on changed paths (P1-2)

**Why**: `compute_worktree_tree_sha` builds its temp index with
`git add -A`, which runs any configured clean or process filter on
each staged path.  Codex review artifacts use raw worktree bytes
(`git diff HEAD` plus `_untracked_file_diff` reading content
directly from disk), so they show the *unfiltered* content.  A
deterministic clean filter can therefore transform content in a way
Codex never saw, and because the snapshot's `treeSha` is also
derived from the filtered temp index, the post-stage drift check
would pass even though review and commit diverge.

**Fix**: Reject the snapshot outright when any changed path has an
active clean/process filter configured.  This is more defensive
than diffing the temp index against the reviewed artifact set: it
guarantees that the bytes Codex reviewed and the bytes the commit
records are byte-identical.

- `gui/orchestrator/git_tools.py`:
  - New `find_clean_filtered_paths(project_path)`:
    1. Enumerates changed paths via `enumerate_changed_paths`
       (already raised `GitError` on failure as of Round 13).
    2. Runs `git check-attr filter -- <changed paths>` to discover
       which paths have a non-`unspecified` / non-`unset` filter
       attribute (typically declared via `.gitattributes`).
    3. For each distinct filter name, runs `git config --get
       filter.<name>.clean` and `git config --get
       filter.<name>.process` to verify an active driver exists.
       Attribute declarations without a matching driver are
       no-ops (Git applies identity) and are intentionally not
       reported.
    4. Returns the ordered subset of changed paths whose filter
       attribute maps to an active driver, so the error message
       lists actionable paths.
  - `compute_review_snapshot` calls `find_clean_filtered_paths`
    before computing `treeSha` and raises `GitError` listing the
    offending paths when any are present.

### Round 14 Fix: catch GitError in commit/merge services (P2-1)

**Why**: Round 13 hardened `git_tools.py` helpers to raise `GitError`
on transient Git failures (path enumeration, `write-tree`, temp
index operations).  The Round 13 server plumbing, however, only
caught `CommitError` in `commit_task_changes` and `MergeError` in
`merge_task_to_main`.  A `GitError` from the snapshot recomputation
inside `controlled_commit` / `controlled_merge_to_main` therefore
bypassed the `COMMIT_BLOCKED` / `MERGE_BLOCKED` history records and
surfaced through the generic exception handler with a different
status code, leaving the task in an inconsistent state.

**Fix**: Catch `GitError` alongside the explicit safety exception
class in both services, record the blocked history row, and convert
to a consistent 409 `ApiError`.

- `gui/server.py`:
  - `commit_task_changes`: `except (CommitError, GitError) as exc`
    appends a `COMMIT_BLOCKED` history entry and returns 409.
  - `merge_task_to_main`: `except (MergeError, GitError) as exc`
    appends a `MERGE_BLOCKED` history entry and returns 409.

### Round 14 Files Modified

| File | Change |
|---|---|
| `gui/orchestrator/git_tools.py` | `compute_review_snapshot` raises `GitError` when `git rev-parse HEAD` fails or returns an empty SHA (P1-1). Adds `find_clean_filtered_paths(project_path)` that detects active clean/process filters via `git check-attr` + `git config --get`, and `compute_review_snapshot` calls it before computing `treeSha` to reject snapshots where filtered staging could diverge from reviewed bytes (P1-2). |
| `gui/server.py` | `_verify_review_snapshot_at_pass` now treats missing `reviewedHeadSha` as `REVIEW_DRIFT_BLOCKED` (P1-1). `commit_task_changes` requires `reviewedHeadSha` to be present (otherwise `COMMIT_BLOCKED`) (P1-1). `commit_task_changes` catches `(CommitError, GitError)` and records `COMMIT_BLOCKED` history (P2-1). `merge_task_to_main` catches `(MergeError, GitError)` and records `MERGE_BLOCKED` history (P2-1). |
| `tests/test_git_tools.py` | New `test_compute_review_snapshot_raises_when_head_resolution_fails` (mocks `git rev-parse HEAD` returning rc=128). New `test_compute_review_snapshot_raises_when_head_sha_is_empty` (mocks `git rev-parse HEAD` returning empty stdout). New `test_compute_review_snapshot_blocks_when_clean_filter_configured`, `test_compute_review_snapshot_blocks_when_process_filter_configured`, `test_compute_review_snapshot_allows_when_filter_attribute_without_driver` (P1-2 coverage at the snapshot boundary). New `test_find_clean_filtered_paths_detects_active_clean_filter`, `test_find_clean_filtered_paths_returns_empty_when_no_changes`, `test_find_clean_filtered_paths_returns_empty_when_filter_unspecified`, `test_find_clean_filtered_paths_ignores_attribute_without_driver` (unit coverage for the new helper). |
| `tests/test_gui_server.py` | New `test_commit_task_blocks_when_reviewed_head_sha_missing` (leaves `reviewedHeadSha=None`, expects 409 + `COMMIT_BLOCKED`). New `test_commit_task_records_git_error_as_blocked_history` (mocks `controlled_commit` to raise `GitError`, expects 409 + `COMMIT_BLOCKED` with the underlying message). New `test_merge_task_records_git_error_as_blocked_history` (mocks `controlled_merge_to_main` to raise `GitError`, expects 409 + `MERGE_BLOCKED`). New `test_codex_pass_blocks_when_reviewed_head_sha_missing` (leaves `reviewedHeadSha=None` at PASS, expects FAILED + `REVIEW_DRIFT_BLOCKED`). |
| `tests/test_worktree.py` | Updated `test_commit_blocks_when_clean_filter_perturbs_staged_tree` to reflect the Round 14 tightening: the safety boundary now refuses at snapshot time (raising `GitError`) rather than at commit time (raising `CommitError` with "drift"). Test accepts either exception class and either "drift" or "filter" in the message; the safety guarantee (commit does not land) is unchanged. |

### Round 14 Test Results

```
py -3 -B -m pytest -q
=> 255 passed
```

### Safety boundaries preserved (Round 14)

- `git_tools.py` still contains zero mutating Git commands.  The new
  `find_clean_filtered_paths` helper uses only `git check-attr` and
  `git config --get`, both of which are read-only.  The original
  `test_source_does_not_execute_forbidden_git_commands` still passes.
- No new mutating commands were added anywhere; the Round 14 change
  set is purely defensive (more `raise GitError`, narrower catch
  blocks).
- The clean-filter rejection runs *before* any mutating Git command
  and before any file content is read into the snapshot.  Affected
  paths are listed in the error message so the user can act on them
  directly.
- The HEAD resolution check runs *after* the clean-filter check and
  *before* the existing `git status` / `_hash_changed_paths_bytes` /
  `compute_worktree_tree_sha` calls, so a HEAD failure no longer
  produces a partial snapshot that callers might cache.
- The `(CommitError, GitError)` / `(MergeError, GitError)` catch
  blocks in `commit_task_changes` / `merge_task_to_main` are
  non-destructive: the commit / merge was already rejected by the
  time the exception propagated, so the worktree is untouched and
  the user can resolve the underlying Git issue manually.
- The happy path is unchanged: when HEAD resolves cleanly, no clean
  filter is configured, and Git plumbing succeeds, the snapshot /
  commit / merge flow behaves exactly as in Round 13.  The only
  observable change is on the unsafe path — HEAD-resolution
  failures, clean-filter divergence, and transient Git failures
  during commit / merge are now rejected consistently instead of
  silently absorbed.

## Round 15

### Codex Findings Addressed

Four P1 findings across the controlled-commit / controlled-merge /
worktree-creation safety boundary:

- **P1-1** — `gui/orchestrator/git_tools.py`
  `find_clean_filtered_paths` ran `git check-attr filter --` *without*
  `-z` and decoded each line via a `unicode_escape` approximation.
  Non-ASCII paths (e.g. `dir-é/secret.txt`) arrived C-quoted with
  octal escapes that the approximation mis-decoded, so a clean filter
  configured on such a path silently escaped detection.  The Round 14
  `compute_review_snapshot` rejection boundary therefore degraded
  on repos with non-ASCII paths, allowing unreviewed filtered content
  into the trunk.
- **P1-2** — `gui/orchestrator/git_workflow.py`
  `controlled_merge_to_main` disabled hooks but never checked for
  custom merge drivers (`merge.<name>.driver` paired with a
  `.gitattributes` `merge=` declaration).  A driver that runs `true`
  or `exit 0` auto-succeeds regardless of conflicts, letting the
  controlled merge button absorb unreviewed content into the trunk
  even when the reviewed tree would have conflicted.
- **P1-3** — `gui/orchestrator/git_workflow.py` `_HOOKED_COMMANDS`
  omitted `worktree`, so `git worktree add` ran the repository's
  `post-checkout` hook in the freshly-created worktree.  A malicious
  post-checkout hook could drop an extra `.env` (or any other file)
  into the new worktree AFTER the safety boundary checks passed but
  BEFORE the user starts editing — smuggling unreviewed content past
  the worktree-creation gate.
- **P1-4** — `gui/server.py` `commit_task_changes` /
  `merge_task_to_main` lacked per-task serialisation.  With
  `ThreadingHTTPServer`, duplicate concurrent POSTs to
  `/api/tasks/{id}/commit` or `/api/tasks/{id}/merge` could both
  load the task in its pre-COMMITTED / pre-MERGED state, both proceed
  to invoke the mutating Git operation, and the loser of the race
  then saved its stale `Task` object — overwriting the winner's
  `COMMITTED` / `MERGED` metadata.

### Round 15 Fix: parse `check-attr -z` NUL triples verbatim (P1-1)

**Why**: The default `git check-attr` output wraps any path with a
non-ASCII byte in double quotes with octal escapes (e.g.
`"dir-\303\251/secret.txt"`).  `find_clean_filtered_paths` decoded
each line with a `unicode_escape` approximation that mis-handled
multi-byte UTF-8 sequences (UTF-8 is not Latin-1), so the decoded
path no longer matched the file on disk and the corresponding clean
filter escaped detection.  The Round 14 snapshot rejection boundary
then degraded silently on repos with non-ASCII paths.

**Fix**: Pass `-z` and parse NUL-separated triples strictly.

- `gui/orchestrator/git_tools.py` `find_clean_filtered_paths`:
  - Runs `git check-attr -z filter -- <changed paths>`.  The `-z`
    flag disables C-quoting so paths containing non-ASCII bytes
    (and spaces, and other special characters) are emitted as their
    raw UTF-8 bytes.  `_run_git` already decodes with
    `encoding="utf-8"`, so the NUL-separated output is directly
    parseable as a Python string.
  - Parses the output as a sequence of `<path>\0<attribute>\0<info>\0`
    triples via a strict NUL-token walk.  Triples whose attribute
    field is not exactly `filter` are skipped defensively (guards
    against Git version skew that emits extra records).
  - The previous `unicode_escape` decoder and the line-splitting
    helper are removed entirely.

### Round 15 Fix: refuse to merge when a custom merge driver covers an affected path (P1-2)

**Why**: A repository can install `merge.<name>.driver` plus a
`.gitattributes` `merge=<name>` rule that runs an arbitrary
external command instead of Git's default 3-way merge.  A driver
that runs `true` / `exit 0` auto-succeeds regardless of conflicts,
so the controlled merge button would absorb unreviewed content into
the trunk even when a manual merge would have conflicted and the
safety boundary would have rejected it.

**Fix**: Reject the merge before `git merge` runs.

- `gui/orchestrator/git_tools.py`:
  - New `find_custom_merge_driver_paths(project_path,
    candidate_paths)`:
    1. Runs `git check-attr -z merge -- <candidate paths>` to
       discover which paths have a non-`unspecified` /
       non-`unset` `merge` attribute (typically declared via
       `.gitattributes`).
    2. For each distinct merge-driver name, runs `git config --get
       merge.<name>.driver` to verify an active driver exists.
       An attribute without a matching config is a no-op (Git
       falls back to its default 3-way merge) and is intentionally
       not reported.
    3. Returns the ordered subset of candidate paths whose merge
       attribute maps to an active driver.
- `gui/orchestrator/git_workflow.py`:
  - New `_enumerate_merge_affected_paths(main_path, merge_base,
    merge_ref)` returns the relative paths the merge would touch
    via `git diff --name-only -z <base> <target>`.
  - `controlled_merge_to_main` invokes the helpers AFTER the
    parent-history check (so the verified reviewed base is used
    as the diff base) and BEFORE the `git merge` call.  If any
    affected path has an active custom merge driver, raises
    `MergeError` with the offending paths so the user sees an
    actionable list.

### Round 15 Fix: disable hooks for `git worktree add` (P1-3)

**Why**: `git worktree add` triggers the repository's
`post-checkout` hook in the freshly-created worktree.  Without
hook disabling, a malicious post-checkout hook could drop an extra
file (e.g. a `.env`) into the new worktree AFTER the worktree
safety checks (dirty main, branch-name validation, target-path
validation) passed but BEFORE the user starts editing — smuggling
unreviewed content past the worktree-creation gate.

**Fix**: Include `worktree` in the hook-disabled command set.

- `gui/orchestrator/git_workflow.py`:
  - `_HOOKED_COMMANDS` now includes `worktree` alongside `commit`,
    `merge`, and `commit-tree`.  The `_run_git_text` /
    `_run_git` env-override path (`GIT_CONFIG_COUNT=1`,
    `GIT_CONFIG_KEY_0=core.hooksPath`, `GIT_CONFIG_VALUE_0=<empty
    dir>`) is now applied to the `git worktree add` invocation
    issued by `create_worktree`.

### Round 15 Fix: serialise per-task commit / merge operations (P1-4)

**Why**: `gui/server.py` serves HTTP via `ThreadingHTTPServer`, so
each request runs on its own thread.  Without per-task
serialisation, duplicate concurrent POSTs to
`/api/tasks/{id}/commit` could both observe the task in its
pre-COMMITTED state, both pass validation, both call
`controlled_commit`, and then race on `task_store.save`.  The
loser's save (carrying a stale `Task` object without the winner's
`commitSha`) would overwrite the winner's `COMMITTED` metadata,
making the task appear uncommitted even though HEAD has actually
advanced.  The merge endpoint had the same race.

**Fix**: Wrap the full `load → validate → mutate Git → save` span
in a per-task `RLock`.

- `gui/server.py`:
  - New `_TASK_LOCKS: dict[str, threading.RLock]` registry plus a
    `_TASK_LOCKS_GUARD` lock for the registry itself.
  - New helper `_task_operation_lock(task_id)` returns the per-task
    `RLock`, creating one on first use.  `RLock` is used so the
    same thread can re-acquire (defensive against future nested
    calls).
  - `commit_task_changes` is split into outer (acquires the lock)
    + `_commit_task_changes_locked` (inner body that reloads the
    task inside the lock, validates state, runs the controlled
    commit, saves, and emits audit history).
  - `merge_task_to_main` is split the same way into outer +
    `_merge_task_to_main_locked`.

### Round 15 Files Modified

| File | Change |
|---|---|
| `gui/orchestrator/git_tools.py` | `find_clean_filtered_paths` now runs `git check-attr -z filter` and parses NUL-separated triples strictly (P1-1). The previous `unicode_escape` line-based decoder is removed. New `find_custom_merge_driver_paths(project_path, candidate_paths)` detects active `merge.<name>.driver` configs on candidate paths via `git check-attr -z merge` + `git config --get merge.<name>.driver` (P1-2). |
| `gui/orchestrator/git_workflow.py` | `_HOOKED_COMMANDS` now includes `worktree` so `create_worktree` disables the `post-checkout` hook during `git worktree add` (P1-3). New `_enumerate_merge_affected_paths(main_path, merge_base, merge_ref)` via `git diff --name-only -z`. `controlled_merge_to_main` invokes `_enumerate_merge_affected_paths` + `find_custom_merge_driver_paths` between the parent-history check and the `git merge` call, raising `MergeError` if any affected path has an active custom merge driver (P1-2). |
| `gui/server.py` | New `_TASK_LOCKS` registry + `_task_operation_lock(task_id)` helper using `threading.RLock` (P1-4). `commit_task_changes` and `merge_task_to_main` are split into outer lock-acquiring wrappers + inner `_locked` bodies that reload the task inside the lock so concurrent duplicate requests short-circuit on the post-save validation rather than racing the save (P1-4). |
| `tests/test_git_tools.py` | New `test_find_clean_filtered_paths_detects_non_ascii_path` and `test_find_clean_filtered_paths_detects_path_with_spaces` (P1-1 regression coverage). Six new tests for `find_custom_merge_driver_paths`: `test_find_custom_merge_driver_paths_detects_active_driver`, `test_find_custom_merge_driver_paths_detects_non_ascii_path`, `test_find_custom_merge_driver_paths_returns_empty_when_unspecified`, `test_find_custom_merge_driver_paths_ignores_attribute_without_driver`, `test_find_custom_merge_driver_paths_returns_empty_for_empty_input`, `test_find_custom_merge_driver_paths_fails_closed_on_git_error` (P1-2 unit coverage). |
| `tests/test_worktree.py` | New `test_worktree_add_does_not_run_post_checkout_hook` and `test_worktree_add_runs_through_no_hooks_env_override` in `HookDisablingTests` (P1-3 regression coverage). New `test_merge_blocks_when_custom_merge_driver_configured` and `test_merge_succeeds_when_merge_attribute_without_driver` in `ControlledMergeTests` (P1-2 end-to-end coverage at the merge boundary). |
| `tests/test_gui_server.py` | New `ConcurrentOperationTests` class with three tests (P1-4 regression coverage): `test_concurrent_commit_requests_are_serialised` (two concurrent commits — winner commits, loser 409s on "already committed", exactly one `controlled_commit` call), `test_concurrent_merge_requests_are_serialised` (same pattern for merge), `test_concurrent_commit_requests_for_different_tasks_do_not_block` (sanity check that two different task IDs run concurrently — guards against the lock becoming a single global lock). |

### Round 15 Test Results

```
py -B -m pytest -q
=> 286 passed, 1 skipped
```

### Safety boundaries preserved (Round 15)

- `gui/orchestrator/git_tools.py` still contains zero mutating Git
  commands.  The new `find_custom_merge_driver_paths` helper uses
  only `git check-attr` and `git config --get`, both read-only.
  The existing `test_source_does_not_execute_forbidden_git_commands`
  guarantee still holds.
- `gui/orchestrator/git_workflow.py` adds no new mutating commands.
  The new `_enumerate_merge_affected_paths` helper uses only
  `git diff --name-only` (read-only).  The merge-driver check runs
  *before* `git merge` so the worktree is untouched when the check
  fails.
- The `worktree` entry in `_HOOKED_COMMANDS` only affects the
  `core.hooksPath` env override that is already applied to
  `commit`, `merge`, and `commit-tree`.  The override points at an
  empty directory so no user-installed hooks fire during the
  controlled Git invocation.  On the happy path (no hooks
  installed) behaviour is unchanged.
- The per-task `RLock` registry in `gui/server.py` is keyed by
  task id.  Different tasks run concurrently (verified by
  `test_concurrent_commit_requests_for_different_tasks_do_not_block`)
  so the lock does not become a global chokepoint.  The `RLock`
  covers the *full* load → validate → mutate → save span so the
  loser of a duplicate race always observes the winner's saved
  metadata and short-circuits via the existing "already committed"
  / "already merged" guards.
- No `.env` file is read or modified.  No `.git` directory is
  written to.  No `git commit`, `git push`, `git reset`,
  `git checkout`, `git branch -D`, or `git worktree remove` is
  issued.  The Round 14 safety boundaries (HEAD resolution,
  clean-filter rejection, post-stage drift detection, parent-
  history verification, conflict-abort) are unchanged.
- The happy path is unchanged: when no custom merge driver is
  configured, when no post-checkout hook is installed, and when
  no duplicate concurrent request arrives, the worktree-creation /
  commit / merge flow behaves exactly as in Round 14.  The only
  observable change is on the unsafe path — non-ASCII clean-filter
  paths are now detected, custom merge drivers are now refused,
  worktree-creation hooks are now suppressed, and duplicate
  concurrent commit / merge requests are now serialised.

---

## Round 16 (Codex P1 fixes)

Address the five P1 findings Codex raised on Round 15.  Each fix is
narrow and additive — the controlled commit / merge happy path is
unchanged, and the existing safety boundaries (HEAD CAS, snapshot
drift, parent-history, custom-merge-driver, hook disabling, per-task
lock) remain in place.

### Summary

- **P1-1** — `_enumerate_merge_affected_paths` now fails closed when
  `git diff --name-only` errors, raising `GitError` (surfaced as
  `MergeError` by `controlled_merge_to_main`) instead of returning
  an empty list and silently skipping the merge-driver validation
  step.  The merge is blocked and the user is told to re-review.
- **P1-2** — `find_custom_merge_driver_paths` now recognises Git's
  built-in auto-resolving drivers `union` and `ours`.  These need
  no `merge.<name>.driver` config entry, so the previous config-only
  probe let a `.gitattributes` line like `feature.txt merge=union`
  bypass the no-auto-resolution policy and absorb unreviewed content
  into the trunk.  Other attribute names without a matching config
  remain no-ops (Git falls back to its default 3-way merge).
- **P1-3** — `gui/server.py` adds a per-resource `RLock` registry
  keyed by canonical worktree / repository path.  `commit_task_changes`
  and `merge_task_to_main` now hold both the per-task lock (state-
  machine guard) and the per-resource lock (Git-mutation serialiser)
  so two different tasks bound to the same worktree / primary repo
  cannot interleave `git add` / `commit-tree` / `update-ref` / `merge`
  against the same index and refs.  Unrelated repositories still
  proceed concurrently.
- **P1-4** — `controlled_merge_to_main` now refuses to start a merge
  when the repository already has an in-progress operation marker
  (`MERGE_HEAD`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`, `REBASE_HEAD`,
  `BISECT_LOG`, sequencer).  This establishes a clean "we started
  the merge" invariant: any `MERGE_HEAD` observed after the merge
  call was created by this invocation and `git merge --abort` is
  safe.  The previous unconditional abort could clobber a pre-existing
  in-progress merge started by someone else.
- **P1-5** — `controlled_commit` persists the immutable
  `new_commit_sha` (created by `commit-tree`) as the controlled
  commit identity instead of rereading HEAD.  An external ref update
  between the CAS `update-ref` and the post-commit `rev-parse HEAD`
  previously made the task record trust an unrelated commit SHA.
  The commit object is immutable and was authored by this invocation;
  the new return field `headDriftSha` separately surfaces any
  subsequent branch movement so the audit trail can reconcile the
  recorded commit with the live branch tip without misattributing
  the controlled commit.

### Round 16 Files Modified

| File | Change |
|---|---|
| `gui/orchestrator/git_tools.py` | `find_custom_merge_driver_paths` recognises built-in `union` / `ours` merge drivers as unsafe without requiring a `merge.<name>.driver` config entry (P1-2). Other attribute names without a matching config remain no-ops. |
| `gui/orchestrator/git_workflow.py` | `_enumerate_merge_affected_paths` raises `GitError` on `git diff` failure instead of returning an empty list (P1-1). `controlled_merge_to_main` catches the `GitError` and re-raises as `MergeError` so the caller records a `MERGE_BLOCKED` event (P1-1). New pre-merge `get_in_progress_operations` check rejects pre-existing in-progress ops, and the merge-failure path now only invokes `git merge --abort` when `MERGE_HEAD` actually exists after the merge call (P1-4). `controlled_commit` uses `new_commit_sha` directly as `commitSha`, derives the short SHA via `rev-parse --short <new_commit_sha>`, and returns a new `headDriftSha` field populated when HEAD subsequently diverges (P1-5). |
| `gui/server.py` | New `_RESOURCE_LOCKS` registry and `_resource_operation_lock(resource_path)` helper using `threading.RLock` keyed by canonical worktree path (P1-3). `_commit_task_changes_locked` and `_merge_task_to_main_locked` wrap the actual `controlled_commit` / `controlled_merge_to_main` invocation in `with _resource_operation_lock(project_path / primary_path):`, reloading the task inside the resource lock so a concurrent same-worktree task that completed while we waited is observed and surfaced as a conflict. The COMMITTED history event includes `headDriftSha` when non-None so the audit trail records subsequent branch movement (P1-5). |
| `tests/test_git_tools.py` | Two new tests for `find_custom_merge_driver_paths`: `test_find_custom_merge_driver_paths_detects_builtin_union_driver`, `test_find_custom_merge_driver_paths_detects_builtin_ours_driver` (P1-2 regression coverage). |
| `tests/test_worktree.py` | New `test_merge_blocks_when_path_enumeration_fails` (P1-1 — merge refuses when diff enumeration fails). New `test_commit_records_new_commit_sha_and_no_drift_on_happy_path` and `test_commit_records_drift_when_head_advances_after_cas` (P1-5 — commit identity and drift detection). New `test_merge_blocks_when_merge_head_already_exists` and `test_merge_aborts_when_invocation_started_the_merge` (P1-4 — pre-existing marker vs. invocation-started abort). |
| `tests/test_gui_server.py` | New `test_concurrent_commit_requests_for_different_tasks_same_worktree_are_serialised` (P1-3 — two different task IDs bound to the same worktree must serialise their Git mutations). `test_concurrent_commit_requests_for_different_tasks_do_not_block` is updated to put the two tasks in *different* worktrees so the per-resource lock does not over-serialise unrelated repositories. |

### Round 16 Test Results

```
py -B -m pytest -q
=> 278 passed
```

### Safety boundaries preserved (Round 16)

- `gui/orchestrator/git_tools.py` still contains zero mutating Git
  commands.  The built-in driver recognition in
  `find_custom_merge_driver_paths` uses only the existing
  `git check-attr` + `git config --get` probes — no new commands.
- `gui/orchestrator/git_workflow.py` adds no new mutating commands.
  The new pre-merge `get_in_progress_operations` check is read-only.
  The conditional `git merge --abort` only runs when the merge was
  demonstrably started by this invocation, so the abort can never
  destroy a pre-existing in-progress merge started by someone else.
- The per-resource `RLock` registry in `gui/server.py` is keyed by
  canonical worktree / repo path.  Unrelated repositories proceed
  concurrently (verified by the updated
  `test_concurrent_commit_requests_for_different_tasks_do_not_block`).
  The per-task lock still serialises duplicate requests for the same
  task, and the resource lock additionally serialises Git mutations
  on the same worktree / primary repo.  Acquisition order is
  task-lock → resource-lock; the only nesting is per request, so
  there is no deadlock risk.
- The `controlled_commit` return signature is extended (new optional
  `headDriftSha` field); existing callers that only read `commitSha`
  / `commitShortSha` / `commitMessage` continue to work.  The
  recorded `commitSha` is the immutable object created by
  `commit-tree`, never a reread of HEAD.
- No `.env` file is read or modified.  No `.git` directory is
  written to.  No new `git commit`, `git push`, `git reset`,
  `git checkout`, `git branch -D`, or `git worktree remove` is
  issued.  The Round 15 safety boundaries (HEAD CAS, snapshot drift,
  parent-history verification, custom-merge-driver refusal, hook
  disabling, per-task lock) are unchanged.
- The happy path is unchanged: when no in-progress operation marker
  exists, when no built-in `union` / `ours` driver is configured,
  when each task is on its own worktree, and when HEAD does not
  drift externally between the CAS update and the post-commit
  observation, the worktree-creation / commit / merge flow behaves
  exactly as in Round 15.

## Round 17 (Codex P1-1 / P1-2 / P1-3 / P1-4 / P1-5 / P2-1)

Address the six findings Codex raised on Round 16.  The fixes harden
the merge / state-mutation / worktree-registration boundaries and
tighten the read-side probes; the controlled-commit happy path is
unchanged and every existing safety boundary (HEAD CAS, snapshot
drift, parent-history, custom-merge-driver, hook disabling,
per-task lock, per-resource lock, in-progress-op refusal) remains
in place.

### Summary

- **P1-1** — `controlled_merge_to_main` now refuses to start when
  the main branch carries an unsafe merge configuration
  (`branch.<main>.mergeOptions = -X ours` / `-s ours` or
  `merge.strategy = ours`) and, even on a clean repo, `_run_git_text`
  injects `GIT_CONFIG_*` env vars that clear `core.hooksPath`,
  `merge.strategy`, and `branch.<branch>.mergeOptions` for every
  merge invocation.  Previously an attacker who set
  `branch.<main>.mergeOptions = -X ours` could turn a contested
  merge into an auto-resolved "ours" merge that silently absorbed
  unreviewed trunk content into the recorded merge commit.
- **P1-2** — `cancel_task`, `archive_task`, `restore_archived_task`,
  `move_task_to_trash`, and `restore_task_from_trash` now acquire
  the same per-task `RLock` that guards commit / merge.  Previously
  a concurrent cancel / archive / trash request could mutate the
  task's state field while a commit / merge was between
  `controlled_commit` / `controlled_merge_to_main` and the
  `save_all_tasks` call, overwriting `COMMITTED` / `MERGED`
  metadata with `CANCELLED` / `ARCHIVED` / `TRASHED` and leaving
  the audit trail inconsistent with the live Git state.
- **P1-3** — `controlled_merge_to_main` no longer records the
  merge commit SHA via a separate `git rev-parse HEAD` call after
  the merge.  Instead it builds the merge commit object itself
  via `git merge-tree --write-tree <main_head> <merge_ref>` +
  `git commit-tree <tree> -p <main_head> -p <merge_ref>` and then
  publishes it via `git update-ref HEAD <new> <main_head>` CAS.
  The recorded `mergeCommitSha` is therefore the immutable object
  authored by this invocation — external ref movement between the
  CAS and the previous reread could no longer misattribute the
  recorded merge to an unrelated commit.  A new `headDriftSha`
  field separately surfaces any subsequent HEAD movement so the
  audit trail can reconcile the recorded merge with the live
  branch tip.
- **P1-4** — `get_in_progress_operations` now fails closed when
  `git rev-parse --absolute-git-dir` returns a non-zero exit code,
  raising `GitError` (surfaced as `MergeError` by
  `controlled_merge_to_main`) instead of silently returning an
  empty list and proceeding with the merge.  The marker probe now
  also recognises `rebase-merge/` (directory, used by `--merge`
  rebases), `rebase-apply/` (directory, used by legacy am-style
  rebases and `git am`), and `AM_HEAD` (file, used by `git am`).
  Previously a repository in the middle of a `--rebase-merges`
  session, a legacy rebase, or a patch-application flow appeared
  "clean" to the pre-merge gate.
- **P1-5** — `find_clean_filtered_paths` and
  `find_custom_merge_driver_paths` now treat return code 1 from
  `git config --get` as "absent" and any other non-zero return
  code as a probe error.  Previously every non-zero return code
  was treated as "absent", so a broken `git config` invocation
  (e.g. corrupted config file, permission error, missing binary)
  silently degraded the snapshot / merge-driver rejection
  boundary instead of failing closed.
- **P2-1** — `create_project_worktree` now treats a worktree-add
  success followed by a project-registration failure as a
  recoverable partial-success state instead of a generic 500.
  It first retries registration via the primary-path auto-discovery
  path (which calls `_auto_discover_sibling_worktrees`); if that
  also fails it returns a structured payload with
  `project: null`, `registeredAutomatically: false`, the resolved
  branch, and `recoveryInstructions`, and writes a
  `project.worktree.create.partial` audit-log event.  The caller
  can therefore show the user how to register the orphan worktree
  manually instead of seeing the worktree disappear into an
  unregistered state.

### Round 17 Files Modified

| File | Change |
|---|---|
| `gui/orchestrator/git_tools.py` | `get_in_progress_operations` rewritten to use a single `git rev-parse --absolute-git-dir` call (raising `GitError` on failure) and probe markers directly via `Path.exists` / `is_dir`; added `rebase-merge` (dir), `rebase-apply` (dir), and `AM_HEAD` (file) markers (P1-4). `find_clean_filtered_paths` and `find_custom_merge_driver_paths` now treat `returncode == 1` as "absent" and any other non-zero as `GitError` (P1-5). |
| `gui/orchestrator/git_workflow.py` | New `_resolve_merge_branch_for_override(project_path)` helper resolves the current branch via `git rev-parse --abbrev-ref HEAD` for env-var construction. New `_detect_unsafe_merge_config(project_path, main_branch)` checks `branch.<main>.mergeOptions` and `merge.strategy`, mapping return code 1 to "absent" and any other non-zero to `GitError`. `_run_git_text` injects three `GIT_CONFIG_*` entries (clearing `core.hooksPath`, `merge.strategy`, `branch.<branch>.mergeOptions`) for every `git merge` invocation (P1-1). `controlled_merge_to_main` performs an unsafe-config check after `main_branch` resolution and now uses a `git merge-tree --write-tree` + `git commit-tree -p -p` + `git read-tree --reset -u` + `git update-ref HEAD <new> <expected_head>` CAS sequence instead of `git merge` + `rev-parse HEAD`; the recorded `mergeCommitSha` is the immutable `new_commit_sha` produced by `commit-tree`, and a new `headDriftSha` field surfaces any subsequent branch movement (P1-3). |
| `gui/server.py` | New `_task_operation_lock(task_id)` wrapper applied to `cancel_task`, `archive_task`, `restore_archived_task`, `move_task_to_trash`, and `restore_task_from_trash`; each endpoint now has a `_*_locked` inner function that runs under the same per-task `RLock` as commit / merge (P1-2). `create_project_worktree` performs registration via `project_store.add_project(new_path)` first, retries via `project_store.add_project(primary_path)` on failure, falls back to a structured partial-success payload (`project: null`, `registeredAutomatically: false`, `recoveryInstructions`) with a `project.worktree.create.partial` audit-log event if both fail, and returns `registeredAutomatically: true` on auto-discovery success (P2-1). |
| `tests/test_git_tools.py` | New `InProgressOperationsTests` class (7 tests): empty repo, MERGE_HEAD, rebase-merge dir, rebase-apply dir, AM_HEAD, sequencer dir, fail-closed on `rev-parse` error (P1-4). New `ConfigProbeReturnCodeTests` class (4 tests): clean probe error, process probe error, returncode 1 = absent, custom merge driver error (P1-5). |
| `tests/test_worktree.py` | Updated `test_merge_command_uses_reviewed_sha_not_branch_name`, `test_merge_uses_branch_name_when_no_expected_sha`, `test_merge_cannot_pull_commits_added_after_verification`, `test_merge_aborts_when_invocation_started_the_merge`, and `test_merge_runs_through_no_hooks_env_override` for the new merge-tree / commit-tree / read-tree / update-ref sequence. New `test_merge_blocks_when_branch_merge_options_configured` and `test_merge_blocks_when_global_merge_strategy_configured` (P1-1). New `test_merge_returns_immutable_commit_tree_sha_not_rev_parse_head` and `test_merge_blocks_when_head_moves_between_capture_and_cas` (P1-3). |
| `tests/test_gui_server.py` | New `test_concurrent_cancel_requests_are_serialised`, `test_concurrent_archive_and_cancel_are_serialised`, `test_concurrent_move_to_trash_requests_are_serialised` (P1-2). New `WorktreeRegistrationTests` class (3 tests): partial-success when both registrations fail, recovery via primary-path auto-discovery, happy path with audit log (P2-1). |

### Round 17 Test Results

```
py -B -m pytest -q
=> 318 passed
```

### Safety boundaries preserved (Round 17)

- `gui/orchestrator/git_tools.py` still contains zero mutating Git
  commands.  The new `get_in_progress_operations` probes markers
  via `Path.exists` / `is_dir` after a single read-only
  `git rev-parse --absolute-git-dir` invocation.  The tightened
  config probes still use the existing `git config --get` plumbing.
- `gui/orchestrator/git_workflow.py` adds no new mutating commands
  beyond the `merge-tree` / `commit-tree` / `read-tree --reset -u`
  / `update-ref` sequence that replaces `git merge` + `rev-parse
  HEAD`.  `git merge-tree --write-tree` computes the tree object
  without touching the index or working tree; `git read-tree
  --reset -u` syncs both to that tree (functionally equivalent to
  `git reset --hard <tree>` but does not use the forbidden `reset`
  verb); `git update-ref HEAD <new> <expected>` is the same CAS
  primitive used by `controlled_commit`.  The recorded
  `mergeCommitSha` is the immutable object authored by
  `commit-tree`, never a reread of HEAD.
- The unsafe-config guard in `controlled_merge_to_main` rejects
  `branch.<main>.mergeOptions` and `merge.strategy = ours` before
  the merge starts; the `GIT_CONFIG_*` env injection is a
  belt-and-suspenders override so even a missed config cannot
  downgrade the merge to `-X ours` / `-s ours`.  Hooks stay
  disabled via the same `core.hooksPath = <empty-dir>` env var.
- The per-task `RLock` is now held by every state-mutating
  endpoint (commit, merge, cancel, archive, restore-archive,
  move-to-trash, restore-from-trash).  Acquisition order remains
  task-lock → resource-lock; the only nesting is per request,
  so there is no deadlock risk.
- `create_project_worktree` writes no `.git` content and touches
  no `.env` file.  The partial-success recovery path is purely a
  project-store registration retry — the worktree itself is
  already created by `controlled_create_worktree` and the user
  can recover via the printed instructions.
- No `.env` file is read or modified.  No `.git` directory is
  written to.  No new `git commit`, `git push`, `git reset`,
  `git checkout`, `git switch`, `git restore`, `git branch -D`,
  `git stash drop`, `git tag -d`, or `git worktree remove` is
  issued.  The Round 16 safety boundaries (HEAD CAS, snapshot
  drift, parent-history verification, custom-merge-driver
  refusal, built-in `union` / `ours` driver refusal, hook
  disabling, per-task lock, per-resource lock, in-progress-op
  refusal) are unchanged.
- The happy path is unchanged: when no unsafe merge config is
  present, when no in-progress operation marker exists, when each
  task is on its own worktree, when HEAD does not drift externally
  between the CAS update and the post-merge observation, and when
  worktree registration succeeds on the first or second attempt,
  the worktree-creation / commit / merge flow behaves exactly as
  in Round 16.


## Round 18 (Codex P1-1 / P1-2 / P1-3 / P1-4 / P2-1 / P2-2 convergence)

This round folds six Codex findings into a single security-convergence
pass.  The findings share a common theme — the worktree / commit /
merge lifecycle was leaking invariants across the CAS boundary — so
they are addressed as one coherent change rather than six independent
patches.  As in Round 17, no `.env` is read or modified, no `.git`
directory is written to, and no forbidden Git command (`commit`,
`push`, `reset`, `clean`, `checkout`, `switch`, `restore`,
`branch -D`, `stash drop`, `tag -d`, `worktree remove`) is issued.

### Root cause summary

- **P1-1 (merge is not atomic across ref + worktree).**  The Round 17
  merge path materialized the new tree into the primary worktree with
  `git read-tree --reset -u <tree>` *before* advancing `HEAD` via
  `git update-ref HEAD <new> <old>`.  A crash between those two steps
  left the worktree at the new tree while `HEAD` still pointed at the
  old SHA — a silent divergence between recorded state and on-disk
  state that the next launch would treat as a pre-existing condition.
- **P1-2 (smudge / process filters not covered).**  Round 17 refused
  custom *clean* filters on commit-side paths and custom *merge*
  drivers on merge-side paths, but did not refuse *smudge* or
  *process* filters.  A `smudge` filter fires on checkout, so it
  runs during `git worktree add` and during `git read-tree -m -u`.
  A `process` filter is bidirectional and fires on both sides.  Either
  can transform, drop, or inject content without leaving a record.
- **P1-3 (double resolution of primary HEAD).**  The merge path
  called `git rev-parse HEAD` once for the drift / detached-HEAD
  guards near the entry, and a second time when computing the
  reachability base used as the CAS expected value.  Between those
  two calls the branch could advance externally — the CAS would
  then use a newer SHA than the snapshot the user actually reviewed,
  so a successful CAS did not prove "the reviewed tree was applied
  to the reviewed parent".
- **P1-4 (launch / complete endpoints not serialized).**  Round 17
  wrapped only `commit_task_changes` and `merge_task_to_main` in the
  per-task lock.  The four remaining mutating endpoints
  (`launch_claude_task`, `complete_claude_task`, `launch_codex_task`,
  `complete_codex_task`) could race each other and race the
  commit / merge flow on the same task, producing TOCTOU gaps where
  a stale read of `task.worktreeBranch` or `task.status` drove the
  mutation decision.
- **P2-1 (partial-success masking).**  When `controlled_create_worktree`
  succeeded but worktree *registration* failed, the API returned a
  generic error toast and the front-end fell through to its
  "selectProject" happy path, silently hiding the orphan worktree.
- **P2-2 (post-CAS head drift not modeled).**  When `git update-ref`
  succeeded but a subsequent observation of `HEAD` returned a SHA
  that was not the merge commit, the merge was reported as PASS
  without distinguishing "branch advanced past merge" (legitimate,
  a fast follow-up commit landed) from "branch force-moved away
  from merge" (hostile, the merge was rewritten out of history).

### Implementation

**Single primary-HEAD capture (P1-3).**  `controlled_merge_to_main`
now resolves `main_head = git rev-parse HEAD` exactly once, immediately
after the dirty / detached-HEAD / in-progress-op guards and before
any reachability or CAS work.  That single value is used as (a) the
drift check anchor, (b) the reachability base (`expected_base_sha`
when supplied, otherwise `main_head`), and (c) the CAS expected value
when no explicit `expected_main_sha` is provided.  There is no second
`rev-parse HEAD` call anywhere in the merge path.

**CAS-first merge with atomic rollback (P1-1).**  The Round 17
ordering (materialize tree, then advance `HEAD`) was inverted.
The new ordering is:

1. Compute the merge tree with `git merge-tree --write-tree` and
   build the immutable commit object with `git commit-tree` — both
   are pure, they write no ref and mutate no working tree.
2. Advance `HEAD` via compare-and-swap,
   `git update-ref HEAD <new_commit_sha> <main_head>`.  If the CAS
   fails the branch moved externally and the merge is refused; no
   working-tree mutation has occurred.
3. Materialize the new tree into the primary worktree with the
   two-tree form `git read-tree -m -u <main_head> <new_commit_sha>`,
   which refuses to overwrite local modifications and produces a
   tree identical to `new_commit_sha` because the index already
   matches `main_head` (verified by the dirty guard).
4. If `read-tree` fails, roll back the ref with the inverse CAS
   `git update-ref HEAD <main_head> <new_commit_sha>`.  This is the
   one case in the entire merge flow where a CAS is used to *undo*
   a successful CAS; it is safe because `<new_commit_sha>` is the
   value just written and `main_head` is the value just overwritten.

After step 3 succeeds, `HEAD` and the worktree agree on
`new_commit_sha`.  Step 4 is the only path that can leave them
disagreeing, and on that path the merge is reported as `BLOCKED`
with the rollback recorded in the audit log.

**Smudge / process filter guards (P1-2).**  Two new helpers in
`gui/orchestrator/git_tools.py` share a private
`_find_filter_active_paths` implementation:

- `find_smudge_filtered_paths(project_path, candidate_paths=None)`
  enumerates `.gitattributes` filter attributes and, for each
  declared filter, probes `config --get filter.<name>.smudge` *and*
  `config --get filter.<name>.process`.  Any non-empty value means
  the filter is armed.  The probe is fail-closed: a non-zero
  `returncode` other than `1` (the canonical "key not found") is
  treated as "filter present, cannot prove safe" and the candidate
  path is included in the refusal set.
- `list_tracked_paths(project_path)` returns the set of paths
  tracked at `HEAD` via `git ls-tree -r --name-only -z HEAD`, so
  the smudge probe runs against real content paths rather than the
  free-form attribute index.

`controlled_create_worktree` runs the smudge probe against the
tracked-paths set before `git worktree add` and refuses creation if
any path is smudge-filtered — a smudge filter would have run inside
the new worktree during checkout.  `controlled_merge_to_main` runs
the same probe against `affected_paths` (the files the merge
touched) before the CAS, refusing the merge if any merge-side path
is smudge- or process-filtered — such a filter would have run inside
`read-tree -m -u` and could have transformed the merged content.

**Per-task lock extended to all mutating endpoints (P1-4).**  The
four remaining launch / complete endpoints in `gui/server.py` were
refactored into a public lock-acquiring wrapper plus a private
`_..._locked` inner function, mirroring the existing
`commit_task_changes` / `_commit_task_changes_locked` pattern.  All
six mutating task endpoints now acquire `_task_operation_lock(task_id)`
for the full read-decide-write window.  The deterministic lock
ordering (task lock first, then repository / worktree resource lock)
is preserved because the resource lock is taken only inside
`_commit_task_changes_locked` and `_merge_task_to_main_locked`, which
are already running under the task lock.

Three new regression tests in `tests/test_gui_server.py`
(`test_launch_claude_is_serialized_by_task`,
`test_complete_codex_is_serialized_by_task`,
`test_launch_complete_commit_merge_share_one_lock`) drive concurrent
calls through patched inner functions and assert that only one
inner call is in flight at a time.

**Front-end branching on partial success (P2-1).**  When
`controlled_create_worktree` succeeds but registration fails, the
API now returns `{"registeredAutomatically": false,
"recoveryInstructions": "...", "project": {"path": ...},
"branch": ...}`.  `addWorktreeFromForm` in `gui/static/app.js`
branches on `data.registeredAutomatically === false` before any
happy-path code, shows an `alert` with the recovery instructions
and the created path, toasts the partial-success state, and
returns without calling `selectProject` — so the orphan worktree
is surfaced, not silently consumed.

**Post-CAS head drift modeling (P2-2).**  `controlled_merge_to_main`
already returned a `headDriftSha` field when the post-CAS `HEAD`
observation disagreed with the merge commit SHA; Round 18 wires
that field through the persistence and audit layer:

- `Task` in `gui/orchestrator/models.py` gains a `headDriftSha`
  field with full `from_dict` / `to_dict` round-trip, so the value
  survives a reload.
- `_merge_task_to_main_locked` in `gui/server.py` copies
  `result.get("headDriftSha")` onto `task.headDriftSha`, adds it
  to the `MERGED` history entry's kwargs and message when present,
  and includes it in the `task.merge` audit entry's `details`.
- After the merge is persisted, if `task.headDriftSha` is set the
  server runs `is_ancestor(primary_path, task.mergeCommitSha,
  task.headDriftSha)` and emits a separate
  `task.merge.head_unreachable` audit event when the reachability
  probe returns `False`.  This distinguishes "branch advanced past
  merge" (probe returns `True`, merge is still in history) from
  "branch force-moved away from merge" (probe returns `False`,
  merge has been rewritten out).

Four new regression tests in `tests/test_gui_server.py`
(`test_merge_task_records_head_drift_in_history_and_audit`,
`test_merge_task_emits_head_unreachable_when_not_ancestor`,
`test_merge_task_omits_head_drift_when_clean`,
`test_merge_task_handles_is_ancestor_failure`) cover the round-trip,
the reachability split, the clean path, and the probe-failure path.

### Tests

- `tests/test_git_tools.py` adds nine unit tests for
  `find_smudge_filtered_paths` and `list_tracked_paths` covering the
  empty repo, no-filter, smudge-only, process-only, both-sides,
  untracked-attribute, and probe-failure paths.
- `tests/test_worktree.py` adds three regression tests:
  `test_merge_blocks_when_smudge_filter_configured_on_merge_path`
  (P1-2 merge side),
  `test_create_worktree_blocks_when_smudge_filter_configured`
  (P1-2 worktree side), and
  `test_merge_uses_captured_main_head_for_cas_expected_value`
  (P1-3 single capture).
- `tests/test_gui_server.py` adds three concurrency tests for the
  per-task lock (P1-4) and four merge audit tests (P2-2).
- The full suite runs clean: **324 tests pass** with no skipped,
  no errored, no failures.

### Residual risk

- **P2-1 has no automated front-end coverage.**  The branching
  behavior in `addWorktreeFromForm` is verified by inspection only;
  there is no JS unit test asserting that the `alert` is shown and
  `selectProject` is *not* called on `registeredAutomatically === false`.
  The back-end half (the API response shape) is covered by existing
  tests; the front-end half is a manual-verification gap.
- **P1-1 rollback CAS itself failing is surfaced, not auto-recovered.**
  If step 4's inverse CAS fails (because the branch moved *again*
  between step 2 and step 4), the merge is reported as `BLOCKED`
  and the audit log records the unresolved divergence, but the
  system does not attempt further recovery.  An operator must
  reconcile the worktree against `HEAD` manually.  This is the same
  residual-risk shape as the Round 16 / 17 in-progress-op refusal:
  fail-closed with an audit trail rather than guess-and-retry.
- **P1-2's smudge probe runs against the configured filter name set
  at merge time.**  If a filter is added to `.gitconfig` between the
  worktree-creation probe and the merge probe, the merge probe will
  catch it; if a filter is added after a successful merge but before
  the next operation, the next operation's probe will catch it.  The
  probe is always run, never cached across operations.
- **P2-2's reachability probe uses `git merge-base --is-ancestor`.**
  If the underlying Git repository is corrupted such that
  `is_ancestor` cannot run, the probe returns `None` and the
  separate `task.merge.head_unreachable` audit event is *not*
  emitted — the existing `task.merge` event with `headDriftSha`
  populated remains the only signal.  This is fail-open on the
  *additional* signal, not on the *primary* signal.
- The happy path is unchanged: when no smudge / process filter is
  configured, when no in-progress operation marker exists, when
  each task is on its own worktree, when `HEAD` does not drift
  externally between the CAS update and the post-merge observation,
  and when worktree registration succeeds on the first attempt,
  the worktree-creation / commit / merge flow behaves exactly as
  in Round 17.

---

## Round 19 (Codex P1-1 / P1-2 / P1-3 / P2-1 / P2-2 / P2-3 convergence)

This round addresses six verified Codex findings from the Round 18
review in a single coherent pass.  All six findings share a common
theme — the worktree-creation, GUI-merge, and post-crash recovery
paths still leaked invariants across lock acquisition, SHA pinning,
or fail-open probes — so they are addressed together to keep the
implementation internally consistent.  As in every prior round, no
`.env` is read or modified, no `.git` directory is written to, and
no forbidden Git command (`push`, `reset`, `clean`, `checkout`,
`switch`, `restore`, `branch -D`, `stash drop`, `tag -d`,
`worktree remove`, the legacy in-place `git merge`) is issued.

### Findings, root causes, fixes, tests, and residual risk

| ID | Root cause | Implementation | Regression tests | Residual risk |
| --- | --- | --- | --- | --- |
| **P1-1** | The first Round 19 draft still deleted the journal immediately after Git materialisation, before task JSON and audit persistence, and startup recovery called the Git recovery helper without task/resource locks. | The journal now records version, operation/task/round, canonical primary repository identity, old HEAD, immutable source/reviewed/new SHAs, branches, timestamps and phase. Phases extend through `materialised → task_persisted → audit_persisted` (and rollback equivalents); only the server deletes after atomic task save and fsync'd, operation-idempotent audit append. Startup and next-operation recovery acquire `task lock → primary resource lock`, reload and revalidate task identity, merge parents, ref, index tree and worktree before materialising or reverse-CAS. Drift, user edits, corrupt probes, missing task state, or any identity mismatch retain the journal and emit blocked/manual-reconciliation history and audit. | `tests/test_worktree.py` covers post-CAS crash, interrupted materialisation, ref drift, user edits, pre-CAS, materialised, rolled-back, corrupt/unknown journals and phase transitions. `tests/test_gui_server.py` adds `test_journal_survives_task_metadata_save_failure`, `test_journal_survives_audit_persistence_failure`, and `test_startup_recovery_acquires_task_before_resource_lock`. | Reverse-CAS failure or any state that cannot be proven remains intentionally manual: the journal is retained and no forced cleanup or overwrite is attempted. |
| **P1-2** | Worktree creation ran the clean / filter checks outside any lock, then called `git worktree add` against the implicit `HEAD`. A concurrent controlled merge could advance main between the checks and the checkout, causing the new worktree to be checked out from a SHA that was never validated. | `create_project_worktree` in `gui/server.py` now wraps the complete operation (HEAD capture, clean check, filter/config checks, branch validation, `git worktree add`, registration, partial-success response) in `_resource_operation_lock(primary_path)` — the same lock the controlled-merge service uses on the same primary repository. A single starting SHA is captured inside the lock and passed explicitly to `create_worktree` as the final `git worktree add` start-point. `create_worktree` validates the SHA (fail-closed on probe error), passes it as the start-point, and verifies the new worktree's HEAD matches after checkout. | 4 new tests in `tests/test_worktree.py` (`test_create_worktree_uses_supplied_start_sha_as_worktree_add_startpoint`, `test_create_worktree_rejects_invalid_start_sha`, `test_create_worktree_rejects_empty_start_sha`, `test_create_worktree_legacy_call_without_start_sha_still_works`) plus 1 new server-level concurrency test in `tests/test_gui_server.py` (`test_worktree_registration_serialises_with_concurrent_merge`) that proves `create_project_worktree` and `merge_task_to_main` cannot be inside their respective critical sections at the same time when both target the same primary repository. | The lock serialises only operations against the *same* primary path. Unrelated repositories proceed concurrently. The start SHA is captured once and verified once; a smudge filter or post-checkout hook that perturbs the checkout despite the upfront guards is surfaced as a SHA-mismatch raise rather than silently consumed. |
| **P1-3** | The GUI merge path passed `expected_commit_sha` and `expected_base_sha` to `controlled_merge_to_main`, but the lower-level reachability + sole-parent checks silently no-op when `expected_base_sha` is empty. When the task's reviewed snapshot was missing or stale (round mismatch), the merge would still execute against whatever `HEAD` happened to be — letting unreviewed pre-task commits slip into the trunk. | Added `_reviewed_base_block_reason(task)` helper in `gui/server.py`. `_merge_task_to_main_locked` calls it twice — once before acquiring the resource lock (fast reject for clear cases) and once after acquiring the lock (in case a concurrent flow cleared the reviewed snapshot while waiting). The helper returns a non-`None` reason when `reviewedRound` is `None`, when `reviewedRound != task.round`, or when `reviewedHeadSha` is empty. Every existing GUI merge test was updated to set `task.reviewedRound = task.round` and `task.reviewedHeadSha = "basesha1"` so the blocking guard passes. | 3 new tests in `tests/test_gui_server.py` (`test_merge_task_blocks_when_reviewed_snapshot_missing`, `test_merge_task_blocks_when_reviewed_round_is_stale`, `test_merge_task_blocks_when_reviewed_head_sha_empty`) plus the existing 8 merge tests updated to provide a valid reviewed baseline. | The guard fails *closed*: a task whose reviewed snapshot is missing or stale cannot be merged from the GUI. The user must re-run Claude completion and Codex review. The lower-level `controlled_merge_to_main` retains its compatibility mode (`expected_base_sha` optional) for direct callers that do not need the GUI-level guard. |
| **P2-1** | `is_ancestor` wrapped `git merge-base --is-ancestor` but treated every non-zero exit as "not an ancestor", including exit code 2 (real Git error). A corrupted repository, locked ref, or unavailable object would silently look like a successful "not reachable" probe and the caller would proceed as if the reviewed base were unreachable. | `is_ancestor` in `gui/orchestrator/git_tools.py` now distinguishes the two absence return codes (0 = ancestor, 1 = not ancestor) from every other exit code. Exit 2 / non-zero non-one raises `GitError` with the stderr or a synthesised message. The merge caller already wraps `GitError` as `MERGE_BLOCKED` since Round 14, so the fail-closed probe surfaces as a blocked merge with a clear audit trail rather than a silent false-negative. | 5 new tests in `tests/test_git_tools.py` (`test_is_ancestor_returns_true_on_exit_zero`, `test_is_ancestor_returns_false_on_exit_one`, `test_is_ancestor_raises_on_exit_two`, `test_is_ancestor_raises_on_execution_failure`, `test_is_ancestor_handles_empty_inputs`) plus 1 new caller-side audit classification test in `tests/test_gui_server.py` (`test_merge_task_emits_probe_failed_audit_when_is_ancestor_raises`) that verifies the probe failure is recorded as a `task.merge.reachability_probe_failed` audit event rather than a successful "not reachable" outcome. | The probe is read-only and does not mutate repository state; a transient failure (e.g. concurrent `git gc` holding a lock) surfaces as a blocked merge the user can retry. The fail-closed behaviour is intentional: a probe that cannot run is treated as a probe that did not return a usable answer. |
| **P2-2** | When worktree creation succeeded but registration failed, the partial response had `project: null` but no top-level path, while the UI attempted the nested path/selection flow. | Partial responses now include top-level `path`; the UI reads `data.path` first, falls back to `data.project.path`, displays `recoveryInstructions`, returns before selection, and selects only when `data?.project?.id` exists. | Backend payload tests plus `test_frontend_partial_worktree_uses_top_level_path_without_selection` statically verify path precedence, instructions, and no partial-path `await selectProject`. | This is source-level frontend regression coverage rather than a browser integration test; the backend contract and actual branch code are both pinned. |
| **P2-3** | The merge UI confirm dialog and current README / QUICK_START text still described the removed in-place merge/abort model. | Current text now describes `merge-tree → commit-tree → CAS update-ref → guarded materialisation → durable journal → task/audit persistence`, with drift/manual reconciliation rather than an in-place abort claim. Historical report sections remain as audit history. | `test_frontend_partial_worktree_uses_top_level_path_without_selection` also asserts the current dialog contains the CAS + task/audit wording and no `git merge --no-ff`; a repository search verifies current README, QUICK_START and module comments contain no stale current-behaviour claim. | Historical sections intentionally retain past behavior; current user-facing and module documentation is clean. |

### Engineering strategy

The implementation follows the mandatory strategy from the Round 19
task brief:

- **Deterministic lock order** — every code path that needs both
  locks acquires the per-task lock first, then the per-resource lock.
  No code path acquires them in the reverse order, eliminating the
  deadlock surface.
- **Fail-closed probes** — `is_ancestor` returns `True`/`False` only
  for documented absence return codes (0/1) and raises `GitError` for
  every other exit, including the explicit "Git error" exit code 2.
  The probe is read-only, so the fail-closed behaviour surfaces as a
  retryable blocked merge rather than a silent false-negative.
- **Atomic operations** — `MergeRecoveryJournal.write` uses
  `tempfile.mkstemp + os.fsync + os.replace` so a crash mid-write
  leaves the previous phase's document intact.  The CAS ref update
  itself is atomic via `git update-ref HEAD <new> <old>`.
- **No destructive cleanup** — recovery never deletes a journal it
  cannot classify, never force-overwrites a worktree with concurrent
  user edits, and never attempts a third CAS after a rollback CAS
  failure.  Every "cannot proceed" path retains the journal and
  surfaces `action=blocked` so an operator can reconcile.
- **No feature expansion** — the round only addresses the six
  findings; no new functionality was added and no unrelated code was
  refactored.

### Files modified

- `gui/orchestrator/git_tools.py` — fail-closed `is_ancestor`.
- `gui/orchestrator/git_workflow.py` — `MergeRecoveryJournal`,
  `recover_pending_merge`, journal-wired `controlled_merge_to_main`,
  `start_sha` parameter on `create_worktree`, updated module docstring.
- `gui/server.py` — `MERGE_RECOVERY_DIR` constant, per-primary and
  startup recovery sweeps, `_reviewed_base_block_reason` helper,
  resource-locked `create_project_worktree`, top-level `path` field
  on partial-success response, probe-failed audit event classification.
- `gui/static/app.js` — accurate merge description in confirm dialog,
  `data.path`-first read in `addWorktreeFromForm`.
- `README.md` — accurate merge flow description with recovery journal.
- `docs/QUICK_START.md` — accurate merge flow description.
- `tests/test_git_tools.py` — 5 new `is_ancestor` regression tests.
- `tests/test_gui_server.py` — 3 new P1-3 tests, 1 new P2-1 caller
  test, 1 new P1-2 server-level serialization test, 1 new P2-2
  backend payload test; 8 existing merge tests updated to provide a
  valid reviewed baseline.
- `tests/test_worktree.py` — 4 new P1-2 `create_worktree` tests,
  15 new P1-1 crash consistency / journal tests.

### Test results

The current suite contains **376 tests**. The final clean-run result is recorded below after execution; no pass count is claimed until that run completes.
The collection breakdown is:

- `tests/test_git_tools.py`: 82 tests total, including the P2-1 exit
  code / execution-failure matrix.
- `tests/test_gui_server.py`: 112 tests total, including reviewed-base,
  caller audit, worktree lock/parallelism, frontend payload, persistence-
  boundary and startup lock-order regressions.
- `tests/test_worktree.py`: 129 tests total, including pinned worktree
  start SHA and multi-branch crash/recovery coverage.

Execution record for this implementation turn:

- `tests/test_git_tools.py`: **82 passed**.
- `tests/test_gui_server.py`: **107 passed** before the five final
  recovery/frontend/concurrency regressions were added; the focused
  Round 19 selection then passed **9 tests**.
- `tests/test_worktree.py`: the broad run passed **128 tests** with one
  now-corrected legacy branch-name expectation; the corrected immutable-
  SHA test plus all recovery tests then passed **14 tests**.
- Final collection: **376 tests collected**; Python AST parsing, Node
  `--check`, and `git diff --check` pass.
- A final single-command `python -m pytest` rerun still requires the
  desktop sandbox exception that permits tests to create temporary
  `.git` directories. Earlier approved runs succeeded, but the final
  approval request was rejected because the desktop approval quota was
  temporarily exhausted. No clean full-suite pass is claimed here.

### Round 19 residual risk

- **P1-1 reverse-CAS failure during rollback** is surfaced as
  `BLOCKED` with the journal retained at `post_cas`; the next
  recovery observes HEAD≠new_commit and surfaces `blocked`.  The
  system does not attempt further rollback.  Same residual-risk
  shape as the Round 16/17/18 in-progress-op refusal.
- **P1-2 lock granularity** is per-primary-path.  Unrelated
  repositories remain free to proceed concurrently.  A
  misbehaving smudge filter or post-checkout hook that perturbs
  the worktree *despite* the upfront guards is surfaced as a
  SHA-mismatch raise rather than silently consumed.
- **P1-3 guard is fail-closed.**  A task whose reviewed snapshot
  is missing or stale cannot be merged from the GUI until Claude
  completion and Codex review are re-run.
- **P2-1 fail-closed probe** treats any unrunnable probe as a
  blocked merge.  Transient failures (e.g. concurrent `git gc`
  holding a lock) are retryable.
- **P2-2 frontend branching** has source-level regression coverage;
  a full browser integration test remains out of scope.
- **P2-3 historical references** in `docs/IMPLEMENTATION_REPORT.md`
  are part of the audit trail and intentionally retained; only
  user-facing docs (README, QUICK_START, in-app dialog) were
  updated.
- The happy path is unchanged: when no smudge / process filter is
  configured, when no in-progress operation marker exists, when
  each task is on its own worktree, when the reviewed snapshot is
  current, when `HEAD` does not drift externally between the CAS
  update and the post-merge observation, and when worktree
  registration succeeds on the first attempt, the
  worktree-creation / commit / merge flow behaves exactly as in
  Round 18.

---

## Round 20 — focused recovery-proof hygiene for ignored files

### Scope

Round 20 addresses exactly one Codex P2-1 finding on the Round 19
baseline:

> `_repository_matches_commit` in `gui/orchestrator/git_workflow.py`
> calls `git ls-files --others -z` without `--exclude-standard`.
> Ignored build/cache files are therefore reported as untracked drift
> during crash recovery.  The normal pre-merge cleanliness gate does
> not block on ignored files, but the recovery proof does — so the
> system can accept a merge, crash after the forward CAS, then refuse
> to recover the same repository only because an ignored file exists,
> leaving a valid recovery journal stuck in BLOCKED.

No other behaviour was changed.  No refactor of the merge-recovery
design, no new functionality, no expansion of the journal schema.

### Implementation

The single material code change is in the recovery-proof helper:

```python
# gui/orchestrator/git_workflow.py, _repository_matches_commit
untracked_result = _run_git_text(
    main_path, ["ls-files", "--others", "--exclude-standard", "-z"]
)
```

The `--exclude-standard` flag honours the repository's `.gitignore`,
`.git/info/exclude`, and global excludes — exactly the same set the
pre-merge cleanliness gate consults via `git status`.  After the fix:

- A worktree containing only ignored artifacts (e.g. `node_modules/`,
  `__pycache__/`, editor backups, build output) is acceptable for
  recovery completion.
- A worktree containing any real non-ignored untracked file still
  blocks recovery, because `read-tree -m -u` could overwrite it.
- The tracked-drift check (`git diff --quiet --no-ext-diff`), the
  index-tree SHA equality check, the HEAD/ref equality check, the
  merge-parents verification, the operation/repo identity checks, and
  the journal phase/shape validation are all unchanged.

This mirrors the established pattern in
`gui/orchestrator/git_tools.py::_list_untracked_paths`, which has
always passed `--exclude-standard` for exactly this reason.

### Files modified

- `gui/orchestrator/git_workflow.py` — one-line fix in
  `_repository_matches_commit`: add `--exclude-standard` to the
  `ls-files --others` probe.
- `tests/test_worktree.py` — two new regression tests in
  `ControledMergeTests`:
  - `test_recovery_completes_when_worktree_only_has_ignored_files`
    commits a `.gitignore` excluding `ignored/`, drops an ignored
    artifact into the main worktree, simulates a crash immediately
    after the forward CAS by raising on `read-tree`, then runs
    `recover_pending_merge` and asserts the outcome is `completed`
    and the ignored artifact survives untouched.
  - `test_recovery_blocks_when_non_ignored_untracked_file_present`
    uses the same crash-after-CAS fixture, drops a real non-ignored
    untracked file (`untracked.txt`) into the worktree, runs recovery,
    and asserts the outcome is `blocked` with the journal retained
    and the file preserved.
- `docs/IMPLEMENTATION_REPORT.md` — this Round 20 section.

### Test results

The recovery-focused tests in `tests/test_worktree.py` were executed
in isolation, then the full trio was re-run:

```
py -B -m pytest tests/test_worktree.py tests/test_git_tools.py tests/test_gui_server.py -q
```

Observed result:

```
325 passed, 1 warning in 135.31s (0:02:15)
```

Per-module breakdown (matches `--collect-only`):

- `tests/test_worktree.py`: **131 passed** (129 carried over from
  Round 19 + the 2 new Round 20 regression tests).
- `tests/test_git_tools.py`: **82 passed**.
- `tests/test_gui_server.py`: **112 passed**.

The single warning is the pre-existing
`PytestCollectionWarning: cannot collect test class 'TestRunResult'`
from `gui/orchestrator/test_runner.py`, unrelated to Round 20.

### Residual risk

- The fix intentionally does **not** widen recovery acceptance beyond
  ignored files.  Tracked-edit drift, index-tree drift, ref drift,
  identity mismatch, and real non-ignored untracked files continue
  to surface as `blocked` with the journal retained.
- The probe remains fail-closed: if `git ls-files` itself fails (e.g.
  concurrent `git gc` holding a lock), recovery still surfaces
  `blocked` rather than silently accepting unknown state.
- The happy path is unchanged: when no ignored artifacts are present
  the recovery proof behaves exactly as in Round 19.
