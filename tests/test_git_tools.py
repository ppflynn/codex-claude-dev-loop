import os
import shutil
import stat as stat_module
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui.orchestrator import git_tools
from gui.orchestrator.git_tools import compute_repo_id, list_worktrees


def completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


class GitToolsTests(unittest.TestCase):
    def make_dir(self):
        root = Path(__file__).resolve().parents[1] / ".gui" / "test-tmp" / uuid.uuid4().hex
        root.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        return root

    def make_git_repo(self):
        root = self.make_dir()
        subprocess.run(["git", "-C", str(root), "init"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "test@test.test"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "Test"],
            capture_output=True, check=True,
        )
        (root / "readme.md").write_text("# test\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "readme.md"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-m", "init"],
            capture_output=True, check=True,
        )
        return root

    def make_worktree(self, main_repo, branch="feature/test-wt"):
        wt_path = Path(__file__).resolve().parents[1] / ".gui" / "test-tmp" / f"wt-{uuid.uuid4().hex}"
        subprocess.run(
            ["git", "-C", str(main_repo), "worktree", "add", "-b", branch, str(wt_path)],
            capture_output=True, check=True,
        )
        self.addCleanup(lambda: shutil.rmtree(str(wt_path), ignore_errors=True))
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "-C", str(main_repo), "worktree", "remove", "--force", str(wt_path)],
                capture_output=True,
            )
        )
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "-C", str(main_repo), "branch", "-D", branch],
                capture_output=True,
            )
        )
        return wt_path

    def test_non_git_repo_is_rejected(self):
        with mock.patch("gui.orchestrator.git_tools._run_git", return_value=completed([], 128, "", "not a git repo")):
            with self.assertRaises(git_tools.GitError):
                git_tools.assert_git_work_tree(self.make_dir())

    def test_clean_repo_passes_and_dirty_repo_fails(self):
        root = self.make_dir()
        with mock.patch("gui.orchestrator.git_tools._run_git", return_value=completed([], 0, "")):
            git_tools.assert_clean_work_tree(root)
        with mock.patch("gui.orchestrator.git_tools._run_git", return_value=completed([], 0, "?? new.txt\n")):
            with self.assertRaises(git_tools.DirtyWorkTreeError):
                git_tools.assert_clean_work_tree(root)

    def test_collect_status_and_diff_artifacts(self):
        root = self.make_dir()
        task_dir = self.make_dir() / "task"
        # Call sequence inside ``collect_git_artifacts``:
        #   1. rev-parse --is-inside-work-tree  (assert_git_work_tree)
        #   2. status --short                   (git_status)
        #   3. diff HEAD --name-status -z       (enumerate_changed_paths → has_env_changes)
        #   4. ls-files --others -z             (enumerate_changed_paths → has_env_changes)
        #   5. diff HEAD --stat                 (review artifact)
        #   6. diff HEAD                        (review artifact)
        #   7. ls-files --others -z             (_untracked_files_diff → _list_untracked_paths)
        # ``-z`` output is NUL-terminated so paths containing non-ASCII or
        # quoted components survive verbatim and the ``.env`` segment check
        # receives a clean path (no trailing newline).
        responses = [
            completed([], 0, "true\n"),
            completed([], 0, " M src/app.py\n"),
            completed([], 0, ""),
            completed([], 0, ""),
            completed([], 0, " src/app.py | 2 +-\n"),
            completed([], 0, "diff --git a/src/app.py b/src/app.py\n"),
            completed([], 0, ""),
        ]
        with mock.patch("gui.orchestrator.git_tools._run_git", side_effect=responses):
            artifacts = git_tools.collect_git_artifacts(root, task_dir, 1)
        self.assertTrue(artifacts.status_path.exists())
        self.assertTrue(artifacts.diff_stat_path.exists())
        self.assertTrue(artifacts.diff_path.exists())

    def test_collect_includes_untracked_text_files_in_diff_artifact(self):
        root = self.make_dir()
        task_dir = self.make_dir() / "task"
        new_file = root / "vscode-extension" / "src" / "extension.ts"
        new_file.parent.mkdir(parents=True)
        new_file.write_text("export function activate() {}\n", encoding="utf-8")
        # See ``test_collect_status_and_diff_artifacts`` for the call sequence.
        # Both ``ls-files --others -z`` invocations must return the untracked
        # path NUL-terminated so the untracked-diff fold reads the actual file
        # from disk and the ``.env`` segment check sees a clean path.
        untracked_nul = "vscode-extension/src/extension.ts\0"
        responses = [
            completed([], 0, "true\n"),
            completed([], 0, "?? vscode-extension/src/extension.ts\n"),
            completed([], 0, ""),
            completed([], 0, untracked_nul),
            completed([], 0, ""),
            completed([], 0, ""),
            completed([], 0, untracked_nul),
        ]
        with mock.patch("gui.orchestrator.git_tools._run_git", side_effect=responses):
            artifacts = git_tools.collect_git_artifacts(root, task_dir, 1)

        diff = artifacts.diff_path.read_text(encoding="utf-8")
        diff_stat = artifacts.diff_stat_path.read_text(encoding="utf-8")
        self.assertIn("Untracked files included for review", diff)
        self.assertIn("diff --git a/vscode-extension/src/extension.ts", diff)
        self.assertIn("+export function activate() {}", diff)
        self.assertIn("vscode-extension/src/extension.ts", diff_stat)

    def test_env_change_blocks_diff_content(self):
        root = self.make_dir()
        task_dir = self.make_dir() / "task"
        # ``has_env_changes`` calls ``enumerate_changed_paths`` which runs
        # ``diff HEAD --name-status -z`` then ``ls-files --others -z``.  The
        # ``-z`` output is NUL-terminated (not newline-terminated), so the
        # ``.env`` path must be emitted as ``.env\0``; otherwise
        # ``path_has_env_segment`` would see a trailing newline and miss the
        # ``.env`` segment, letting the change sail past the guard.
        responses = [
            completed([], 0, "true\n"),
            completed([], 0, "?? .env\n"),
            completed([], 0, ""),
            completed([], 0, ".env\0"),
        ]
        with mock.patch("gui.orchestrator.git_tools._run_git", side_effect=responses):
            with self.assertRaises(git_tools.EnvFileChangedError):
                git_tools.collect_git_artifacts(root, task_dir, 1)
        self.assertNotIn("SECRET=1", (task_dir / "git_diff_round_1.diff").read_text(encoding="utf-8"))

    def test_env_change_in_nested_untracked_directory_blocks_diff_content(self):
        # ``git status --short`` may collapse an entirely-untracked directory
        # to ``?? dir/``.  ``has_env_changes`` must still catch a nested
        # ``dir/.env`` via ``git ls-files --others -z``, which emits the
        # nested path NUL-terminated (``config/.env\0``) rather than the
        # collapsed ``?? config/`` form.
        root = self.make_dir()
        task_dir = self.make_dir() / "task"
        responses = [
            completed([], 0, "true\n"),
            completed([], 0, "?? config/\n"),
            completed([], 0, ""),
            completed([], 0, "config/.env\0"),
        ]
        with mock.patch("gui.orchestrator.git_tools._run_git", side_effect=responses):
            with self.assertRaises(git_tools.EnvFileChangedError):
                git_tools.collect_git_artifacts(root, task_dir, 1)
        self.assertNotIn(
            "SECRET=1",
            (task_dir / "git_diff_round_1.diff").read_text(encoding="utf-8"),
        )

    def test_compute_review_snapshot_includes_staged_content(self):
        repo = self.make_git_repo()
        # Staging a new file must change the snapshot's diff hash so that
        # drift detection catches any unreviewed staged content.
        before = git_tools.compute_review_snapshot(repo)
        (repo / "staged.txt").write_text("staged content\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repo), "add", "staged.txt"],
            capture_output=True,
            check=True,
        )
        after = git_tools.compute_review_snapshot(repo)
        self.assertNotEqual(before["diffHash"], after["diffHash"])
        # HEAD has not moved; only the diff hash should differ.
        self.assertEqual(before["headSha"], after["headSha"])

    def test_compute_review_snapshot_detects_large_untracked_drift(self):
        # Regression test for Codex P1-2 round 4: ``_untracked_files_diff``
        # skips files > MAX_UNTRACKED_FILE_BYTES for the review diff display,
        # but ``compute_review_snapshot`` must hash the actual bytes so a
        # post-review edit to a large untracked file is detected.
        repo = self.make_git_repo()
        large_payload_a = b"x" * (git_tools.MAX_UNTRACKED_FILE_BYTES + 1024)
        large_payload_b = b"y" * (git_tools.MAX_UNTRACKED_FILE_BYTES + 1024)
        (repo / "large.bin").write_bytes(large_payload_a)
        before = git_tools.compute_review_snapshot(repo)
        (repo / "large.bin").write_bytes(large_payload_b)
        after = git_tools.compute_review_snapshot(repo)
        self.assertNotEqual(before["diffHash"], after["diffHash"])

    def test_compute_review_snapshot_detects_binary_untracked_drift(self):
        # Binary files are skipped in the review diff (they can't be
        # rendered) but must still contribute their bytes to the snapshot
        # hash so a post-review change is detected.
        repo = self.make_git_repo()
        (repo / "blob.dat").write_bytes(b"\x00\x01\x02\x03 binary")
        before = git_tools.compute_review_snapshot(repo)
        (repo / "blob.dat").write_bytes(b"\x00\x01\x02\x03 BINARY")
        after = git_tools.compute_review_snapshot(repo)
        self.assertNotEqual(before["diffHash"], after["diffHash"])

    def test_compute_review_snapshot_stable_when_unchanged(self):
        repo = self.make_git_repo()
        (repo / "extra.txt").write_text("hello\n", encoding="utf-8")
        a = git_tools.compute_review_snapshot(repo)
        b = git_tools.compute_review_snapshot(repo)
        self.assertEqual(a, b)

    def test_compute_review_snapshot_stable_across_staging(self):
        # Regression for Codex P1-2 round 8: the snapshot's ``diffHash``
        # is computed from file bytes on disk (via
        # ``_hash_changed_paths_bytes``) rather than from ``git diff HEAD``
        # text + untracked-byte hash.  This makes the digest invariant
        # across ``git add -A``: a file that was untracked at artifact
        # time and is staged at commit time produces the same hash, so
        # the post-stage drift check can compare against the reviewed
        # snapshot without false positives.
        repo = self.make_git_repo()
        (repo / "tracked_change.txt").write_text("modified\n", encoding="utf-8")
        (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        before = git_tools.compute_review_snapshot(repo)
        subprocess.run(
            ["git", "-C", str(repo), "add", "-A"],
            capture_output=True, check=True,
        )
        after = git_tools.compute_review_snapshot(repo)
        self.assertEqual(
            before["diffHash"],
            after["diffHash"],
            "diffHash must be stable across git add -A so the post-stage "
            "drift check does not produce false positives",
        )

    def test_compute_review_snapshot_blocks_when_untracked_env_present(self):
        # Regression for Codex P1-1 round 7: ``compute_review_snapshot`` must
        # refuse to read or hash untracked ``.env`` bytes.  An untracked
        # ``.env`` introduced between artifact collection and PASS-time
        # verification must raise ``EnvFileChangedError`` before any content
        # is read.
        repo = self.make_git_repo()
        (repo / ".env").write_text("SECRET=leaked\n", encoding="utf-8")
        with self.assertRaises(git_tools.EnvFileChangedError) as ctx:
            git_tools.compute_review_snapshot(repo)
        self.assertIn(".env", str(ctx.exception))

    def test_compute_review_snapshot_blocks_when_tracked_env_present(self):
        # ``git diff HEAD`` would dump tracked ``.env`` content into the diff
        # text.  The path-safety guard must run BEFORE that call.
        repo = self.make_git_repo()
        (repo / ".env").write_text("SECRET=baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", ".env"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "track env"],
            capture_output=True,
            check=True,
        )
        (repo / ".env").write_text("SECRET=rotated\n", encoding="utf-8")
        with self.assertRaises(git_tools.EnvFileChangedError) as ctx:
            git_tools.compute_review_snapshot(repo)
        self.assertIn(".env", str(ctx.exception))

    def test_compute_review_snapshot_blocks_before_reading_env_bytes(self):
        # The path-safety guard must run BEFORE ``_hash_changed_paths_bytes``
        # (which reads every staged file's bytes — tracked and untracked
        # alike — now that compute_review_snapshot hashes content from disk
        # rather than from the diff text).  If the guard is in the wrong
        # order the mock fires and the test fails with AssertionError.
        repo = self.make_git_repo()
        (repo / ".env").write_text("SECRET=leaked\n", encoding="utf-8")
        with mock.patch(
            "gui.orchestrator.git_tools._hash_changed_paths_bytes",
            side_effect=AssertionError("must not hash bytes when .env present"),
        ):
            with self.assertRaises(git_tools.EnvFileChangedError):
                git_tools.compute_review_snapshot(repo)

    def test_enumerate_changed_paths_lists_nested_untracked_env(self):
        repo = self.make_git_repo()
        nested = repo / "secrets" / "nested"
        nested.mkdir(parents=True)
        (nested / ".env").write_text("SECRET=1\n", encoding="utf-8")
        (repo / "tracked_change.py").write_text("print('change')\n", encoding="utf-8")
        paths = git_tools.enumerate_changed_paths(repo)
        self.assertIn("secrets/nested/.env", paths)
        self.assertIn("tracked_change.py", paths)

    # ------------------------------------------------------------------
    # Symlink / lstat-mode regression coverage (Codex P1-1 / P2-1 round 9).
    # ------------------------------------------------------------------

    def _skip_if_no_symlinks(self):
        """Skip the test when the OS / privileges refuse to create symlinks."""
        probe = self.make_dir() / "probe"
        target = probe / "target.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
        link = probe / "link.txt"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks not supported on this platform: {exc}")

    def test_compute_review_snapshot_blocks_when_untracked_symlink_targets_env(self):
        # Codex P1-1 round 9: a benign-named untracked symlink whose stored
        # target string references ``.env`` must trip the path-safety guard
        # before the hasher reads the destination's bytes.  Without the
        # symlink-aware guard the previous implementation would resolve
        # ``link.txt`` to ``.env`` and silently hash the secret bytes.
        self._skip_if_no_symlinks()
        repo = self.make_git_repo()
        (repo / ".env").write_text("SECRET=leaked\n", encoding="utf-8")
        (repo / "link.txt").symlink_to(repo / ".env")
        with self.assertRaises(git_tools.EnvFileChangedError) as ctx:
            git_tools.compute_review_snapshot(repo)
        message = str(ctx.exception)
        # The error must surface the symlink itself AND its target so the
        # user can see what was rejected.
        self.assertIn("link.txt", message)
        self.assertIn(".env", message)

    def test_compute_review_snapshot_blocks_when_symlink_targets_tracked_env(self):
        # Variant where the env file is already tracked and clean (so it
        # does not appear in ``enumerate_changed_paths`` directly).  Only
        # the untracked symlink exposes the secret; the previous guard
        # would not see it because the link's name is benign.
        self._skip_if_no_symlinks()
        repo = self.make_git_repo()
        (repo / ".env").write_text("SECRET=baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", ".env"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "track env"],
            capture_output=True,
            check=True,
        )
        # ``.env`` is now committed and clean; the only new path Git
        # would stage is the symlink itself.
        (repo / "link.txt").symlink_to(repo / ".env")
        with self.assertRaises(git_tools.EnvFileChangedError) as ctx:
            git_tools.compute_review_snapshot(repo)
        self.assertIn("link.txt", str(ctx.exception))
        self.assertIn(".env", str(ctx.exception))

    def test_compute_review_snapshot_blocks_before_reading_env_through_symlink(self):
        # Order invariant: the symlink-aware env guard must run BEFORE
        # ``_hash_changed_paths_bytes`` reads any file bytes.  Otherwise
        # the hasher would follow the link and read the secret before the
        # guard raised.  The mock fails the test if the hasher is reached.
        self._skip_if_no_symlinks()
        repo = self.make_git_repo()
        (repo / ".env").write_text("SECRET=leaked\n", encoding="utf-8")
        (repo / "link.txt").symlink_to(repo / ".env")
        with mock.patch(
            "gui.orchestrator.git_tools._hash_changed_paths_bytes",
            side_effect=AssertionError("must not hash bytes when symlink targets .env"),
        ):
            with self.assertRaises(git_tools.EnvFileChangedError):
                git_tools.compute_review_snapshot(repo)

    def test_hash_changed_paths_bytes_does_not_follow_symlink(self):
        # Two symlinks with the same name but different target strings must
        # produce different hashes — even when the destinations share the
        # same bytes.  This proves the hasher uses the link target string
        # (as Git stages it) instead of reading through the link.
        self._skip_if_no_symlinks()
        repo = self.make_git_repo()
        target_a = repo / "a.txt"
        target_b = repo / "b.txt"
        target_a.write_text("same payload\n", encoding="utf-8")
        target_b.write_text("same payload\n", encoding="utf-8")
        link = repo / "link.txt"
        link.symlink_to(target_a)
        before = git_tools._hash_changed_paths_bytes(repo)
        link.unlink()
        link.symlink_to(target_b)
        after = git_tools._hash_changed_paths_bytes(repo)
        self.assertNotEqual(
            before,
            after,
            "symlink target swap must perturb the hash even when the "
            "destination bytes are identical",
        )

    def test_hash_changed_paths_bytes_detects_mode_drift(self):
        # Codex P2-1 round 9: a post-review chmod must perturb the hash
        # so the drift check refuses to commit an unreviewed mode change.
        repo = self.make_git_repo()
        script = repo / "script.sh"
        script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        before = git_tools._hash_changed_paths_bytes(repo)
        original_mode = script.lstat().st_mode
        try:
            os.chmod(script, original_mode ^ stat_module.S_IWUSR)
        except OSError as exc:
            self.skipTest(f"os.chmod unavailable on this platform: {exc}")
        new_mode = script.lstat().st_mode
        if new_mode == original_mode:
            self.skipTest("os.chmod did not change lstat mode on this platform")
        try:
            after = git_tools._hash_changed_paths_bytes(repo)
            self.assertNotEqual(
                before,
                after,
                "chmod must change the hash so mode-only drift is detected",
            )
        finally:
            try:
                os.chmod(script, original_mode)
            except OSError:
                pass

    def test_compute_review_snapshot_detects_mode_drift(self):
        # End-to-end variant of the mode-drift test through the public
        # snapshot API.  HEAD and content are unchanged; only the mode
        # bit flips, but the snapshot's diffHash must still change so
        # ``controlled_commit``'s drift check rejects the commit.
        repo = self.make_git_repo()
        script = repo / "script.sh"
        script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        before = git_tools.compute_review_snapshot(repo)
        original_mode = script.lstat().st_mode
        try:
            os.chmod(script, original_mode ^ stat_module.S_IWUSR)
        except OSError as exc:
            self.skipTest(f"os.chmod unavailable on this platform: {exc}")
        new_mode = script.lstat().st_mode
        if new_mode == original_mode:
            self.skipTest("os.chmod did not change lstat mode on this platform")
        try:
            after = git_tools.compute_review_snapshot(repo)
            self.assertEqual(before["headSha"], after["headSha"])
            self.assertNotEqual(before["diffHash"], after["diffHash"])
        finally:
            try:
                os.chmod(script, original_mode)
            except OSError:
                pass

    def test_has_env_changes_detects_symlink_target(self):
        # Direct check on the public helper so ``collect_git_artifacts``
        # also benefits from the symlink-aware guard.
        self._skip_if_no_symlinks()
        repo = self.make_git_repo()
        (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
        (repo / "link.txt").symlink_to(repo / ".env")
        self.assertTrue(git_tools.has_env_changes(repo))

    def test_untracked_file_diff_renders_symlink_target_without_following(self):
        # Renderer invariant: an untracked symlink must appear in the
        # review diff with ``mode 120000`` and its target string as the
        # blob content.  The destination's bytes must NOT leak through.
        self._skip_if_no_symlinks()
        repo = self.make_git_repo()
        secret = repo / "secret.txt"
        secret.write_text("TOPSECRET\n", encoding="utf-8")
        (repo / "link.txt").symlink_to(secret)
        diff_text, _ = git_tools._untracked_file_diff(repo, "link.txt")
        self.assertIn("new file mode 120000", diff_text)
        self.assertIn("secret.txt", diff_text)
        self.assertNotIn("TOPSECRET", diff_text)

    # ------------------------------------------------------------------
    # Codex P1-1 / P1-2 round 14 regression coverage.
    # ------------------------------------------------------------------

    def test_compute_review_snapshot_raises_when_head_resolution_fails(self):
        # Codex P1-1 round 14: when ``git rev-parse HEAD`` fails, the
        # snapshot must NOT silently return ``headSha=None``.  A ``None``
        # HEAD would let downstream callers skip the HEAD drift check,
        # the CAS ref update, and the merge base reachability check,
        # allowing unreviewed history to slip into the trunk.
        repo = self.make_git_repo()
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        original_run_git = git_tools._run_git
        with mock.patch(
            "gui.orchestrator.git_tools._run_git",
            side_effect=lambda path, args: completed(
                args, 128, "", "fatal: not a git repository"
            )
            if args[:2] == ["rev-parse", "HEAD"]
            else original_run_git(path, args),
        ):
            with self.assertRaises(git_tools.GitError) as ctx:
                git_tools.compute_review_snapshot(repo)
        message = str(ctx.exception)
        # The error message should surface the underlying Git failure
        # so callers can see *why* HEAD resolution broke, not just that
        # it did.
        self.assertTrue(message, "GitError message must be non-empty")
        self.assertIn("not a git repository", message)

    def test_compute_review_snapshot_raises_when_head_sha_is_empty(self):
        # Variant: ``git rev-parse HEAD`` succeeds but returns an empty
        # string (e.g. unborn branch in a freshly-init'd repo).  The
        # snapshot must still refuse rather than returning ``headSha=None``.
        repo = self.make_git_repo()
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        original_run_git = git_tools._run_git
        with mock.patch(
            "gui.orchestrator.git_tools._run_git",
            side_effect=lambda path, args: completed(args, 0, "\n", "")
            if args[:2] == ["rev-parse", "HEAD"]
            else original_run_git(path, args),
        ):
            with self.assertRaises(git_tools.GitError):
                git_tools.compute_review_snapshot(repo)

    def test_compute_review_snapshot_blocks_when_clean_filter_configured(self):
        # Codex P1-2 round 14: a clean filter configured on a changed
        # path can transform content during ``git add``.  The Codex
        # review artifacts are built from raw worktree bytes, so Codex
        # would never see the transformed content.  The snapshot must
        # refuse to compute so the safety boundary fails closed.
        repo = self.make_git_repo()
        # Configure a deterministic clean filter that uppercases content.
        subprocess.run(
            ["git", "-C", str(repo), "config", "filter.uc.clean", "tr a-z A-Z"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "filter.uc.smudge", "cat"],
            capture_output=True, check=True,
        )
        (repo / ".gitattributes").write_text("feature.py filter=uc\n", encoding="utf-8")
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        with self.assertRaises(git_tools.GitError) as ctx:
            git_tools.compute_review_snapshot(repo)
        message = str(ctx.exception)
        self.assertIn("feature.py", message)
        self.assertIn("filter", message.lower())

    def test_compute_review_snapshot_blocks_when_process_filter_configured(self):
        # Variant where the filter is a long-running ``process`` filter
        # instead of a one-shot ``clean`` command.  Both can transform
        # content during staging; both must be rejected.
        repo = self.make_git_repo()
        subprocess.run(
            ["git", "-C", str(repo), "config", "filter.uc.clean", "tr a-z A-Z"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "filter.uc.process", "cat"],
            capture_output=True, check=True,
        )
        (repo / ".gitattributes").write_text("feature.py filter=uc\n", encoding="utf-8")
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        with self.assertRaises(git_tools.GitError):
            git_tools.compute_review_snapshot(repo)

    def test_compute_review_snapshot_allows_when_filter_attribute_without_driver(self):
        # A filter attribute that has no matching ``clean`` or ``process``
        # config is a no-op (Git applies identity).  The snapshot must
        # compute normally so legitimate attribute configurations do not
        # break the review flow.
        repo = self.make_git_repo()
        (repo / ".gitattributes").write_text("feature.py filter=uc\n", encoding="utf-8")
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        snapshot = git_tools.compute_review_snapshot(repo)
        self.assertIsNotNone(snapshot["headSha"])
        self.assertTrue(snapshot["treeSha"])

    def test_find_clean_filtered_paths_detects_active_clean_filter(self):
        repo = self.make_git_repo()
        subprocess.run(
            ["git", "-C", str(repo), "config", "filter.uc.clean", "tr a-z A-Z"],
            capture_output=True, check=True,
        )
        (repo / ".gitattributes").write_text("feature.py filter=uc\n", encoding="utf-8")
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        paths = git_tools.find_clean_filtered_paths(repo)
        self.assertIn("feature.py", paths)

    def test_find_clean_filtered_paths_returns_empty_when_no_changes(self):
        repo = self.make_git_repo()
        # Repo is clean; no paths to check.
        self.assertEqual(git_tools.find_clean_filtered_paths(repo), [])

    def test_find_clean_filtered_paths_returns_empty_when_filter_unspecified(self):
        repo = self.make_git_repo()
        # No filter attribute configured at all.
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        self.assertEqual(git_tools.find_clean_filtered_paths(repo), [])

    def test_find_clean_filtered_paths_ignores_attribute_without_driver(self):
        # Filter attribute declared but no clean/process config: not a
        # violation because Git applies identity.
        repo = self.make_git_repo()
        (repo / ".gitattributes").write_text("feature.py filter=uc\n", encoding="utf-8")
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        self.assertEqual(git_tools.find_clean_filtered_paths(repo), [])

    def test_find_clean_filtered_paths_detects_non_ascii_path(self):
        # Codex P1-1 round 15 regression: ``git check-attr`` without
        # ``-z`` emits C-quoted paths for non-ASCII components
        # (``"dir-\303\251/file.txt"``).  The previous implementation's
        # ``unicode_escape`` approximation produced a different string
        # that no longer matched the file on disk, so the clean filter
        # escaped detection.  Using ``-z`` + NUL-triple parsing preserves
        # the actual path bytes (UTF-8 decoded by ``_run_git``).
        repo = self.make_git_repo()
        subprocess.run(
            ["git", "-C", str(repo), "config", "filter.uc.clean", "tr a-z A-Z"],
            capture_output=True, check=True,
        )
        # Use ``.gitattributes`` patterns so both the non-ASCII file and
        # a benign control file are covered.
        (repo / ".gitattributes").write_text(
            "feature.py filter=uc\n"
            "dir-é/*.txt filter=uc\n",
            encoding="utf-8",
        )
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        non_ascii_dir = repo / "dir-é"
        non_ascii_dir.mkdir(parents=True, exist_ok=True)
        (non_ascii_dir / "secret.txt").write_text(
            "sensitive\n", encoding="utf-8",
        )
        paths = git_tools.find_clean_filtered_paths(repo)
        self.assertIn("feature.py", paths)
        self.assertIn(
            "dir-é/secret.txt",
            paths,
            "non-ASCII path with active clean filter must be detected; "
            "got: " + ", ".join(paths),
        )

    def test_find_clean_filtered_paths_detects_path_with_spaces(self):
        # Variant: spaces in the path force C-quoting in the default
        # ``check-attr`` output.  ``-z`` parsing preserves the spaces.
        # In ``.gitattributes`` syntax, a path containing spaces must be
        # wrapped in double quotes — the space is the field separator
        # and cannot be escaped with a backslash.
        repo = self.make_git_repo()
        subprocess.run(
            ["git", "-C", str(repo), "config", "filter.uc.clean", "tr a-z A-Z"],
            capture_output=True, check=True,
        )
        (repo / ".gitattributes").write_text(
            '"with space.py" filter=uc\n', encoding="utf-8",
        )
        (repo / "with space.py").write_text("print('hi')\n", encoding="utf-8")
        paths = git_tools.find_clean_filtered_paths(repo)
        self.assertIn("with space.py", paths)

    def test_find_smudge_filtered_paths_detects_active_smudge_filter(self):
        # Codex P1-2 round 18: a smudge filter configured on a path
        # transforms content during checkout (``git worktree add``,
        # ``git read-tree -u`` after a merge).  Mirrors the clean-side
        # detection but checks ``filter.<name>.smudge`` and
        # ``filter.<name>.process`` instead of ``.clean``.
        repo = self.make_git_repo()
        subprocess.run(
            ["git", "-C", str(repo), "config", "filter.lc.smudge", "tr A-Z a-z"],
            capture_output=True, check=True,
        )
        (repo / ".gitattributes").write_text("feature.py filter=lc\n", encoding="utf-8")
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        paths = git_tools.find_smudge_filtered_paths(repo)
        self.assertIn("feature.py", paths)

    def test_find_smudge_filtered_paths_detects_active_process_filter(self):
        # A long-running ``process`` filter is bidirectional: it acts as
        # both clean and smudge.  ``find_smudge_filtered_paths`` must
        # detect it the same way ``find_clean_filtered_paths`` does.
        repo = self.make_git_repo()
        subprocess.run(
            ["git", "-C", str(repo), "config", "filter.lc.process", "cat"],
            capture_output=True, check=True,
        )
        (repo / ".gitattributes").write_text("feature.py filter=lc\n", encoding="utf-8")
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        paths = git_tools.find_smudge_filtered_paths(repo)
        self.assertIn("feature.py", paths)

    def test_find_smudge_filtered_paths_returns_empty_when_filter_unspecified(self):
        repo = self.make_git_repo()
        # No filter attribute configured at all.
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        self.assertEqual(git_tools.find_smudge_filtered_paths(repo), [])

    def test_find_smudge_filtered_paths_ignores_attribute_without_driver(self):
        # Filter attribute declared but no smudge / process config: not
        # a violation because Git applies identity on checkout.
        repo = self.make_git_repo()
        (repo / ".gitattributes").write_text("feature.py filter=lc\n", encoding="utf-8")
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        self.assertEqual(git_tools.find_smudge_filtered_paths(repo), [])

    def test_find_smudge_filtered_paths_ignores_clean_only_filter(self):
        # A clean-only filter is active on the commit side but not on
        # the checkout side; ``find_smudge_filtered_paths`` must NOT
        # report it because the worktree-creation / merge-materialisation
        # paths never invoke the clean side.
        repo = self.make_git_repo()
        subprocess.run(
            ["git", "-C", str(repo), "config", "filter.lc.clean", "tr a-z A-Z"],
            capture_output=True, check=True,
        )
        (repo / ".gitattributes").write_text("feature.py filter=lc\n", encoding="utf-8")
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        self.assertEqual(git_tools.find_smudge_filtered_paths(repo), [])

    def test_find_smudge_filtered_paths_returns_empty_for_empty_input(self):
        repo = self.make_git_repo()
        # Repo is clean; no paths to check.
        self.assertEqual(git_tools.find_smudge_filtered_paths(repo), [])

    def test_find_smudge_filtered_paths_detects_non_ascii_path(self):
        # Non-ASCII paths must round-trip through the ``-z`` parser.
        repo = self.make_git_repo()
        subprocess.run(
            ["git", "-C", str(repo), "config", "filter.lc.smudge", "tr A-Z a-z"],
            capture_output=True, check=True,
        )
        (repo / ".gitattributes").write_text(
            "feature.py filter=lc\n"
            "dir-é/*.txt filter=lc\n",
            encoding="utf-8",
        )
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        non_ascii_dir = repo / "dir-é"
        non_ascii_dir.mkdir(parents=True, exist_ok=True)
        (non_ascii_dir / "secret.txt").write_text("HELLO\n", encoding="utf-8")
        paths = git_tools.find_smudge_filtered_paths(repo)
        self.assertIn("feature.py", paths)
        self.assertIn(
            "dir-é/secret.txt",
            paths,
            "non-ASCII path with active smudge filter must be detected; "
            "got: " + ", ".join(paths),
        )

    def test_find_smudge_filtered_paths_with_candidate_list(self):
        # When called with an explicit candidate list (used by
        # ``controlled_merge_to_main`` for merge-affected paths), only
        # candidates with an active smudge / process filter are
        # returned, and unrelated paths are ignored.
        repo = self.make_git_repo()
        subprocess.run(
            ["git", "-C", str(repo), "config", "filter.lc.smudge", "tr A-Z a-z"],
            capture_output=True, check=True,
        )
        (repo / ".gitattributes").write_text("feature.py filter=lc\n", encoding="utf-8")
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        (repo / "other.py").write_text("print('other')\n", encoding="utf-8")
        paths = git_tools.find_smudge_filtered_paths(repo, ["feature.py", "other.py"])
        self.assertEqual(paths, ["feature.py"])

    def test_find_smudge_filtered_paths_fails_closed_on_config_error(self):
        # Codex P1-5 round 17 / round 18 invariant: corrupt config
        # must surface as ``GitError`` rather than silently degrading
        # the safety boundary.
        repo = self.make_git_repo()
        subprocess.run(
            ["git", "-C", str(repo), "config", "filter.lc.smudge", "tr A-Z a-z"],
            capture_output=True, check=True,
        )
        (repo / ".gitattributes").write_text("feature.py filter=lc\n", encoding="utf-8")
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")

        def fake_run(_path, args):
            if args and args[:3] == ["config", "--get", "filter.lc.smudge"]:
                return completed(args, 128, "", "fatal: bad config line 1")
            return real_run_git(_path, args)

        real_run_git = git_tools._run_git
        with mock.patch("gui.orchestrator.git_tools._run_git", side_effect=fake_run):
            with self.assertRaises(git_tools.GitError):
                git_tools.find_smudge_filtered_paths(repo)

    def test_source_does_not_execute_forbidden_git_commands(self):
        source = (Path(__file__).resolve().parents[1] / "gui" / "orchestrator" / "git_tools.py").read_text(encoding="utf-8")
        for forbidden in ('"commit"', '"push"', '"reset"', '"clean"', '"checkout"', '"switch"', '"restore"'):
            self.assertNotIn(forbidden, source)

    def test_compute_repo_id_is_stable_across_worktrees(self):
        main_repo = self.make_git_repo()
        wt = self.make_worktree(main_repo, branch="feature/repo-id-test")
        main_common = git_tools.get_git_common_dir(main_repo)
        wt_common = git_tools.get_git_common_dir(wt)
        self.assertEqual(compute_repo_id(main_common), compute_repo_id(wt_common))
        self.assertIsNotNone(compute_repo_id(main_common))
        self.assertTrue(compute_repo_id(main_common).startswith("repo_"))

    def test_compute_repo_id_none_for_empty(self):
        self.assertIsNone(compute_repo_id(None))
        self.assertIsNone(compute_repo_id(""))

    def test_list_worktrees_returns_primary_and_worktree(self):
        main_repo = self.make_git_repo()
        wt = self.make_worktree(main_repo, branch="feature/list-test")
        entries = list_worktrees(main_repo)
        paths = [e.path for e in entries]
        types = {e.path: e.type for e in entries}
        self.assertIn(str(main_repo), [str(Path(p)) for p in paths])
        self.assertIn(str(wt), [str(Path(p)) for p in paths])
        self.assertEqual(types[entries[0].path], "primary")
        worktree_entries = [e for e in entries if e.type == "worktree"]
        self.assertTrue(any(Path(e.path) == wt for e in worktree_entries))

    def test_list_worktrees_invoked_from_worktree_still_lists_main(self):
        main_repo = self.make_git_repo()
        wt = self.make_worktree(main_repo, branch="feature/reverse-list")
        entries = list_worktrees(wt)
        paths = [str(Path(e.path)) for e in entries]
        self.assertIn(str(main_repo), paths)
        self.assertIn(str(wt), paths)

    def test_list_worktrees_returns_empty_for_non_git(self):
        root = self.make_dir()
        with mock.patch("gui.orchestrator.git_tools._run_git", return_value=completed([], 128, "", "not a git repo")):
            self.assertEqual(list_worktrees(root), [])

    # ------------------------------------------------------------------
    # Submodule / gitlink drift regression coverage (Codex P1-2 round 11).
    # ------------------------------------------------------------------

    def _skip_if_no_submodules(self):
        """Skip the test when the host cannot create a real submodule.

        Constructing a submodule requires ``git submodule add`` to clone
        from a reachable source.  We probe by attempting a minimal
        submodule setup; if it fails for any reason (no git, sandbox
        blocks ``protocol.file.allow``, missing privileges), the test is
        skipped rather than failing.
        """
        probe_parent = self.make_dir()
        probe_sub = self.make_dir()
        try:
            for repo in (probe_sub, probe_parent):
                subprocess.run(["git", "-C", str(repo), "init"], capture_output=True, check=True)
                subprocess.run(
                    ["git", "-C", str(repo), "config", "user.email", "t@t"],
                    capture_output=True, check=True,
                )
                subprocess.run(
                    ["git", "-C", str(repo), "config", "user.name", "t"],
                    capture_output=True, check=True,
                )
            (probe_sub / "f").write_text("x", encoding="utf-8")
            subprocess.run(["git", "-C", str(probe_sub), "add", "f"], capture_output=True, check=True)
            subprocess.run(["git", "-C", str(probe_sub), "commit", "-m", "i"], capture_output=True, check=True)
            subprocess.run(
                [
                    "git", "-C", str(probe_parent),
                    "-c", "protocol.file.allow=always",
                    "submodule", "add", str(probe_sub), "sub",
                ],
                capture_output=True, check=True,
            )
            subprocess.run(
                ["git", "-C", str(probe_parent), "commit", "-m", "add sub"],
                capture_output=True, check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            self.skipTest(f"host cannot create submodules: {exc}")
        finally:
            shutil.rmtree(str(probe_parent), ignore_errors=True)
            shutil.rmtree(str(probe_sub), ignore_errors=True)

    def _make_repo_with_submodule(self):
        """Create a parent repo with one initialized submodule.

        Returns ``(parent_repo, submodule_source_repo)``.  The submodule
        source has at least one commit so tests can advance it to a
        fresh commit and update the parent's working-tree submodule
        HEAD to detect drift.
        """
        parent_repo = self.make_git_repo()
        submodule_source = self.make_dir()
        subprocess.run(
            ["git", "-C", str(submodule_source), "init"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(submodule_source), "config", "user.email", "t@t"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(submodule_source), "config", "user.name", "t"],
            capture_output=True, check=True,
        )
        (submodule_source / "f").write_text("v1\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(submodule_source), "add", "f"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(submodule_source), "commit", "-m", "v1"],
            capture_output=True, check=True,
        )
        subprocess.run(
            [
                "git", "-C", str(parent_repo),
                "-c", "protocol.file.allow=always",
                "submodule", "add", str(submodule_source), "sub",
            ],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(parent_repo), "commit", "-m", "add sub"],
            capture_output=True, check=True,
        )
        return parent_repo, submodule_source

    def _advance_submodule(self, parent_repo, submodule_source, content):
        """Add a fresh commit to ``submodule_source`` and fast-forward the
        parent's submodule working-tree HEAD to it.  This leaves the
        parent's index pointer unchanged so the submodule appears as
        ``M sub`` in ``git status`` and ``git diff HEAD``.
        """
        (submodule_source / "f").write_text(content, encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(submodule_source), "add", "f"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(submodule_source), "commit", "-m", content],
            capture_output=True, check=True,
        )
        subprocess.run(
            [
                "git", "-C", str(parent_repo / "sub"),
                "-c", "protocol.file.allow=always",
                "pull", str(submodule_source), "HEAD",
            ],
            capture_output=True, check=True,
        )

    def test_get_tracked_path_modes_returns_gitlink_for_submodule(self):
        # ``git ls-files --stage`` emits mode ``160000`` for gitlink
        # entries.  ``get_tracked_path_modes`` must surface that so the
        # hasher can branch into the submodule-aware path.
        self._skip_if_no_submodules()
        parent, _ = self._make_repo_with_submodule()
        modes = git_tools.get_tracked_path_modes(parent)
        self.assertEqual(modes.get("sub"), git_tools.GITLINK_MODE)

    def test_hash_changed_paths_bytes_detects_submodule_pointer_drift(self):
        # Codex P1-2 round 11 regression: when a submodule pointer
        # changes from commit B to commit C while the parent worktree
        # is *already* dirty on the submodule, the directory's lstat
        # mode, the ``git status --short`` text, and the staged set
        # all stay the same — only the submodule's working-tree HEAD
        # SHA flips.  Without explicit gitlink handling the previous
        # hasher would emit a constant ``"not-a-regular-file"`` marker
        # for both states and the drift check would pass, letting the
        # one-click commit absorb the unreviewed submodule pointer.
        self._skip_if_no_submodules()
        parent, submodule_source = self._make_repo_with_submodule()

        # Make the parent worktree dirty on the submodule by advancing
        # the working-tree HEAD to a new commit.  Parent's index still
        # points at v1.
        self._advance_submodule(parent, submodule_source, "v2\n")
        # Sanity: ``sub`` is now in the parent's changed-paths set so
        # the hasher actually visits it.  (Without this precondition
        # the test would pass trivially because ``sub`` would be in
        # neither snapshot.)
        self.assertIn("sub", git_tools.enumerate_changed_paths(parent))

        before = git_tools._hash_changed_paths_bytes(parent)

        # Advance the submodule working HEAD again.  The parent's
        # ``git status`` still shows ``M sub`` — same path, same lstat
        # mode, same staged set, but a different submodule commit.
        self._advance_submodule(parent, submodule_source, "v3\n")
        self.assertIn("sub", git_tools.enumerate_changed_paths(parent))

        after = git_tools._hash_changed_paths_bytes(parent)
        self.assertNotEqual(
            before,
            after,
            "submodule pointer drift must perturb the hash; otherwise the "
            "controlled commit can absorb an unreviewed submodule pointer",
        )

    def test_compute_review_snapshot_detects_submodule_pointer_drift(self):
        # End-to-end variant through the public snapshot API.  The
        # snapshot's ``diffHash`` must change when the submodule pointer
        # advances, even though HEAD and the visible status text might
        # be equivalent in the two states.
        self._skip_if_no_submodules()
        parent, submodule_source = self._make_repo_with_submodule()
        self._advance_submodule(parent, submodule_source, "v2\n")
        before = git_tools.compute_review_snapshot(parent)

        self._advance_submodule(parent, submodule_source, "v3\n")
        after = git_tools.compute_review_snapshot(parent)
        self.assertNotEqual(
            before["diffHash"],
            after["diffHash"],
            "snapshot diffHash must detect submodule pointer drift",
        )

    def test_get_submodule_head_returns_none_for_missing_dir(self):
        # ``get_submodule_head`` must return ``None`` (not raise) when
        # the submodule checkout directory is missing.  The hasher
        # emits a deterministic ``"submodule-uninitialized"`` marker
        # in that case so a deleted-but-tracked submodule still
        # perturbs the hash.
        parent = self.make_dir()
        result = git_tools.get_submodule_head(parent, "nonexistent-sub")
        self.assertIsNone(result)

    # ------------------------------------------------------------------
    # Codex P1-2 round 15 regression coverage: custom merge drivers.
    # ------------------------------------------------------------------

    def test_find_custom_merge_driver_paths_detects_active_driver(self):
        # Codex P1-2 round 15: a path with ``merge=<name>`` attribute
        # plus a matching ``merge.<name>.driver`` config must be detected
        # so the controlled merge can refuse instead of letting the
        # driver auto-resolve conflicts and absorb unreviewed content.
        repo = self.make_git_repo()
        subprocess.run(
            ["git", "-C", str(repo), "config", "merge.mine.driver", "true"],
            capture_output=True, check=True,
        )
        (repo / ".gitattributes").write_text(
            "feature.txt merge=mine\n", encoding="utf-8",
        )
        (repo / "feature.txt").write_text("hello\n", encoding="utf-8")
        paths = git_tools.find_custom_merge_driver_paths(repo, ["feature.txt"])
        self.assertIn("feature.txt", paths)

    def test_find_custom_merge_driver_paths_detects_non_ascii_path(self):
        # Same NUL-parsing invariant as ``find_clean_filtered_paths`` but
        # for the merge-attribute variant: a non-ASCII path with a custom
        # merge driver must be detected verbatim.
        repo = self.make_git_repo()
        subprocess.run(
            ["git", "-C", str(repo), "config", "merge.mine.driver", "true"],
            capture_output=True, check=True,
        )
        (repo / ".gitattributes").write_text(
            "dir-é/*.txt merge=mine\n", encoding="utf-8",
        )
        non_ascii_dir = repo / "dir-é"
        non_ascii_dir.mkdir(parents=True, exist_ok=True)
        (non_ascii_dir / "feature.txt").write_text(
            "hello\n", encoding="utf-8",
        )
        candidate = "dir-é/feature.txt"
        paths = git_tools.find_custom_merge_driver_paths(repo, [candidate])
        self.assertIn(candidate, paths)

    def test_find_custom_merge_driver_paths_returns_empty_when_unspecified(self):
        repo = self.make_git_repo()
        # No merge attribute declared at all.
        paths = git_tools.find_custom_merge_driver_paths(
            repo, ["feature.txt"],
        )
        self.assertEqual(paths, [])

    def test_find_custom_merge_driver_paths_ignores_attribute_without_driver(self):
        # ``merge`` attribute declared but no ``merge.<name>.driver``
        # config: Git falls back to its default merge, so this is not a
        # security concern.
        repo = self.make_git_repo()
        (repo / ".gitattributes").write_text(
            "feature.txt merge=mine\n", encoding="utf-8",
        )
        paths = git_tools.find_custom_merge_driver_paths(
            repo, ["feature.txt"],
        )
        self.assertEqual(paths, [])

    def test_find_custom_merge_driver_paths_returns_empty_for_empty_input(self):
        repo = self.make_git_repo()
        self.assertEqual(
            git_tools.find_custom_merge_driver_paths(repo, []),
            [],
        )

    def test_find_custom_merge_driver_paths_fails_closed_on_git_error(self):
        # The helper must raise on Git failure so the merge safety
        # boundary does not silently degrade.
        repo = self.make_dir()
        with mock.patch(
            "gui.orchestrator.git_tools._run_git",
            return_value=completed([], 128, "", "git check-attr merge failed"),
        ):
            with self.assertRaises(git_tools.GitError) as ctx:
                git_tools.find_custom_merge_driver_paths(
                    repo, ["feature.txt"],
                )
        self.assertIn("check-attr", str(ctx.exception).lower())

    # ------------------------------------------------------------------
    # Codex P1-2 round 16 regression coverage: built-in merge drivers.
    # ------------------------------------------------------------------

    def test_find_custom_merge_driver_paths_detects_builtin_union_driver(self):
        # Codex P1-2 round 16: Git's built-in ``union`` driver needs no
        # ``merge.<name>.driver`` config entry — it is part of Git
        # itself.  Without this recognition, a ``.gitattributes`` line
        # like ``feature.txt merge=union`` would let the controlled
        # merge auto-resolve what should have been a conflict by
        # concatenating both sides, absorbing unreviewed content into
        # the trunk.
        repo = self.make_git_repo()
        (repo / ".gitattributes").write_text(
            "feature.txt merge=union\n", encoding="utf-8",
        )
        (repo / "feature.txt").write_text("hello\n", encoding="utf-8")
        paths = git_tools.find_custom_merge_driver_paths(repo, ["feature.txt"])
        self.assertIn("feature.txt", paths)

    def test_find_custom_merge_driver_paths_detects_builtin_ours_driver(self):
        # Codex P1-2 round 16: same as above but for the ``ours``
        # built-in driver, which drops the incoming side entirely.
        repo = self.make_git_repo()
        (repo / ".gitattributes").write_text(
            "feature.txt merge=ours\n", encoding="utf-8",
        )
        (repo / "feature.txt").write_text("hello\n", encoding="utf-8")
        paths = git_tools.find_custom_merge_driver_paths(repo, ["feature.txt"])
        self.assertIn("feature.txt", paths)


class FailClosedEnumerationTests(unittest.TestCase):
    """Codex P1-1 round 13 regression coverage.

    Every safety-sensitive path enumeration in ``git_tools`` (the
    ``.env`` guard, the snapshot hash, the artifact collection) must
    fail closed when the underlying ``git`` command fails.  A transient
    failure that silently returned an empty list would let later
    artifact / commit commands succeed while dropping protected or
    unreviewed paths from the guard.
    """

    def make_dir(self):
        root = Path(__file__).resolve().parents[1] / ".gui" / "test-tmp" / uuid.uuid4().hex
        root.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        return root

    def test_list_untracked_paths_raises_on_git_failure(self):
        root = self.make_dir()
        with mock.patch(
            "gui.orchestrator.git_tools._run_git",
            return_value=completed([], 128, "", "git ls-files failed"),
        ):
            with self.assertRaises(git_tools.GitError) as ctx:
                git_tools._list_untracked_paths(root)
        self.assertIn("git ls-files", str(ctx.exception).lower())

    def test_enumerate_changed_paths_raises_on_diff_failure(self):
        # ``enumerate_changed_paths`` issues two commands (``git diff HEAD
        # --name-status -z`` then ``git ls-files --others -z``).  Failure
        # of EITHER must raise so a tracked ``.env`` modification cannot
        # slip past the guard while untracked enumeration succeeds.
        root = self.make_dir()
        with mock.patch(
            "gui.orchestrator.git_tools._run_git",
            return_value=completed([], 128, "", "diff HEAD --name-status failed"),
        ):
            with self.assertRaises(git_tools.GitError) as ctx:
                git_tools.enumerate_changed_paths(root)
        self.assertIn("name-status", str(ctx.exception).lower())

    def test_enumerate_changed_paths_raises_on_ls_files_failure(self):
        # The first call succeeds (returns no tracked changes); the
        # second call (``ls-files --others``) fails.  The helper must
        # still raise instead of returning the partial set of tracked
        # changes.
        root = self.make_dir()
        responses = [
            completed([], 0, ""),
            completed([], 128, "", "git ls-files failed"),
        ]
        with mock.patch("gui.orchestrator.git_tools._run_git", side_effect=responses):
            with self.assertRaises(git_tools.GitError) as ctx:
                git_tools.enumerate_changed_paths(root)
        self.assertIn("ls-files", str(ctx.exception).lower())

    def test_get_tracked_path_modes_raises_on_failure(self):
        # ``get_tracked_path_modes`` is consulted by the snapshot hasher
        # to detect gitlink (submodule) entries.  A silent failure that
        # returned an empty map would collapse every submodule entry to
        # a constant ``"not-a-regular-file"`` marker, hiding submodule
        # pointer drift.
        root = self.make_dir()
        with mock.patch(
            "gui.orchestrator.git_tools._run_git",
            return_value=completed([], 128, "", "git ls-files --stage failed"),
        ):
            with self.assertRaises(git_tools.GitError) as ctx:
                git_tools.get_tracked_path_modes(root)
        self.assertIn("ls-files", str(ctx.exception).lower())

    def test_compute_review_snapshot_propagates_enumeration_failure(self):
        # End-to-end: a transient failure during snapshot computation
        # must raise rather than produce a partial snapshot.
        root = self.make_dir()
        with mock.patch(
            "gui.orchestrator.git_tools.enumerate_env_violations",
            side_effect=git_tools.GitError("git ls-files --others failed."),
        ):
            with self.assertRaises(git_tools.GitError):
                git_tools.compute_review_snapshot(root)


class InProgressOperationsTests(unittest.TestCase):
    """Codex P1-4 round 17 regression coverage.

    ``get_in_progress_operations`` previously called ``git rev-parse
    --git-path <marker>`` once per marker and silently ``continue``d on
    any non-zero return code.  A non-zero exit from ``rev-parse`` is
    typically a real repository error (corrupt HEAD, unreadable config,
    broken common dir), not "marker absent"; treating errors as absent
    silently degraded the safety boundary.  Additionally, the helper
    missed three standard markers (``rebase-merge/`` and
    ``rebase-apply/`` directories for the two rebase back-ends, and
    ``AM_HEAD`` for ``git am``).

    Round 17 rewrite: fail closed on ``rev-parse --absolute-git-dir``
    error, then probe markers directly via ``Path.exists`` / ``is_dir``.
    """

    def make_dir(self):
        root = Path(__file__).resolve().parents[1] / ".gui" / "test-tmp" / uuid.uuid4().hex
        root.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        return root

    def make_git_repo(self):
        root = self.make_dir()
        subprocess.run(["git", "-C", str(root), "init"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "test@test.test"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "Test"],
            capture_output=True, check=True,
        )
        (root / "readme.md").write_text("# test\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "readme.md"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-m", "init"],
            capture_output=True, check=True,
        )
        return root

    def test_returns_empty_for_clean_repo(self):
        repo = self.make_git_repo()
        self.assertEqual(git_tools.get_in_progress_operations(repo), [])

    def test_detects_merge_head_marker(self):
        repo = self.make_git_repo()
        git_dir = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        (Path(git_dir) / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")
        ops = git_tools.get_in_progress_operations(repo)
        self.assertIn("merge", ops)

    def test_detects_rebase_merge_directory(self):
        # Codex P1-4 round 17: ``rebase-merge`` directory marker for
        # Git's default rebase back-end (2.6+).  Previously undetected
        # because the helper only checked for ``REBASE_HEAD``.
        repo = self.make_git_repo()
        git_dir = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        (Path(git_dir) / "rebase-merge").mkdir()
        ops = git_tools.get_in_progress_operations(repo)
        self.assertIn("rebase", ops)

    def test_detects_rebase_apply_directory(self):
        # Codex P1-4 round 17: ``rebase-apply`` directory marker for
        # the ``am``-based rebase back-end.
        repo = self.make_git_repo()
        git_dir = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        (Path(git_dir) / "rebase-apply").mkdir()
        ops = git_tools.get_in_progress_operations(repo)
        self.assertIn("rebase", ops)

    def test_detects_am_head_marker(self):
        # Codex P1-4 round 17: ``AM_HEAD`` marker for ``git am``.
        repo = self.make_git_repo()
        git_dir = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        (Path(git_dir) / "AM_HEAD").write_text("deadbeef\n", encoding="utf-8")
        ops = git_tools.get_in_progress_operations(repo)
        self.assertIn("am", ops)

    def test_detects_sequencer_directory(self):
        repo = self.make_git_repo()
        git_dir = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        (Path(git_dir) / "sequencer").mkdir()
        ops = git_tools.get_in_progress_operations(repo)
        self.assertIn("sequencer", ops)

    def test_fails_closed_when_absolute_git_dir_fails(self):
        # Codex P1-4 round 17: previously each ``rev-parse --git-path``
        # call was allowed to fail silently; now the single
        # ``rev-parse --absolute-git-dir`` call raises ``GitError`` so
        # the caller surfaces ``COMMIT_BLOCKED`` / ``MERGE_BLOCKED``
        # rather than silently reporting no in-progress operations.
        repo = self.make_dir()
        with mock.patch(
            "gui.orchestrator.git_tools._run_git",
            return_value=completed([], 128, "", "fatal: not a git repository"),
        ):
            with self.assertRaises(git_tools.GitError):
                git_tools.get_in_progress_operations(repo)


class ConfigProbeReturnCodeTests(unittest.TestCase):
    """Codex P1-5 round 17 regression coverage.

    ``git config --get <key>`` returns exit code 1 when the key is
    absent and any other non-zero code on a real error (corrupt
    config, malformed key, permission denied).  The previous
    implementations of ``find_clean_filtered_paths`` and
    ``find_custom_merge_driver_paths`` treated every non-zero return as
    "not configured", silently degrading the safety boundary.  Round 17
    tightens the probes: only ``returncode == 1`` means absent; any
    other non-zero raises ``GitError``.
    """

    def make_dir(self):
        root = Path(__file__).resolve().parents[1] / ".gui" / "test-tmp" / uuid.uuid4().hex
        root.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        return root

    def make_git_repo(self):
        root = self.make_dir()
        subprocess.run(["git", "-C", str(root), "init"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "test@test.test"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "Test"],
            capture_output=True, check=True,
        )
        (root / "readme.md").write_text("# test\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "readme.md"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-m", "init"],
            capture_output=True, check=True,
        )
        return root

    def _stage_filtered_repo(self):
        # Configure a filter attribute but defer the ``config --get``
        # call to the mock so we can exercise each return code.
        repo = self.make_git_repo()
        (repo / ".gitattributes").write_text("feature.py filter=uc\n", encoding="utf-8")
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        return repo

    def test_find_clean_filtered_paths_raises_on_config_error(self):
        # Codex P1-5 round 17: ``config --get filter.<name>.clean``
        # returning exit 128 (corrupt config) must raise instead of
        # being treated as "filter inactive".
        repo = self._stage_filtered_repo()
        # ``enumerate_changed_paths`` is mocked to return a single
        # filtered path so the helper proceeds directly to the
        # ``check-attr`` + config-probe sequence.
        responses = [
            # ``check-attr -z filter -- feature.py``.
            completed(["check-attr"], 0, "feature.py\0filter\0uc\0"),
            # First probe (``filter.uc.clean``) fails with 128.
            completed(["config"], 128, "", "fatal: bad config line 1"),
        ]
        with mock.patch(
            "gui.orchestrator.git_tools.enumerate_changed_paths",
            return_value=["feature.py"],
        ), mock.patch(
            "gui.orchestrator.git_tools._run_git",
            side_effect=responses,
        ):
            with self.assertRaises(git_tools.GitError):
                git_tools.find_clean_filtered_paths(repo)

    def test_find_clean_filtered_paths_raises_on_process_config_error(self):
        # ``filter.<name>.clean`` is absent (exit 1) but the process
        # probe fails (exit 128).  Must raise instead of returning [].
        repo = self._stage_filtered_repo()
        responses = [
            completed(["check-attr"], 0, "feature.py\0filter\0uc\0"),
            completed(["config"], 1, ""),  # clean absent
            completed(["config"], 128, "", "fatal: bad config"),  # process error
        ]
        with mock.patch(
            "gui.orchestrator.git_tools.enumerate_changed_paths",
            return_value=["feature.py"],
        ), mock.patch(
            "gui.orchestrator.git_tools._run_git",
            side_effect=responses,
        ):
            with self.assertRaises(git_tools.GitError):
                git_tools.find_clean_filtered_paths(repo)

    def test_find_clean_filtered_paths_treats_returncode_1_as_absent(self):
        # ``config --get`` returns exit 1 for both the clean and process
        # probes — the filter is genuinely inactive.  Must NOT raise.
        repo = self._stage_filtered_repo()
        responses = [
            completed(["check-attr"], 0, "feature.py\0filter\0uc\0"),
            completed(["config"], 1, ""),  # clean absent
            completed(["config"], 1, ""),  # process absent
        ]
        with mock.patch(
            "gui.orchestrator.git_tools.enumerate_changed_paths",
            return_value=["feature.py"],
        ), mock.patch(
            "gui.orchestrator.git_tools._run_git",
            side_effect=responses,
        ):
            self.assertEqual(git_tools.find_clean_filtered_paths(repo), [])

    def test_find_custom_merge_driver_paths_raises_on_config_error(self):
        # Codex P1-5 round 17: ``config --get merge.<name>.driver``
        # returning exit 128 must raise instead of being treated as
        # "driver inactive".  ``find_custom_merge_driver_paths`` takes
        # the candidate paths directly (no ``enumerate_changed_paths``
        # call), so we only need to mock ``_run_git`` for the
        # check-attr + config sequence.
        repo = self.make_git_repo()
        responses = [
            # ``check-attr -z merge -- feature.txt``.
            completed(["check-attr"], 0, "feature.txt\0merge\0mine\0"),
            # Config probe fails (corrupt config).
            completed(["config"], 128, "", "fatal: bad config line 1"),
        ]
        with mock.patch(
            "gui.orchestrator.git_tools._run_git",
            side_effect=responses,
        ):
            with self.assertRaises(git_tools.GitError):
                git_tools.find_custom_merge_driver_paths(repo, ["feature.txt"])

    # ------------------------------------------------------------------
    # Codex P2-1 round 19: ``is_ancestor`` fail-closed semantics.
    # ------------------------------------------------------------------

    def test_is_ancestor_returns_true_for_exit_zero(self):
        repo = self.make_git_repo()
        with mock.patch(
            "gui.orchestrator.git_tools._run_git",
            return_value=completed(["merge-base"], 0, "", ""),
        ):
            self.assertTrue(git_tools.is_ancestor(repo, "sha1", "sha2"))

    def test_is_ancestor_returns_false_for_exit_one(self):
        repo = self.make_git_repo()
        with mock.patch(
            "gui.orchestrator.git_tools._run_git",
            return_value=completed(["merge-base"], 1, "", ""),
        ):
            self.assertFalse(git_tools.is_ancestor(repo, "sha1", "sha2"))

    def test_is_ancestor_raises_on_other_exit_code(self):
        # Codex P2-1 round 19: exit code 2 (or any code that is not 0
        # or 1) indicates a Git-level failure (corrupted object DB,
        # unreadable ref, etc.).  Previously the function collapsed
        # any non-zero exit to ``False``, silently letting the probe
        # result be treated as a clean "not an ancestor" decision.
        repo = self.make_git_repo()
        with mock.patch(
            "gui.orchestrator.git_tools._run_git",
            return_value=completed(["merge-base"], 2, "", "fatal: bad object"),
        ):
            with self.assertRaises(git_tools.GitError) as ctx:
                git_tools.is_ancestor(repo, "sha1", "sha2")
        self.assertIn("bad object", str(ctx.exception))

    def test_is_ancestor_raises_on_execution_failure(self):
        # When the underlying ``_run_git`` itself raises (e.g.
        # subprocess crash), the error must propagate rather than be
        # swallowed into a ``False`` return.
        repo = self.make_git_repo()
        with mock.patch(
            "gui.orchestrator.git_tools._run_git",
            side_effect=git_tools.GitError("subprocess failed"),
        ):
            with self.assertRaises(git_tools.GitError):
                git_tools.is_ancestor(repo, "sha1", "sha2")

    def test_is_ancestor_returns_false_for_empty_inputs(self):
        # Empty input is the documented "absence" case — return False
        # without invoking Git at all.
        repo = self.make_git_repo()
        self.assertFalse(git_tools.is_ancestor(repo, "", "sha2"))
        self.assertFalse(git_tools.is_ancestor(repo, "sha1", ""))
        self.assertFalse(git_tools.is_ancestor(repo, "", ""))


if __name__ == "__main__":
    unittest.main()
