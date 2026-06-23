from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

def _resource_root() -> Path:
    """Return the directory that holds bundled resources.

    Source mode: the repository root (parent of ``gui/``).
    Frozen mode (PyInstaller): the bundle root, where ``gui/static``,
    ``scripts``, ``docs`` and ``.claude`` live. ``sys._MEIPASS`` is set
    by PyInstaller for both onedir and onefile builds and points at the
    directory containing the extracted/bundled modules and data files.
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


ROOT = _resource_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gui.orchestrator.adapters import ClaudeCliWindowAdapter, CodexCliWindowAdapter
from gui.orchestrator.cli_window import load_settings
from gui.orchestrator.git_tools import (
    DirtyWorkTreeError,
    EnvFileChangedError,
    GitError,
    assert_clean_work_tree,
    assert_git_work_tree,
    collect_git_artifacts,
    compute_repo_id,
    compute_review_snapshot,
    get_current_branch,
    get_git_common_dir,
    get_main_worktree_path,
    is_ancestor,
    is_git_worktree,
    list_worktrees,
)
from gui.orchestrator.git_workflow import (
    CommitError,
    MergeError,
    MergeRecoveryJournal,
    WorktreeCreationError,
    controlled_commit,
    controlled_merge_to_main,
    create_worktree,
    recover_pending_merge,
)
from gui.orchestrator.prompts import (
    write_claude_implementation_prompt,
    write_codex_review_prompt,
    write_fix_prompt,
)
from gui.orchestrator.report_parser import ReportValidationError, load_review_report
from gui.orchestrator.state_machine import (
    Status,
    StateTransitionError,
    cancel as cancel_status,
    transition,
)
from gui.orchestrator.models import utc_now as utc_now_str
from gui.orchestrator.store import TaskStore, TaskStoreError
from gui.orchestrator.test_runner import run_tests


STATIC_DIR = ROOT / "gui" / "static"
# Application state directory. Overridable via the ``CCDL_STATE_DIR``
# environment variable or :func:`configure_paths` so the desktop app can
# redirect writes to ``%LOCALAPPDATA%\\CodexClaudeDevLoop`` when frozen,
# while source-mode invocations continue to use the in-repo ``.gui``.
STATE_DIR: Path = (
    Path(os.environ["CCDL_STATE_DIR"]).expanduser()
    if os.environ.get("CCDL_STATE_DIR")
    else ROOT / ".gui"
)
PROJECTS_FILE = STATE_DIR / "projects.json"
TASKS_DIR = STATE_DIR / "tasks"
TRASH_TASKS_DIR = STATE_DIR / "trash" / "tasks"
SETTINGS_FILE = STATE_DIR / "settings.json"
AUDIT_LOG_FILE = STATE_DIR / "audit.log"
# Codex P1-1 round 19: durable merge-recovery journal directory.
# Lives under application state storage so the journal survives process
# restarts and is never written inside ``.git`` or as an untracked file
# in the target worktree.
MERGE_RECOVERY_DIR = STATE_DIR / "merge_recovery"

ARTIFACTS = {
    "implementationReport": "docs/IMPLEMENTATION_REPORT.md",
    "claudeLog": "docs/claude-run.log",
    "changesStatus": "docs/CHANGES_STATUS.txt",
    "changesDiff": "docs/CHANGES_DIFF.txt",
    "reviewInput": "docs/REVIEW_INPUT.md",
    "codexReview": "docs/CODEX_REVIEW.json",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# Per-task mutex registry guarding the controlled commit / merge services
# (Codex P1-4 round 15).  ``ThreadingHTTPServer`` dispatches each HTTP
# request on its own thread, so a duplicate POST to
# ``/api/tasks/{id}/commit`` (or ``/merge``) can race a previous in-flight
# request.  Without serialisation, both requests can read the task in its
# pre-COMMITTED state, both proceed to execute the Git operation, and the
# loser of the race then saves its own stale ``Task`` object — overwriting
# the winner's ``COMMITTED`` metadata and making the task appear
# uncommitted even though HEAD has actually advanced.
# The lock covers the *entire* "load → validate → mutate Git → save"
# span so concurrent requests are serialised per task.  The registry
# itself is guarded by ``_TASK_LOCKS_GUARD`` so two requests for the
# same task that arrive simultaneously still get the *same* lock object.
_TASK_LOCKS: dict[str, threading.RLock] = {}
_TASK_LOCKS_GUARD = threading.Lock()


def _task_operation_lock(task_id: str) -> threading.RLock:
    """Return the per-task ``RLock`` used to serialise controlled commit / merge."""
    if not task_id:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Task id is required.")
    with _TASK_LOCKS_GUARD:
        lock = _TASK_LOCKS.get(task_id)
        if lock is None:
            lock = threading.RLock()
            _TASK_LOCKS[task_id] = lock
    return lock


# Per-resource mutex registry keyed by canonical worktree / repository
# path (Codex P1-3 round 16).  The per-task lock above serialises
# requests for the *same* task ID, but different tasks bound to the same
# worktree / repository can still race the underlying Git index and refs
# because each task's per-task lock is independent.  Without a
# resource-level lock, two tasks on the same worktree can interleave
# ``git add -A`` / ``commit-tree`` / ``update-ref`` invocations — the
# CAS ref update catches the loser, but only after both Git operations
# have run and the loser's ``COMMIT_BLOCKED`` audit record has been
# written.  Holding the resource lock around the actual Git mutation
# serialises those mutations per worktree / repo, while unrelated
# repositories remain free to proceed concurrently.
_RESOURCE_LOCKS: dict[str, threading.RLock] = {}
_RESOURCE_LOCKS_GUARD = threading.Lock()
_AUDIT_LOG_LOCK = threading.RLock()


def _resource_lock_key(resource_path: Path) -> str:
    """Canonical cache key for ``resource_path``.

    Uses ``resolve(strict=False)`` so the key is stable even when the
    path does not currently exist (e.g. a worktree that was just
    removed).  Lower-cases the resulting string so Windows's
    case-insensitive filesystem does not produce two distinct locks for
    what is effectively the same directory.
    """
    try:
        resolved = resource_path.expanduser().resolve(strict=False)
    except (OSError, ValueError):
        resolved = resource_path
    return str(resolved).lower()


def _resource_operation_lock(resource_path: Path) -> threading.RLock:
    """Return the per-resource ``RLock`` used to serialise Git mutations on
    ``resource_path`` (Codex P1-3 round 16).

    The per-task lock serialises the ``load → validate → save`` span for
    a single task; this resource lock additionally serialises the
    actual Git mutations (``git add``, ``commit-tree``, ``update-ref``,
    ``merge``) so two different tasks bound to the same worktree /
    primary repository cannot concurrently perturb the same index or
    refs.  Unrelated repositories still proceed concurrently because
    their resource locks are independent.
    """
    if resource_path is None:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Resource path is required.")
    key = _resource_lock_key(resource_path)
    with _RESOURCE_LOCKS_GUARD:
        lock = _RESOURCE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _RESOURCE_LOCKS[key] = lock
    return lock


def _merge_recovery_dir(task_store: TaskStore) -> Path:
    """Return the application-state journal directory owned by ``task_store``."""
    try:
        return Path(task_store.tasks_root).parent / "merge_recovery"
    except (AttributeError, TypeError, ValueError):
        return MERGE_RECOVERY_DIR


def now_ms() -> int:
    return int(time.time() * 1000)


def read_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def write_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_audit_log(action: str, subject: str, details: dict[str, Any] | None = None) -> None:
    AUDIT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "action": action,
        "subject": subject,
        "details": details or {},
    }
    with _AUDIT_LOG_LOCK:
        with AUDIT_LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _merge_audit_exists(operation_id: str, action: str) -> bool:
    """Return whether a durable merge audit for this operation already exists."""
    if not operation_id or not AUDIT_LOG_FILE.is_file():
        return False
    with _AUDIT_LOG_LOCK:
        try:
            lines = AUDIT_LOG_FILE.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
    for line in lines:
        try:
            payload = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if payload.get("action") != action:
            continue
        details = payload.get("details")
        if isinstance(details, dict) and details.get("operationId") == operation_id:
            return True
    return False


def _write_merge_audit_once(
    action: str, subject: str, operation_id: str, details: dict[str, Any]
) -> None:
    """Idempotently append one durable audit line for a merge operation."""
    with _AUDIT_LOG_LOCK:
        if _merge_audit_exists(operation_id, action):
            return
        write_audit_log(action, subject, {"operationId": operation_id, **details})


def normalize_path(raw_path: str) -> Path:
    if not raw_path or not raw_path.strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, "Project path is required.")
    path = Path(raw_path).expanduser()
    try:
        return path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"Path does not exist: {raw_path}") from exc


def project_id(path: Path) -> str:
    return hashlib.sha1(str(path).lower().encode("utf-8")).hexdigest()[:12]


def detect_project_kind(path: Path) -> str:
    if not path.is_dir():
        raise ApiError(HTTPStatus.BAD_REQUEST, "Project path must be a directory.")
    if (path / "scripts" / "run-claude.ps1").is_file() and (path / "docs").is_dir():
        return "orchestrator"
    if (path / ".git").exists() or is_inside_git_repo(path):
        return "git-uninitialized"
    raise ApiError(
        HTTPStatus.BAD_REQUEST,
        "Directory is not an orchestrator project or a Git repository.",
    )


def is_inside_git_repo(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip().lower() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def _path_is_worktree_root(path: Path) -> bool:
    """Return True when ``path`` is itself a Git working-tree root.

    Distinguishes a real primary or linked worktree from a stray subdirectory
    that merely lives inside a parent Git repository.  Uses ``git rev-parse
    --show-toplevel`` so empty ``.git`` placeholders and nested paths do not
    pass: only paths whose declared toplevel matches themselves qualify.
    """
    if not path.exists() or not path.is_dir():
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    toplevel = (result.stdout or "").strip()
    if not toplevel:
        return False
    try:
        return Path(toplevel).resolve(strict=False) == path.resolve(strict=False)
    except (OSError, ValueError):
        return False


def make_project(path: Path, name: str | None = None) -> dict[str, Any]:
    kind = detect_project_kind(path)
    project: dict[str, Any] = {
        "id": project_id(path),
        "name": name.strip() if name and name.strip() else path.name,
        "path": str(path),
        "kind": kind,
        "worktreeType": None,
        "gitCommonDir": None,
        "repoId": None,
        "branch": None,
        "mainWorktreePath": None,
        "available": True,
        "lastResult": None,
        "lastExitCode": None,
        "lastRunAt": None,
    }
    if kind in ("orchestrator", "git-uninitialized"):
        try:
            common_dir = get_git_common_dir(path)
            project["gitCommonDir"] = common_dir
            project["repoId"] = compute_repo_id(common_dir)
            project["branch"] = get_current_branch(path)
            if is_git_worktree(path):
                project["worktreeType"] = "worktree"
                project["mainWorktreePath"] = get_main_worktree_path(path)
            else:
                project["worktreeType"] = "primary"
        except Exception:
            pass
    return project


def clamp_max_rounds(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 3
    return max(1, min(15, parsed))


# Executable used to launch the orchestrator PowerShell script. Defaults
# to ``"powershell"`` (Windows PowerShell 5.x, present on every supported
# Windows install). The desktop app overrides this via
# :func:`set_powershell_executable` with whatever it resolved at startup
# (``powershell.exe`` or ``pwsh.exe``) so the run loop does not break on
# hosts whose only PowerShell is PowerShell 7 (``pwsh``).
POWERSHELL_EXECUTABLE: str = "powershell"


def set_powershell_executable(path: str | None) -> None:
    """Override the executable used to drive ``scripts/run-claude.ps1``.

    Pass an absolute path resolved by the desktop app's dependency probe
    so a host that only has ``pwsh.exe`` (PowerShell 7) on PATH can still
    launch runs. Passing ``None`` resets to the default ``"powershell"``
    so legacy source-mode callers see no behaviour change.
    """
    global POWERSHELL_EXECUTABLE
    POWERSHELL_EXECUTABLE = path.strip() if path and path.strip() else "powershell"


def build_run_command(project_path: Path, options: dict[str, Any]) -> list[str]:
    script_path = project_path / "scripts" / "run-claude.ps1"
    command = [
        POWERSHELL_EXECUTABLE,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-MaxRounds",
        str(clamp_max_rounds(options.get("maxRounds", 3))),
    ]
    if options.get("skipTests"):
        command.append("-SkipTests")
    if options.get("allowNoTests"):
        command.append("-AllowNoTests")
    if options.get("skipCodexReview"):
        command.append("-SkipCodexReview")
    review_command = str(options.get("reviewCommand") or "").strip()
    if review_command:
        command.extend(["-ReviewCommand", review_command])
    return command


def exit_code_to_result(exit_code: int | None, stopped: bool = False) -> str:
    if stopped:
        return "STOPPED"
    return {
        0: "PASS",
        1: "ENVIRONMENT_ERROR",
        2: "FAILED_OR_MANUAL_VERIFY",
        3: "CLAUDE_ERROR",
        4: "INTERRUPTED",
        5: "UNKNOWN",
        6: "CODEX_REVIEW_INVALID",
        7: "NEEDS_FIX",
        8: "NEEDS_CODEX_REVIEW",
    }.get(exit_code, "FAILED")


class ProjectStore:
    def __init__(self, path: Path = PROJECTS_FILE):
        self.path = path
        self._lock = threading.RLock()

    def list_projects(self) -> list[dict[str, Any]]:
        with self._lock:
            data = read_json_file(self.path, {"projects": []})
            projects = data.get("projects", [])
            if not isinstance(projects, list):
                return []
            changed = False
            for project in projects:
                project_path = project.get("path", "")
                if project_path:
                    is_dir = Path(project_path).is_dir()
                    project["available"] = is_dir
                    if is_dir:
                        changed = self._refresh_project_metadata(project) or changed
                else:
                    project["available"] = False
            if changed:
                write_json_file(self.path, {"projects": projects})
            return projects

    def save_projects(self, projects: list[dict[str, Any]]) -> None:
        with self._lock:
            write_json_file(self.path, {"projects": projects})

    @staticmethod
    def _same_path(a: str, b: str) -> bool:
        na = a.strip().lower().replace("\\", "/").rstrip("/")
        nb = b.strip().lower().replace("\\", "/").rstrip("/")
        return na == nb

    @staticmethod
    def _refresh_project_metadata(project: dict[str, Any]) -> bool:
        """Refresh worktree metadata for an available project. Returns True if anything changed."""
        project_path = project.get("path", "")
        if not project_path:
            return False
        path = Path(project_path)
        kind = project.get("kind")
        if kind not in ("orchestrator", "git-uninitialized"):
            return False
        changed = False
        try:
            common_dir = get_git_common_dir(path)
            if project.get("gitCommonDir") != common_dir:
                project["gitCommonDir"] = common_dir
                changed = True
            repo_id = compute_repo_id(common_dir)
            if project.get("repoId") != repo_id:
                project["repoId"] = repo_id
                changed = True
            branch = get_current_branch(path)
            if project.get("branch") != branch:
                project["branch"] = branch
                changed = True
            if is_git_worktree(path):
                if project.get("worktreeType") != "worktree":
                    project["worktreeType"] = "worktree"
                    changed = True
                main_path = get_main_worktree_path(path)
                if project.get("mainWorktreePath") != main_path:
                    project["mainWorktreePath"] = main_path
                    changed = True
            else:
                if project.get("worktreeType") != "primary":
                    project["worktreeType"] = "primary"
                    changed = True
        except Exception:
            pass
        return changed

    def add_project(self, path: Path, name: str | None = None) -> dict[str, Any]:
        project = make_project(path, name)
        projects = self.list_projects()
        updated_existing = False
        for existing in projects:
            if self._same_path(str(path), str(existing.get("path", ""))):
                existing.update(
                    {
                        "name": project["name"],
                        "path": project["path"],
                        "kind": project["kind"],
                        "worktreeType": project.get("worktreeType"),
                        "gitCommonDir": project.get("gitCommonDir"),
                        "repoId": project.get("repoId"),
                        "branch": project.get("branch"),
                        "mainWorktreePath": project.get("mainWorktreePath"),
                        "available": True,
                    }
                )
                self.save_projects(projects)
                project = existing
                updated_existing = True
                break
        if not updated_existing:
            projects.append(project)
            self.save_projects(projects)

        # Auto-discover sibling worktrees so the project tree reflects the
        # repository layout. Failures are silent: a missing common dir just
        # means there is nothing else to register.
        self._auto_discover_sibling_worktrees(path, projects)
        return project

    def _auto_discover_sibling_worktrees(
        self, imported_path: Path, projects: list[dict[str, Any]]
    ) -> None:
        """Register every sibling worktree of the same Git repository.

        Called after a project is added or refreshed so the project list
        reflects the full repository layout.  Never raises — Git discovery is
        best-effort and any failure simply leaves the project list unchanged.
        Only fires when ``imported_path`` is itself a Git worktree root, so a
        stray subdirectory inside a parent repository does not contaminate the
        project list with unrelated siblings.
        """
        if not _path_is_worktree_root(imported_path):
            return
        try:
            siblings = list_worktrees(imported_path)
        except Exception:
            return
        if not siblings:
            return
        changed = False
        existing_paths = {self._normalize_path(p.get("path", "")) for p in projects}
        for sibling in siblings:
            sibling_path_str = sibling.path
            if not sibling_path_str:
                continue
            sibling_path = Path(sibling_path_str)
            if self._normalize_path(str(sibling_path)) == self._normalize_path(str(imported_path)):
                continue
            if not sibling_path.exists() or not sibling_path.is_dir():
                continue
            if self._normalize_path(sibling_path_str) in existing_paths:
                continue
            if not _path_is_worktree_root(sibling_path):
                continue
            try:
                sibling_project = make_project(sibling_path)
            except ApiError:
                continue
            projects.append(sibling_project)
            existing_paths.add(self._normalize_path(sibling_project["path"]))
            changed = True
        if changed:
            self.save_projects(projects)

    @staticmethod
    def _normalize_path(raw: str) -> str:
        return raw.strip().lower().replace("\\", "/").rstrip("/")

    def get_project(self, project_id_value: str) -> dict[str, Any]:
        for project in self.list_projects():
            if project.get("id") == project_id_value:
                return project
        raise ApiError(HTTPStatus.NOT_FOUND, "Project not found.")

    def update_project(self, project_id_value: str, patch: dict[str, Any]) -> dict[str, Any]:
        projects = self.list_projects()
        for project in projects:
            if project.get("id") == project_id_value:
                project.update(patch)
                self.save_projects(projects)
                return project
        raise ApiError(HTTPStatus.NOT_FOUND, "Project not found.")

    def remove_project(self, project_id_value: str) -> dict[str, Any]:
        projects = self.list_projects()
        for index, project in enumerate(projects):
            if project.get("id") == project_id_value:
                removed = dict(project)
                del projects[index]
                self.save_projects(projects)
                return removed
        raise ApiError(HTTPStatus.NOT_FOUND, "Project not found.")


class _Win32JobObject:
    """Best-effort Windows Job Object with kill-on-job-close.

    On Windows 8+ the OS lets us wrap a spawned process (and any
    descendants it later spawns) in a Job Object whose
    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` flag guarantees the whole
    tree dies when the job handle is closed. This is what
    :class:`RunManager.stop` needs: terminating the direct PowerShell
    parent is not enough because Claude / Codex CLI processes spawned
    by ``scripts/run-claude.ps1`` would otherwise outlive the parent
    and keep modifying the project after the desktop window has closed.

    The class is intentionally inert on non-Windows hosts and when the
    ctypes machinery is unavailable so unit tests can run anywhere.
    Instances are falsy until a real handle is attached.
    """

    # Win32 constants we need. Defined inline so the module does not
    # depend on the pywin32 package.
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    def __init__(self) -> None:
        self._kernel32 = None
        self._handle = None
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [
                wintypes.HANDLE,
                wintypes.HANDLE,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]

            # Minimal structure layouts — only the fields we touch are
            # named; the rest are padding bytes that ctypes sizes for us.
            class _IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]

            class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_void_p),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = (
                self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            ok = kernel32.SetInformationJobObject(
                handle,
                self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                kernel32.CloseHandle(handle)
                return
            self._kernel32 = kernel32
            self._handle = handle
        except (ImportError, AttributeError, OSError):
            # ctypes missing, weird Windows build, or the calls failed.
            # Stay inert; RunManager falls back to plain terminate().
            self._kernel32 = None
            self._handle = None

    def __bool__(self) -> bool:
        return self._handle is not None

    def assign(self, process: "subprocess.Popen[Any]") -> bool:
        """Add ``process`` (and any descendants it spawns) to this job.

        Returns ``True`` on success. Failures are silent — the caller
        keeps the run going and just loses the tree-kill guarantee.
        """
        if not self._handle or self._kernel32 is None:
            return False
        try:
            process_handle = self._kernel32.OpenProcess(
                self._PROCESS_SET_QUOTA | self._PROCESS_TERMINATE,
                False,
                process.pid,
            )
            if not process_handle:
                return False
            try:
                return bool(
                    self._kernel32.AssignProcessToJobObject(
                        self._handle, process_handle
                    )
                )
            finally:
                self._kernel32.CloseHandle(process_handle)
        except (AttributeError, OSError):
            return False

    def close(self) -> None:
        """Close the job handle. On Windows this terminates every process
        still assigned to the job (parent + all descendants)."""
        if self._kernel32 is None or self._handle is None:
            return
        try:
            self._kernel32.CloseHandle(self._handle)
        except (AttributeError, OSError):
            pass
        finally:
            self._handle = None


class _FileGate:
    """File-based gate used by the gated Job-aware launcher (P2-2 fix).

    The wrapper process polls for ``path`` to exist via PowerShell
    ``Test-Path``; the parent creates the file with :meth:`release` only
    after the wrapper has been successfully added to the Job Object. This
    guarantees no descendant of the real command can escape the Job.
    """

    __slots__ = ("path",)

    def __init__(self, path: Path) -> None:
        self.path = path

    def release(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch()
        except OSError:
            # Best-effort. If release fails the wrapper will spin until
            # the process is killed by other means; the Job still owns
            # it so cleanup is guaranteed.
            pass

    def cleanup(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def _create_run_gate() -> _FileGate:
    """Return a fresh, un-released file gate under the per-user temp dir."""
    import tempfile
    import uuid

    gate_dir = Path(tempfile.gettempdir()) / "ccdl-run-gates"
    return _FileGate(gate_dir / f"gate-{uuid.uuid4().hex}.txt")


def _build_gated_run_command(
    real_command: list[str],
    gate: _FileGate,
    *,
    powershell_executable: str,
) -> list[str]:
    """Wrap ``real_command`` so a wrapper process can be added to a Job
    before the real command spawns any descendants.

    The wrapper is a single ``powershell`` invocation that polls
    ``Test-Path`` on ``gate.path`` until the parent creates it, then
    invokes ``real_command`` via the call operator ``&``. Because the
    call operator starts the real command as a child of the wrapper,
    the real command (and its own descendants — Claude / Codex CLI)
    inherit the wrapper's Job membership.

    Each argument is single-quoted for PowerShell with embedded single
    quotes escaped as ``''`` per PowerShell conventions.

    The wrapper script ends with an explicit ``exit`` that propagates the
    nested command's ``$LASTEXITCODE``. Without this, Windows PowerShell
    ``-Command`` does not reliably forward a native child's exit status —
    e.g. NEEDS_FIX (7) can be collapsed to 1 or 0 by the wrapper, which
    would make ``exit_code_to_result`` misclassify the run. By reading
    ``$LASTEXITCODE`` and exiting with it explicitly we guarantee the
    wrapper process exit code matches the real command's exit code.
    """

    gate_literal = str(gate.path).replace("'", "''")
    quoted_args = ["'" + arg.replace("'", "''") + "'" for arg in real_command]
    real_invocation = "& " + " ".join(quoted_args)
    # P1-1: capture the nested command's $LASTEXITCODE and exit with it
    # explicitly. ``$null -eq $LASTEXITCODE`` covers the case where the
    # real command was a pure cmdlet / script that did not set
    # $LASTEXITCODE — in that case ``$?`` tells us whether the pipeline
    # succeeded (exit 0) or failed (exit 1).
    exit_propagation = (
        "; $code = $LASTEXITCODE; "
        "if ($null -eq $code) { if ($?) { exit 0 } else { exit 1 } } "
        "else { exit $code }"
    )
    script = (
        f"while (-not (Test-Path -LiteralPath '{gate_literal}')) "
        f"{{ Start-Sleep -Milliseconds 5 }}; {real_invocation}{exit_propagation}"
    )
    return [
        powershell_executable,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]


class RunManager:
    def __init__(self, store: ProjectStore):
        self.store = store
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self.current: dict[str, Any] | None = None
        self._process: subprocess.Popen[str] | None = None
        self._stopping = False
        # Win32 Job Object that wraps the current run's process tree so
        # ``stop()`` terminates descendants too. ``None`` when no run is
        # active or when the Job Object machinery is unavailable.
        self._job: _Win32JobObject | None = None
        # Reader thread for the active run. P2-1 fix: ``start()`` refuses
        # to launch a new run while the previous reader is still alive so
        # the previous run's finalize (which writes ``current``, clears
        # ``_process``/``_job`` and closes the captured Job) cannot race
        # a new run's setup.
        self._reader_thread: threading.Thread | None = None

    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            if not self.current:
                return None
            return {
                key: value
                for key, value in self.current.items()
                if key not in {"command"}
            } | {"command": self.current.get("command", [])}

    def is_project_running(self, project_id_value: str) -> bool:
        with self._lock:
            return bool(
                self.current
                and self.current.get("projectId") == project_id_value
                and self.current.get("status") == "running"
            )

    def start(self, project: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            # P2-1 fix: do not start a new run while the previous run's
            # reader thread is still alive. The reader finalizes the run
            # by writing to ``current`` and closing the captured Job; if
            # we let a new run start now, the old reader's finalize could
            # overwrite the new run's state or close the wrong Job.
            if self._reader_thread is not None and self._reader_thread.is_alive():
                raise ApiError(
                    HTTPStatus.CONFLICT,
                    "Previous run is still finalizing; retry in a moment.",
                )
            if self._process and self._process.poll() is None:
                raise ApiError(HTTPStatus.CONFLICT, "Another run is already active.")

            project_path = Path(project["path"])
            if detect_project_kind(project_path) != "orchestrator":
                raise ApiError(HTTPStatus.BAD_REQUEST, "Project must be initialized before running.")

            command = build_run_command(project_path, options)
            run_id = str(now_ms())
            self.current = {
                "id": run_id,
                "projectId": project["id"],
                "projectName": project["name"],
                "projectPath": str(project_path),
                "status": "running",
                "startedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                "endedAt": None,
                "exitCode": None,
                "result": None,
                "logs": [],
                "command": command,
            }
            self._stopping = False

            # P2-2 fix: prefer the gated Job-aware launcher so the
            # wrapper process is added to the Job BEFORE it spawns the
            # real PowerShell command. That way Claude / Codex CLI
            # descendants of the real command inherit the Job and cannot
            # escape ``stop()`` / ``shutdown()``. Falls back to plain
            # ``Popen`` if Job creation or assignment fails.
            spawn_result = self._spawn_run_process(command, project_path, run_id)
            if spawn_result is None:
                # Spawn failed; ``_spawn_run_process`` already recorded
                # the failure in ``current["logs"]``.
                self.current["status"] = "failed"
                self.current["result"] = "START_FAILED"
                self._condition.notify_all()
                last_log = self.current["logs"][-1] if self.current["logs"] else "Failed to start process."
                raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, last_log)

            process, job, gate = spawn_result
            self._process = process
            self._job = job

            self._append_locked(
                f"Started (gated): {' '.join(command)}"
                if gate is not None
                else f"Started: {' '.join(command)}"
            )
            # P2-1 fix: pass the per-run state as parameters so the
            # reader cannot accidentally pollute a newer run. The reader
            # also gets the gate so it can clean up the gate file once
            # the run finishes.
            reader = threading.Thread(
                target=self._read_process,
                args=(process, run_id, job, gate),
                daemon=True,
                name=f"run-reader-{run_id}",
            )
            self._reader_thread = reader
            reader.start()
            return self.snapshot() or {}

    def _spawn_run_process(
        self,
        command: list[str],
        project_path: Path,
        run_id: str,
    ) -> tuple[subprocess.Popen[str], "_Win32JobObject | None", "_FileGate | None"] | None:
        """Spawn the run process via the gated Job-aware launcher.

        Returns ``(process, job, gate)`` on success. ``job`` and ``gate``
        are ``None`` when the gated path is unavailable (non-Windows or
        Job creation/assignment failure) and the launcher falls back to
        plain ``Popen``. Returns ``None`` if even the fallback spawn
        fails; in that case the failure is appended to ``current["logs"]``
        so the caller can surface it.
        """
        popen_kwargs: dict[str, Any] = {
            "cwd": str(project_path),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }

        # P2-2: try the gated launcher whenever a real Job Object is
        # available. ``_Win32JobObject`` is inert (returns a falsy
        # placeholder) on non-Windows hosts and when ``ctypes`` is
        # missing, so the truthiness check below naturally falls
        # through to plain ``Popen`` on those platforms.
        try:
            job = _Win32JobObject()
        except Exception:
            job = None
        if job:
            gate = _create_run_gate()
            gated_command = _build_gated_run_command(
                command,
                gate,
                powershell_executable=POWERSHELL_EXECUTABLE,
            )
            try:
                wrapper_process = subprocess.Popen(gated_command, **popen_kwargs)
            except OSError as exc:
                self._append_locked(
                    f"Gated launcher spawn failed ({exc}); falling back to plain launch."
                )
                gate.cleanup()
                job.close()
                gate = None
                job = None
            else:
                # Assign the WRAPPER to the Job BEFORE releasing the
                # gate. The real command (and its descendants) are
                # spawned by the wrapper after the gate is released,
                # so they inherit the Job.
                if job.assign(wrapper_process):
                    gate.release()
                    self._append_locked(
                        "Gated launcher: wrapper added to Job, gate released."
                    )
                    return wrapper_process, job, gate
                # Assign failed — terminate the suspended wrapper, wait
                # briefly, and on timeout escalate to ``kill()`` before
                # cleaning up. P2-2: previously the wait timeout was
                # swallowed, leaving the wrapper spinning on a gate
                # file we immediately delete. The wrapper would never
                # see the gate, never exit, and the fallback plain
                # ``Popen`` would start a second unrelated process —
                # leaking a background PowerShell outside the Job
                # lifecycle.
                self._append_locked(
                    "Gated launcher: Job assignment failed; falling back to plain launch."
                )
                try:
                    wrapper_process.terminate()
                except Exception:
                    pass
                try:
                    wrapper_process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        wrapper_process.kill()
                    except Exception:
                        pass
                    try:
                        wrapper_process.wait(timeout=2)
                    except Exception:
                        pass
                except Exception:
                    pass
                gate.cleanup()
                job.close()

        # Plain Popen fallback (non-Windows or gated launch failed).
        try:
            process = subprocess.Popen(command, **popen_kwargs)
        except OSError as exc:
            self._append_locked(f"Failed to start process: {exc}")
            return None
        return process, None, None

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self._process or self._process.poll() is not None:
                raise ApiError(HTTPStatus.CONFLICT, "No active run to stop.")
            self._stopping = True
            self._append_locked("Stop requested.")
            process = self._process
            job = self._job

        try:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                with self._lock:
                    self._append_locked("Process did not stop in time; killed.")
        finally:
            # Closing the job handle terminates every descendant still
            # assigned to it (Claude / Codex CLI children spawned by the
            # PowerShell wrapper). This is the P2-1 fix: terminating the
            # direct PowerShell parent alone is not enough on Windows.
            # The ``finally`` guarantees the close still runs even if
            # ``terminate`` or ``wait`` raises — otherwise descendants
            # could outlive the parent and continue modifying the
            # project after the user has stopped the run.
            if job:
                try:
                    job.close()
                except Exception:
                    pass
                with self._lock:
                    if self._job is job:
                        self._job = None
        return self.snapshot() or {}

    def has_active_run(self) -> bool:
        """Return True iff a child process is currently running."""
        with self._lock:
            return bool(self._process and self._process.poll() is None)

    def stop_if_running(self) -> bool:
        """Stop the active run if one exists. Idempotent.

        Returns ``True`` when an active run was terminated (or at least
        attempted). Returns ``False`` when no run was active at the time
        of the call, so callers driving a shutdown path (e.g. the desktop
        app's ``stop_backend``) can invoke this unconditionally without
        distinguishing the "nothing to stop" case up front.

        Race window: the child may exit between the precondition check
        and ``stop()``. ``stop()`` would then raise ``ApiError`` (CONFLICT),
        which we treat as "no run needed stopping" since the process is
        gone anyway.

        Note: this method only closes the Job Object when the direct
        parent process is still alive. Callers that need to guarantee
        descendant cleanup even when the parent has already exited
        (e.g. desktop shutdown) should prefer :meth:`shutdown`.
        """
        if not self.has_active_run():
            return False
        try:
            self.stop()
            return True
        except ApiError:
            return False

    def shutdown(self) -> bool:
        """Forcefully stop the active run and release the Job Object handle.

        Unlike :meth:`stop_if_running`, this method does NOT gate on
        :meth:`has_active_run`. It unconditionally closes any open Job
        Object handle so descendants are reaped even when:

        * The PowerShell parent process has already exited but a
          descendant (Claude / Codex CLI) is still running inside the
          Job Object.
        * The reader thread is still blocked on inherited stdout and
          has not yet reached its natural-exit ``job.close()`` call.

        This is the P2-1 fix for the desktop shutdown path: because
        ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` only fires when the job
        handle is closed, leaving ``self._job`` open would allow
        descendants to outlive the desktop window and HTTP server.

        Round 8 P2-1: ``_stopping`` is set whenever there is an open Job
        OR a live wrapper process, not only when the wrapper is alive.
        When the wrapper has already exited but descendants are still
        running inside the Job, the reader thread can still be blocked
        on the inherited stdout pipe those descendants hold open; closing
        the Job kills them, the pipe closes, and the reader finalizes the
        wrapper's exit code. Without ``_stopping`` the reader would
        classify the run as ``finished``/``SUCCESS`` even though the user
        explicitly requested a stop — misreporting an aborted
        project-modifying run as a successful completion and updating
        ``lastResult`` incorrectly. ``_append_locked`` notifies the
        condition so any SSE consumer waiting on the run state wakes up
        and observes the stop request.

        Returns ``True`` when work was performed (a process was
        terminated or a Job handle was closed); ``False`` when there
        was nothing to clean up. Safe to call repeatedly — once the
        job is cleared, subsequent calls are no-ops.
        """
        with self._lock:
            process = self._process
            job = self._job
            process_alive = bool(
                process is not None and process.poll() is None
            )
            if process_alive or job is not None:
                self._stopping = True
                self._append_locked("Shutdown requested.")

        try:
            if process_alive and process is not None:
                try:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        with self._lock:
                            self._append_locked(
                                "Process did not stop in time; killed."
                            )
                except Exception:
                    # Terminate / wait / kill are best-effort. The
                    # finally below still closes the job so descendants
                    # are reaped even if the parent refused to die.
                    pass
        finally:
            # Close the Job handle UNCONDITIONALLY so descendants are
            # reaped even when the parent has already exited but a
            # child is still running inside the Job, or the reader
            # thread has not yet reached its natural-exit close().
            if job:
                try:
                    job.close()
                except Exception:
                    pass
                with self._lock:
                    # Only clear if it hasn't been replaced by a new run
                    # in the meantime (defensive — extremely unlikely).
                    if self._job is job:
                        self._job = None

        return process_alive or job is not None

    def shutdown_and_snapshot(self) -> dict[str, Any] | None:
        """Stop the active run, close the Job Object handle unconditionally,
        and return the current run snapshot.

        Used by the HTTP stop endpoint so a user pressing the web UI Stop
        button gets descendants reaped even when the direct wrapper
        process has already exited (which would make :meth:`stop` refuse
        with a 409 CONFLICT). Without this routing, a wrapper that has
        exited while Claude / Codex CLI children keep running inside the
        Job would leave the run visible as "running" until the whole
        desktop app exits.

        Returns the snapshot at the moment of the call. The reader
        thread may still finalize ``current`` afterwards; callers that
        need a stable terminal snapshot should rely on the SSE stream
        or the next ``GET /api/runs/current`` poll.
        """
        self.shutdown()
        return self.snapshot()

    def stream_events(self, last_index: int = 0):
        while True:
            with self._lock:
                while True:
                    current = self.current
                    logs = current.get("logs", []) if current else []
                    if last_index < len(logs):
                        new_logs = logs[last_index:]
                        last_index = len(logs)
                        event = {
                            "run": self.snapshot(),
                            "logs": new_logs,
                            "nextIndex": last_index,
                        }
                        break
                    if current and current.get("status") != "running":
                        event = {
                            "run": self.snapshot(),
                            "logs": [],
                            "nextIndex": last_index,
                            "done": True,
                        }
                        break
                    self._condition.wait(timeout=20)
                    if not self.current:
                        event = {"run": None, "logs": [], "nextIndex": last_index}
                        break
            yield event
            if event.get("done"):
                return

    def _append_locked(self, line: str) -> None:
        if self.current is not None:
            self.current["logs"].append(line.rstrip("\r\n"))
        self._condition.notify_all()

    def _append(self, line: str) -> None:
        with self._lock:
            self._append_locked(line)

    def _read_process(
        self,
        process: subprocess.Popen[str],
        run_id: str,
        job: "_Win32JobObject | None",
        gate: "_FileGate | None",
    ) -> None:
        """Per-run reader thread body.

        P2-1 fix: takes the run's process / id / Job / gate as parameters
        so the reader only ever mutates state that still belongs to this
        run. If a newer run has replaced ``self.current`` /
        ``self._process`` / ``self._job`` while this reader was reading
        stdout or waiting on the process, the reader still:

        * Appends stdout lines ONLY while ``self.current.id == run_id``.
        * Finalizes ``self.current`` / writes ``lastResult`` to the
          project ONLY when it still owns ``self.current``.
        * Clears ``self._process`` / ``self._job`` ONLY when those
          fields still point at this run's captured objects.
        * Closes THIS run's captured Job regardless (the Job Object
          itself is idempotent so this is safe even if ``stop()`` /
          ``shutdown()`` already closed it).

        Every blocking call (``stdout`` iteration, ``stdout.close()``,
        ``process.wait()``, ``store.update_project``) is wrapped so
        transient failures are logged into the run's history instead of
        escaping as ``PytestUnhandledThreadExceptionWarning``.
        """
        # --- stdout drain ---------------------------------------------------
        if process and process.stdout:
            try:
                for line in process.stdout:
                    with self._lock:
                        # Only append while this run still owns ``current``.
                        # A newer run that started after our process exited
                        # would otherwise have its logs polluted.
                        if self.current and self.current.get("id") == run_id:
                            self._append_locked(line)
            except Exception as exc:
                with self._lock:
                    if self.current and self.current.get("id") == run_id:
                        self._append_locked(f"stdout read error: {exc}")
            finally:
                try:
                    process.stdout.close()
                except Exception as exc:
                    with self._lock:
                        if self.current and self.current.get("id") == run_id:
                            self._append_locked(f"stdout close error: {exc}")

        # --- wait -----------------------------------------------------------
        exit_code: int | None = None
        try:
            exit_code = process.wait() if process else None
        except Exception as exc:
            # P2-1 fix: do not let wait() failures escape as background
            # thread exceptions. Record the failure into the run's
            # history (when we still own it) and proceed with
            # ``exit_code=None`` so the finalize path runs.
            with self._lock:
                if self.current and self.current.get("id") == run_id:
                    self._append_locked(f"wait error: {exc}")

        # --- finalize -------------------------------------------------------
        with self._lock:
            stopped = self._stopping
            owns_current = bool(self.current and self.current.get("id") == run_id)
            if owns_current:
                result = exit_code_to_result(exit_code, stopped=stopped)
                self.current["status"] = "stopped" if stopped else "finished"
                self.current["endedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self.current["exitCode"] = exit_code
                self.current["result"] = result
                self.current["logs"].append(
                    f"Finished with exit code {exit_code}: {result}"
                )
                try:
                    self.store.update_project(
                        self.current["projectId"],
                        {
                            "lastResult": result,
                            "lastExitCode": exit_code,
                            "lastRunAt": self.current["endedAt"],
                        },
                    )
                except Exception as exc:
                    self.current["logs"].append(
                        f"Failed to update project metadata: {exc}"
                    )

            # Clear shared fields ONLY when they still belong to this
            # run. A newer run that has installed its own process / Job
            # must not have them wiped out from underneath it.
            if self._process is process:
                self._process = None
            if self._job is job:
                self._job = None

            self._condition.notify_all()

        # Always close THIS run's captured Job so descendants are reaped
        # even when ``current`` was replaced. ``_Win32JobObject.close``
        # is idempotent for real Job Objects (handle becomes ``None``
        # after the first close), so a double-close after ``stop()`` /
        # ``shutdown()`` is safe.
        if job:
            try:
                job.close()
            except Exception:
                pass
        # Clean up the gate file (if any) for the same run.
        if gate:
            try:
                gate.cleanup()
            except Exception:
                pass


def initialize_project(project: dict[str, Any], store: ProjectStore) -> dict[str, Any]:
    target = Path(project["path"])
    if detect_project_kind(target) == "orchestrator":
        return store.update_project(project["id"], {"kind": "orchestrator"})

    (target / "scripts").mkdir(exist_ok=True)
    (target / "docs").mkdir(exist_ok=True)
    (target / ".claude").mkdir(exist_ok=True)

    copies = [
        (ROOT / "scripts" / "run-claude.ps1", target / "scripts" / "run-claude.ps1"),
        (ROOT / "docs" / "PLAN.template.md", target / "docs" / "PLAN.template.md"),
        (
            ROOT / "docs" / "IMPLEMENTATION_REPORT.template.md",
            target / "docs" / "IMPLEMENTATION_REPORT.template.md",
        ),
        (ROOT / "docs" / "CODEX_REVIEW.schema.json", target / "docs" / "CODEX_REVIEW.schema.json"),
        (ROOT / ".claude" / "settings.json", target / ".claude" / "settings.json"),
    ]
    for src, dest in copies:
        if src.exists() and not dest.exists():
            shutil.copy2(src, dest)

    plan = target / "docs" / "PLAN.md"
    plan_template = target / "docs" / "PLAN.template.md"
    if not plan.exists() and plan_template.exists():
        shutil.copy2(plan_template, plan)

    return store.update_project(project["id"], {"kind": "orchestrator"})


def read_plan(project: dict[str, Any]) -> dict[str, Any]:
    _validate_project_available(project)
    path = Path(project["path"]) / "docs" / "PLAN.md"
    if not path.exists():
        return {"exists": False, "content": ""}
    return {"exists": True, "content": path.read_text(encoding="utf-8", errors="replace")}


def write_plan(project: dict[str, Any], content: str) -> dict[str, Any]:
    _validate_project_available(project)
    path = Path(project["path"]) / "docs" / "PLAN.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"exists": True, "content": content}


def read_artifacts(project: dict[str, Any]) -> dict[str, Any]:
    _validate_project_available(project)
    project_path = Path(project["path"])
    artifacts: dict[str, Any] = {}
    for key, relative in ARTIFACTS.items():
        path = project_path / relative
        artifacts[key] = {
            "path": relative,
            "exists": path.exists(),
            "content": path.read_text(encoding="utf-8", errors="replace") if path.exists() else "",
        }
    return artifacts


def _validate_project_available(project: dict[str, Any]) -> Path:
    if project.get("available") is False:
        raise ApiError(
            HTTPStatus.CONFLICT,
            f"Project path is no longer available: {project.get('path', 'unknown')}",
        )
    return resolve_whitelisted_project(project)


def resolve_whitelisted_project(project: dict[str, Any]) -> Path:
    raw_path = str(project.get("path") or "")
    if not raw_path.strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, "Project path is missing.")
    try:
        project_path = Path(raw_path).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Project path does not exist.") from exc
    if not project_path.is_dir():
        raise ApiError(HTTPStatus.BAD_REQUEST, "Project path must be a directory.")
    assert_git_work_tree(project_path)
    return project_path


def set_task_status(task, target: str, message: str) -> None:
    task.set_status(transition(task.status, target), message)


def validate_task_project(task, project_store: ProjectStore) -> Path:
    project = project_store.get_project(task.projectId)
    try:
        project_path = resolve_whitelisted_project(project)
    except ApiError as exc:
        raise ApiError(
            HTTPStatus.CONFLICT,
            f"Target worktree path is no longer available: {project.get('path', 'unknown')}. {exc.message}",
        ) from exc
    try:
        task_path = Path(task.projectPath).expanduser().resolve(strict=True)
    except FileNotFoundError:
        raise ApiError(
            HTTPStatus.CONFLICT,
            f"Task project path no longer exists: {task.projectPath}",
        )
    if task_path != project_path:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Task project path no longer matches the project whitelist.")
    return project_path


def create_task(body: dict[str, Any], project_store: ProjectStore, task_store: TaskStore):
    project = project_store.get_project(str(body.get("projectId", "")))
    try:
        project_path = resolve_whitelisted_project(project)
    except ApiError as exc:
        task = task_store.create(
            project_id=str(project["id"]),
            project_path=str(project.get("path", "")),
            title=str(body.get("title") or ""),
            description=str(body.get("description") or ""),
            acceptance=str(body.get("acceptance") or ""),
            test_command=str(body.get("testCommand") or ""),
            max_rounds=clamp_max_rounds(body.get("maxRounds", 3)),
        )
        task.set_status(Status.BLOCKED, f"Target worktree path does not exist: {exc.message}")
        task_store.save(task)
        return task
    assert_clean_work_tree(project_path)
    task = task_store.create(
        project_id=str(project["id"]),
        project_path=str(project_path),
        title=str(body.get("title") or ""),
        description=str(body.get("description") or ""),
        acceptance=str(body.get("acceptance") or ""),
        test_command=str(body.get("testCommand") or ""),
        max_rounds=clamp_max_rounds(body.get("maxRounds", 3)),
    )
    task.repoId = project.get("repoId")
    task.worktreeType = project.get("worktreeType")
    task.worktreeBranch = project.get("branch")
    task_dir = task_store.task_dir(task.id)
    write_claude_implementation_prompt(task, task_dir)
    set_task_status(task, Status.WAITING_FOR_CLAUDE, "Initial Claude prompt generated.")
    task_store.save(task)
    return task


def create_project_worktree(
    project_id_value: str,
    body: dict[str, Any],
    project_store: "ProjectStore",
) -> dict[str, Any]:
    """Create a development worktree from the project's primary worktree.

    The new worktree is automatically registered as its own project so the
    project tree updates immediately.  Worktree creation only succeeds when
    the source is a primary worktree with a clean working tree, a valid
    branch name, and a non-existing target path.

    Codex P1-2 round 19: the complete operation — HEAD capture, clean
    check, filter/config checks, branch validation, ``git worktree
    add``, and registration/recovery response — runs under the primary
    worktree's per-resource ``RLock``.  Without serialisation, a
    concurrent controlled merge could advance main between the clean /
    filter checks and the ``git worktree add`` invocation, causing the
    new worktree to be checked out from a SHA that was never validated.
    Holding the same lock the controlled-merge service uses serialises
    the two operations on the same primary resource, while unrelated
    repositories remain free to proceed concurrently.

    A single starting SHA is captured inside the lock and passed
    explicitly to ``create_worktree`` as the final ``git worktree add``
    start-point argument so the checkout operates on the validated
    commit rather than the implicit HEAD.
    """
    project = project_store.get_project(project_id_value)
    if project.get("worktreeType") != "primary":
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "Worktrees can only be created from a primary worktree.",
        )
    if project.get("available") is False:
        raise ApiError(HTTPStatus.CONFLICT, "Primary worktree path is no longer available.")
    try:
        primary_path = Path(project["path"]).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Primary worktree path does not exist.") from exc
    branch = str(body.get("branch") or "").strip()
    target_raw = str(body.get("path") or "").strip()
    if not target_raw:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Target path is required.")
    try:
        target_path = Path(target_raw).expanduser().resolve(strict=False)
    except (OSError, ValueError) as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"Invalid target path: {exc}") from exc

    # Codex P1-2 round 19: hold the primary worktree's resource lock
    # around the complete operation so a concurrent controlled merge
    # cannot advance main between checks and creation.  Capture exactly
    # one starting SHA inside the lock and pass it explicitly to
    # ``create_worktree`` as the ``git worktree add`` start-point.
    with _resource_operation_lock(primary_path):
        head_result = subprocess.run(
            ["git", "-C", str(primary_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if head_result.returncode != 0 or not head_result.stdout.strip():
            stderr = (head_result.stderr or head_result.stdout or "").strip()
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "Failed to capture primary worktree HEAD before creating "
                "worktree; refusing to create worktree without a validated "
                f"start SHA. {stderr}",
            )
        captured_start_sha = head_result.stdout.strip()
        try:
            result = create_worktree(
                primary_path, branch, target_path, start_sha=captured_start_sha
            )
        except WorktreeCreationError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc

        new_path = Path(result["path"])
        # Codex P2-1 round 17: previously, ``git worktree add`` succeeded
        # but a subsequent registration failure raised a generic ``500``
        # error and left the new worktree on disk in an "orphan" state —
        # the directory existed, was tracked by Git as a linked worktree,
        # but did not appear in the project list, so the user had no
        # backend affordance to remove or re-import it.  Recover by:
        #
        # 1. Re-trying registration via the *primary* path's project store
        #    entry, whose ``_auto_discover_sibling_worktrees`` flow scans
        #    the repository's worktree list and adds every linked worktree
        #    that is not already registered.  ``add_project`` already
        #    invokes this flow as part of its normal post-registration
        #    bookkeeping; the retry here re-invokes it explicitly against
        #    the primary worktree path so the new linked worktree is
        #    picked up even when the new-path ``add_project`` itself is
        #    what failed.
        # 2. If the retry still cannot find / register the new worktree,
        #    return a partial-success payload (HTTP 201 with
        #    ``worktreeCreated: true`` and ``project: null``) plus
        #    recovery instructions so the frontend can surface the
        #    orphan worktree and guide the user to import it manually.
        #
        # Codex P1-2 round 19: the registration / recovery response is
        # produced inside the same resource lock so the entire
        # operation (HEAD capture → create → register → response) is
        # atomic with respect to concurrent controlled merges on the
        # same primary repository.
        new_project: dict[str, Any] | None = None
        registration_error: str | None = None
        try:
            new_project = project_store.add_project(new_path)
        except Exception as exc:
            registration_error = str(exc)

        if new_project is None:
            # Retry via the primary path's auto-discovery flow: that flow
            # calls ``list_worktrees`` against the primary worktree and
            # registers every sibling that is not already tracked, so the
            # newly-created linked worktree (now present in the Git
            # worktree list) is picked up even when the explicit
            # ``add_project(new_path)`` call above failed.
            try:
                project_store.add_project(primary_path)
            except Exception:
                # Best-effort retry; fall through to the partial-success
                # return below when both attempts fail.
                pass
            # Look up by resolved path: the project may have been added by
            # the auto-discovery retry above.
            try:
                resolved_new = new_path.resolve(strict=False)
            except (OSError, ValueError):
                resolved_new = new_path
            for candidate in project_store.list_projects():
                try:
                    candidate_path = Path(str(candidate.get("path", ""))).resolve(strict=False)
                except (OSError, ValueError):
                    continue
                if candidate_path == resolved_new:
                    new_project = candidate
                    registration_error = None
                    break

        if new_project is None:
            # Both registration attempts failed.  Return a partial-success
            # payload so the frontend can surface the orphan worktree and
            # the user can import it manually via the regular
            # ``POST /api/projects`` endpoint.
            recovery_instructions = (
                f"Worktree was created at {result['path']} on branch "
                f"'{result['branch']}' but could not be registered as a "
                f"project automatically. Open the project list and add the "
                f"path manually to import it. Original error: "
                f"{registration_error or 'unknown'}"
            )
            write_audit_log(
                "project.worktree.create.partial",
                "orphan",
                {
                    "sourceProjectId": project_id_value,
                    "sourcePath": str(primary_path),
                    "branch": result["branch"],
                    "newPath": result["path"],
                    "error": registration_error or "unknown",
                },
            )
            # Codex P2-2 round 19: include the top-level ``path`` field
            # so the frontend can surface the created path even when
            # ``project`` is ``null``.  Previously the partial-success
            # payload omitted ``path``, forcing the UI to read
            # ``data.project.path`` which is undefined in this branch;
            # the UI would then display ``(path unavailable)`` rather
            # than the real created path.
            return {
                "project": None,
                "path": result["path"],
                "branch": result["branch"],
                "worktreeCreated": True,
                "registeredAutomatically": False,
                "recoveryInstructions": recovery_instructions,
            }

        write_audit_log(
            "project.worktree.create",
            new_project["id"],
            {
                "sourceProjectId": project_id_value,
                "sourcePath": str(primary_path),
                "branch": result["branch"],
                "newPath": result["path"],
            },
        )
        return {"project": new_project, "branch": result["branch"], "worktreeCreated": True, "registeredAutomatically": True}


def commit_task_changes(
    task_id: str,
    body: dict[str, Any],
    project_store: ProjectStore,
    task_store: TaskStore,
) -> Task:
    """Commit a PASS task's changes via a controlled backend operation.

    The full ``load → validate → mutate Git → save`` span runs under a
    per-task ``RLock`` (Codex P1-4 round 15).  ``ThreadingHTTPServer``
    dispatches each request on its own thread; without serialisation,
    duplicate concurrent POSTs to ``/api/tasks/{id}/commit`` can both
    read the task in its pre-COMMITTED state, both proceed to execute
    the controlled commit, and the loser of the race then saves its
    own stale ``Task`` object — overwriting the winner's ``COMMITTED``
    metadata and making the task appear uncommitted even though HEAD
    has actually advanced.
    """
    with _task_operation_lock(task_id):
        return _commit_task_changes_locked(task_id, body, project_store, task_store)


def _commit_task_changes_locked(
    task_id: str,
    body: dict[str, Any],
    project_store: ProjectStore,
    task_store: TaskStore,
) -> Task:
    """Inner body of ``commit_task_changes`` — runs while holding the per-task lock."""
    # Reload inside the lock so concurrent requests see consistent state.
    # A previous in-flight request that already mutated ``commitSha`` will
    # short-circuit on the ``task.commitSha`` check below rather than
    # attempting a second commit that fails and overwrites the saved
    # metadata.
    task = task_store.load(task_id)
    if task.archivedAt:
        raise ApiError(HTTPStatus.CONFLICT, "Archived tasks cannot be committed.")
    if task.deletedAt:
        raise ApiError(HTTPStatus.CONFLICT, "Trashed tasks cannot be committed.")
    if task.status in RUNNING_TASK_STATUSES:
        raise ApiError(HTTPStatus.CONFLICT, "Running tasks cannot be committed.")
    if task.status != Status.PASS:
        raise ApiError(HTTPStatus.CONFLICT, "Only PASS tasks can be committed.")
    if task.commitSha:
        raise ApiError(HTTPStatus.CONFLICT, "Task has already been committed.")
    project_path = validate_task_project(task, project_store)
    message = str(body.get("message") or body.get("name") or "").strip()
    # The reviewed snapshot is captured at artifact-collection time so it
    # mirrors exactly what Codex reviewed.  If the snapshot is missing for
    # the current round (e.g. legacy PASS from before this fix, or capture
    # failed at artifact time), refuse to commit: there is no reviewed
    # baseline to compare against, so unreviewed changes could slip in.
    # ``reviewedHeadSha`` is required (Codex P1-1 round 14): without it,
    # the CAS ref update and the merge base reachability check would
    # silently no-op, allowing unreviewed history to slip into the trunk.
    if (
        task.reviewedRound is None
        or task.reviewedRound != task.round
        or not task.reviewedHeadSha
        or not task.reviewedStatusHash
        or not task.reviewedDiffHash
        or not task.reviewedTreeSha
    ):
        task.add_history(
            "COMMIT_BLOCKED",
            "No reviewed snapshot for the current round; re-run Claude completion "
            "and Codex review before committing.",
        )
        task_store.save(task)
        raise ApiError(
            HTTPStatus.CONFLICT,
            "Task has no reviewed snapshot for the current round; re-run the "
            "review cycle before committing.",
        )
    expected_snapshot: dict[str, str | None] = {
        "headSha": task.reviewedHeadSha,
        "statusHash": task.reviewedStatusHash,
        "diffHash": task.reviewedDiffHash,
        "treeSha": task.reviewedTreeSha,
    }
    # Codex P1-3 round 16: hold the per-resource lock around the actual
    # Git mutation so different tasks bound to the same worktree cannot
    # interleave ``git add -A`` / ``commit-tree`` / ``update-ref``
    # invocations against the same index and refs.  Reload the task
    # inside the lock so a concurrent task that committed while we were
    # waiting is observed (its ``commitSha`` populates and we surface
    # the conflict instead of running a second Git mutation that fails
    # the CAS check and overwrites audit state).
    with _resource_operation_lock(project_path):
        task = task_store.load(task_id)
        if task.archivedAt:
            raise ApiError(HTTPStatus.CONFLICT, "Archived tasks cannot be committed.")
        if task.deletedAt:
            raise ApiError(HTTPStatus.CONFLICT, "Trashed tasks cannot be committed.")
        if task.status in RUNNING_TASK_STATUSES:
            raise ApiError(HTTPStatus.CONFLICT, "Running tasks cannot be committed.")
        if task.status != Status.PASS:
            raise ApiError(HTTPStatus.CONFLICT, "Only PASS tasks can be committed.")
        if task.commitSha:
            raise ApiError(HTTPStatus.CONFLICT, "Task has already been committed.")
        try:
            result = controlled_commit(project_path, message, expected_snapshot=expected_snapshot)
        except (CommitError, GitError) as exc:
            # ``CommitError`` covers the explicit safety rejections (empty
            # worktree, .env, drift, in-progress operation, etc.).  ``GitError``
            # covers the underlying read-only helpers
            # (``compute_review_snapshot``, ``enumerate_env_violations``,
            # ``get_index_tree_sha``, ``find_clean_filtered_paths``, …) which
            # Codex P2-1 round 14 noted would otherwise bypass the
            # ``COMMIT_BLOCKED`` history record and surface as a different
            # status code via the generic ``do_POST`` handler.  Recording both
            # under the same history event keeps the audit trail consistent
            # and the user-facing status code uniform.
            task.add_history("COMMIT_BLOCKED", str(exc))
            task_store.save(task)
            raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc

    task.commitSha = result["commitSha"]
    task.commitShortSha = result.get("commitShortSha")
    task.commitMessage = result["commitMessage"]
    task.committedAt = utc_now_str()
    task.lastActivityAt = task.committedAt
    task.stage = "committed"
    history_message = (
        f"Worktree changes committed as {task.commitShortSha or task.commitSha[:10]}: {task.commitMessage}"
    )
    history_kwargs: dict[str, Any] = {
        "commitSha": task.commitSha,
        "commitShortSha": task.commitShortSha,
        "commitMessage": task.commitMessage,
    }
    # Codex P1-5 round 16: if HEAD drifted between the CAS ref update
    # and the post-commit observation, surface it in the audit trail
    # so the recorded ``commitSha`` (the object we authored) can be
    # reconciled with the live branch tip later.  The drift is not
    # blocking — the controlled commit succeeded.
    head_drift = result.get("headDriftSha")
    if head_drift:
        history_message += (
            f" Note: HEAD subsequently moved to {head_drift[:10]}; the recorded "
            "commitSha remains the controlled commit object."
        )
        history_kwargs["headDriftSha"] = head_drift
    task.add_history("COMMITTED", history_message, **history_kwargs)
    task_store.save(task)
    write_audit_log(
        "task.commit",
        task.id,
        {
            "projectId": task.projectId,
            "commitSha": task.commitSha,
            "commitMessage": task.commitMessage,
        },
    )
    return task


def merge_task_to_main(
    task_id: str,
    project_store: ProjectStore,
    task_store: TaskStore,
) -> Task:
    """Merge a committed task's branch into the main worktree.

    The full ``load → validate → mutate Git → save`` span runs under a
    per-task ``RLock`` (Codex P1-4 round 15) for the same reason as
    ``commit_task_changes``: a duplicate concurrent POST would otherwise
    race a previous in-flight request and the loser could overwrite the
    winner's ``MERGED`` metadata.
    """
    # Recovery runs before taking the requesting task lock.  Each journal
    # then acquires its own task lock followed by the primary resource lock;
    # this avoids ever attempting task acquisition while a resource lock is
    # held and keeps the global order task -> resource.
    try:
        task_hint = task_store.load(task_id)
        project_hint = project_store.get_project(task_hint.projectId)
        repo_id = project_hint.get("repoId") or task_hint.repoId
        primary_hint = _find_primary_project_for_repo(
            project_store, repo_id, project_hint
        )
        if primary_hint and primary_hint.get("path"):
            primary_path_hint = Path(str(primary_hint["path"])).expanduser().resolve(
                strict=False
            )
            _recover_pending_merges_for_primary(
                primary_path_hint, task_store, task_id
            )
    except (ApiError, TaskStoreError, OSError, ValueError):
        # The locked implementation below performs authoritative validation.
        pass
    with _task_operation_lock(task_id):
        return _merge_task_to_main_locked(task_id, project_store, task_store)


def _reviewed_base_block_reason(task) -> str | None:
    """Return a blocking reason when the reviewed baseline is unavailable.

    Codex P1-3 round 19: the GUI merge path must never invoke
    ``controlled_merge_to_main`` in compatibility mode (i.e. without a
    validated ``expected_base_sha``).  When ``reviewedHeadSha`` is empty
    or ``reviewedRound`` does not match the current round, the
    lower-level reachability + sole-parent checks silently no-op, so
    unreviewed pre-task commits could slip into the trunk alongside the
    reviewed one.  This helper returns a short reason string describing
    why the merge is blocked (suitable for the ``MERGE_BLOCKED`` audit
    history) or ``None`` when the reviewed baseline is present and
    matches the current round.
    """
    if task.reviewedRound is None:
        return (
            "No reviewed snapshot exists for this task; the merge cannot "
            "verify the reviewed base is current. Re-run Claude completion "
            "and Codex review before merging."
        )
    if task.reviewedRound != task.round:
        return (
            f"Reviewed snapshot is from round {task.reviewedRound} but the "
            f"current round is {task.round}; the reviewed base may not "
            f"reflect the committed change. Re-run Claude completion and "
            f"Codex review before merging."
        )
    if not task.reviewedHeadSha:
        return (
            "Reviewed HEAD SHA is missing; the merge cannot verify the "
            "reviewed base reachability. Re-run Claude completion and "
            "Codex review before merging."
        )
    return None


def _task_journal_identity_error(task, data: dict[str, Any]) -> str | None:
    """Return why task metadata cannot be safely reconciled from ``data``."""
    if task.id != str(data.get("taskId") or ""):
        return "Journal task id does not match the locked task."
    if task.round != data.get("taskRound"):
        return "Journal task round no longer matches the locked task."
    if (task.commitSha or "").lower() != str(data.get("sourceCommitSha") or "").lower():
        return "Journal source commit no longer matches the task commit."
    if (task.reviewedHeadSha or "").lower() != str(data.get("reviewedBaseSha") or "").lower():
        return "Journal reviewed baseline no longer matches the task."
    if (task.worktreeBranch or "") != str(data.get("sourceBranch") or ""):
        return "Journal source branch no longer matches the task worktree branch."
    if task.mergedAt and (task.mergeCommitSha or "").lower() != str(
        data.get("newMergeCommitSha") or ""
    ).lower():
        return "Task is already marked merged with a different merge commit."
    return None


def _history_has_operation(task, event: str, operation_id: str) -> bool:
    return any(
        item.get("event") == event and item.get("operationId") == operation_id
        for item in task.history
    )


def _persist_completed_merge_journal(
    task,
    task_store: "TaskStore",
    journal: MergeRecoveryJournal,
    data: dict[str, Any],
    *,
    project_id: str,
    head_drift_sha: str | None = None,
    recovery_reason: str | None = None,
) -> None:
    """Persist task + audit for a materialised merge, then remove its journal."""
    identity_error = _task_journal_identity_error(task, data)
    if identity_error:
        raise MergeError(identity_error + " Manual reconciliation is required.")
    operation_id = str(data["operationId"])
    merge_sha = str(data["newMergeCommitSha"])
    short_sha = merge_sha[:10]
    short_result = subprocess.run(
        ["git", "-C", str(data["primaryPath"]), "rev-parse", "--short", merge_sha],
        capture_output=True,
        text=True,
        check=False,
    )
    if short_result.returncode == 0 and short_result.stdout.strip():
        short_sha = short_result.stdout.strip()
    if not task.mergedAt:
        task.mergedAt = utc_now_str()
        task.mergeCommitSha = merge_sha
        task.mergeShortSha = short_sha
        task.mergeTargetBranch = str(data["targetBranch"])
        task.mergeSourceBranch = str(data["sourceBranch"])
        task.headDriftSha = head_drift_sha
        task.lastActivityAt = task.mergedAt
        task.stage = "merged"
    if not _history_has_operation(task, "MERGED", operation_id):
        message = (
            f"Branch '{data['sourceBranch']}' merged into '{data['targetBranch']}' "
            f"as {task.mergeShortSha or short_sha}"
        )
        if head_drift_sha:
            message += (
                f" Note: HEAD subsequently moved to {head_drift_sha[:10]}; "
                "manual reconciliation is required."
            )
        task.add_history(
            "MERGED",
            message,
            operationId=operation_id,
            mergeCommitSha=merge_sha,
            mergeShortSha=task.mergeShortSha or short_sha,
            mergeTargetBranch=str(data["targetBranch"]),
            mergeSourceBranch=str(data["sourceBranch"]),
            **({"headDriftSha": head_drift_sha} if head_drift_sha else {}),
        )
    if recovery_reason and not _history_has_operation(task, "MERGE_RECOVERY", operation_id):
        task.add_history(
            "MERGE_RECOVERY", recovery_reason, operationId=operation_id, action="completed"
        )
    task_store.save(task)
    if str(data.get("phase")) != "audit_persisted":
        journal.advance("task_persisted")
    audit_details = {
        "projectId": project_id,
        "mergeCommitSha": merge_sha,
        "mergeTargetBranch": str(data["targetBranch"]),
        "mergeSourceBranch": str(data["sourceBranch"]),
    }
    if head_drift_sha:
        audit_details["headDriftSha"] = head_drift_sha
    _write_merge_audit_once("task.merge", task.id, operation_id, audit_details)
    if head_drift_sha:
        try:
            still_reachable = is_ancestor(
                Path(str(data["primaryPath"])), merge_sha, head_drift_sha
            )
        except GitError as exc:
            _write_merge_audit_once(
                "task.merge.reachability_probe_failed",
                task.id,
                operation_id,
                {
                    "projectId": project_id,
                    "mergeCommitSha": merge_sha,
                    "liveHeadSha": head_drift_sha,
                    "mergeTargetBranch": str(data["targetBranch"]),
                    "reason": str(exc),
                },
            )
            still_reachable = None
        except Exception as exc:
            _write_merge_audit_once(
                "task.merge.reachability_probe_failed",
                task.id,
                operation_id,
                {
                    "projectId": project_id,
                    "mergeCommitSha": merge_sha,
                    "liveHeadSha": head_drift_sha,
                    "mergeTargetBranch": str(data["targetBranch"]),
                    "reason": f"unexpected probe error: {exc}",
                },
            )
            still_reachable = None
        if still_reachable is False:
            _write_merge_audit_once(
                "task.merge.head_unreachable",
                task.id,
                operation_id,
                {
                    "projectId": project_id,
                    "mergeCommitSha": merge_sha,
                    "liveHeadSha": head_drift_sha,
                    "mergeTargetBranch": str(data["targetBranch"]),
                },
            )
    if recovery_reason:
        _write_merge_audit_once(
            "task.merge.recovery.completed",
            task.id,
            operation_id,
            {"primaryPath": str(data["primaryPath"]), "reason": recovery_reason},
        )
    if str(data.get("phase")) != "audit_persisted":
        journal.advance("audit_persisted")
    if head_drift_sha:
        _write_merge_audit_once(
            "task.merge.recovery.blocked",
            task.id,
            operation_id,
            {
                "primaryPath": str(data["primaryPath"]),
                "reason": "HEAD drifted after materialisation; journal retained for manual reconciliation.",
            },
        )
        return
    journal.delete()


def _persist_rolled_back_merge_journal(
    task,
    task_store: "TaskStore",
    journal: MergeRecoveryJournal,
    data: dict[str, Any],
    reason: str,
) -> None:
    """Persist a rolled-back outcome and only then remove the journal."""
    identity_error = _task_journal_identity_error(task, data)
    if identity_error:
        raise MergeError(identity_error + " Manual reconciliation is required.")
    operation_id = str(data["operationId"])
    if not _history_has_operation(task, "MERGE_RECOVERY", operation_id):
        task.add_history(
            "MERGE_RECOVERY", reason, operationId=operation_id, action="rolled_back"
        )
    task_store.save(task)
    if str(data.get("phase")) != "rollback_audit_persisted":
        journal.advance("rollback_task_persisted")
    _write_merge_audit_once(
        "task.merge.recovery.rolled_back",
        task.id,
        operation_id,
        {"primaryPath": str(data["primaryPath"]), "reason": reason},
    )
    if str(data.get("phase")) != "rollback_audit_persisted":
        journal.advance("rollback_audit_persisted")
    journal.delete()


def _record_blocked_recovery(
    task_store: "TaskStore",
    data: dict[str, Any],
    operation_id: str,
    reason: str,
) -> None:
    task_id = str(data.get("taskId") or operation_id)
    try:
        task = task_store.load(task_id)
        if not _history_has_operation(task, "MERGE_BLOCKED", operation_id):
            task.add_history(
                "MERGE_BLOCKED", reason, operationId=operation_id, action="recovery_blocked"
            )
            task_store.save(task)
    except Exception:
        pass
    _write_merge_audit_once(
        "task.merge.recovery.blocked",
        task_id,
        operation_id,
        {"primaryPath": str(data.get("primaryPath") or ""), "reason": reason},
    )


def _recover_one_merge_journal(
    journal: MergeRecoveryJournal,
    task_store: "TaskStore",
    *,
    expected_primary: Path | None = None,
) -> dict[str, Any] | None:
    """Recover one journal while enforcing task-lock -> resource-lock order."""
    initial = journal.read()
    if initial is None:
        if journal.exists():
            reason = "Recovery journal is unreadable; manual reconciliation is required."
            _write_merge_audit_once(
                "task.merge.recovery.blocked", journal.operation_id, journal.operation_id,
                {"reason": reason},
            )
            return {"action": "blocked", "reason": reason}
        return None
    task_id = str(initial.get("taskId") or "")
    if not task_id.startswith("task_"):
        reason = "Recovery journal has no valid task identity; manual reconciliation is required."
        _record_blocked_recovery(task_store, initial, journal.operation_id, reason)
        return {"action": "blocked", "reason": reason}
    with _task_operation_lock(task_id):
        data = journal.read()
        if data is None:
            return None
        journal_primary = str(data.get("primaryPath") or "").strip()
        if not journal_primary:
            reason = "Recovery journal has no primary path identity."
            _record_blocked_recovery(task_store, data, journal.operation_id, reason)
            return {"action": "blocked", "reason": reason}
        try:
            primary_path = Path(journal_primary).expanduser().resolve(strict=False)
        except (OSError, ValueError):
            primary_path = Path(journal_primary)
        if expected_primary and _resource_lock_key(primary_path) != _resource_lock_key(expected_primary):
            return None
        with _resource_operation_lock(primary_path):
            data = journal.read()
            if data is None:
                return None
            try:
                task = task_store.load(task_id)
            except Exception as exc:
                reason = (
                    f"Recovery task metadata is unavailable: {exc}. Manual "
                    "reconciliation is required before Git state can change."
                )
                _record_blocked_recovery(
                    task_store, data, journal.operation_id, reason
                )
                return {"action": "blocked", "reason": reason}
            identity_error = _task_journal_identity_error(task, data)
            if identity_error:
                reason = identity_error + " Manual reconciliation is required."
                _record_blocked_recovery(
                    task_store, data, journal.operation_id, reason
                )
                return {"action": "blocked", "reason": reason}
            try:
                outcome = recover_pending_merge(primary_path, journal)
            except Exception as exc:
                outcome = {
                    "action": "blocked",
                    "reason": f"Recovery probe failed: {exc}. Manual reconciliation is required.",
                }
            if outcome is None:
                return None
            reason = str(outcome.get("reason") or "Recovery outcome was not classified.")
            if outcome.get("action") == "blocked":
                _record_blocked_recovery(task_store, data, journal.operation_id, reason)
                return outcome
            try:
                task = task_store.load(task_id)
                data = journal.read() or data
                if outcome.get("action") == "completed":
                    _persist_completed_merge_journal(
                        task,
                        task_store,
                        journal,
                        data,
                        project_id=task.projectId,
                        recovery_reason=reason,
                    )
                else:
                    _persist_rolled_back_merge_journal(
                        task, task_store, journal, data, reason
                    )
            except Exception as exc:
                blocked_reason = (
                    f"Git recovery reached {outcome.get('action')}, but task/audit "
                    f"persistence could not be proven: {exc}. Manual reconciliation is required."
                )
                _record_blocked_recovery(
                    task_store, data, journal.operation_id, blocked_reason
                )
                return {"action": "blocked", "reason": blocked_reason}
            return outcome


def _recover_pending_merges_for_primary(
    primary_path: Path,
    task_store: "TaskStore",
    requesting_task_id: str | None = None,
) -> list[dict[str, Any]]:
    """Run recovery for every pending merge journal targeting ``primary_path``.

    Codex P1-1 round 19.  Acquires each journal's task lock and then its
    primary resource lock before recovery so a crash between forward CAS
    and materialisation on a previous lifecycle is recovered before a
    new merge attempts to use the same primary repository.  Each
    recovery outcome is recorded as a separate audit event so the
    audit trail distinguishes "completed by recovery", "rolled back by
    recovery", and "blocked — manual reconciliation required".

    Returns the list of recovery outcomes for inspection / testing.
    Never raises: a recovery failure is surfaced as a ``blocked``
    outcome and an audit record, not as an exception that would mask
    the original merge request.
    """
    recovery_dir = _merge_recovery_dir(task_store)
    if not recovery_dir.is_dir():
        return []
    outcomes: list[dict[str, Any]] = []
    try:
        candidates = sorted(recovery_dir.glob("*.json"))
    except OSError:
        return []
    for journal_file in candidates:
        operation_id = journal_file.stem
        try:
            journal = MergeRecoveryJournal(recovery_dir, operation_id)
        except ValueError:
            # Stale / hostile filename — best-effort skip so recovery
            # does not crash the merge request.
            continue
        data = journal.read() or {}
        outcome = _recover_one_merge_journal(
            journal, task_store, expected_primary=primary_path
        )
        if outcome is not None:
            outcomes.append(
                {
                    "operationId": operation_id,
                    "taskId": data.get("taskId"),
                    "primaryPath": data.get("primaryPath"),
                    "requestingTaskId": requesting_task_id,
                    **outcome,
                }
            )
    return outcomes


def _merge_task_to_main_locked(
    task_id: str,
    project_store: ProjectStore,
    task_store: TaskStore,
) -> Task:
    """Inner body of ``merge_task_to_main`` — runs while holding the per-task lock."""
    # Reload inside the lock so concurrent requests see consistent state.
    task = task_store.load(task_id)
    if task.archivedAt:
        raise ApiError(HTTPStatus.CONFLICT, "Archived tasks cannot be merged.")
    if task.deletedAt:
        raise ApiError(HTTPStatus.CONFLICT, "Trashed tasks cannot be merged.")
    if task.status in RUNNING_TASK_STATUSES:
        raise ApiError(HTTPStatus.CONFLICT, "Running tasks cannot be merged.")
    if not task.commitSha:
        raise ApiError(HTTPStatus.CONFLICT, "Task must be committed before merging.")
    if task.mergedAt:
        raise ApiError(HTTPStatus.CONFLICT, "Task has already been merged.")
    # Codex P1-3 round 19: the GUI merge path must NEVER use the
    # lower-level ``controlled_merge_to_main`` compatibility mode
    # (``expected_base_sha`` optional).  When ``reviewedHeadSha`` is
    # missing or ``reviewedRound`` does not match the current round,
    # the lower-level reachability + sole-parent checks silently
    # no-op, which would let unreviewed pre-task commits slip into
    # the trunk.  Block at the GUI boundary BEFORE any Git call so
    # the rejection is recorded as ``MERGE_BLOCKED`` with a clear
    # reason, then re-check after acquiring the resource lock in
    # case a concurrent flow cleared the reviewed snapshot between
    # the two checks.
    reviewed_block = _reviewed_base_block_reason(task)
    if reviewed_block is not None:
        task.add_history("MERGE_BLOCKED", reviewed_block)
        task_store.save(task)
        raise ApiError(HTTPStatus.CONFLICT, reviewed_block)
    project = project_store.get_project(task.projectId)
    repo_id = project.get("repoId") or task.repoId
    primary_project = _find_primary_project_for_repo(project_store, repo_id, project)
    if primary_project is None:
        raise ApiError(HTTPStatus.CONFLICT, "No available primary worktree for this repository.")
    if primary_project.get("available") is False:
        raise ApiError(HTTPStatus.CONFLICT, "Primary worktree path is no longer available.")
    try:
        primary_path = Path(primary_project["path"]).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise ApiError(HTTPStatus.CONFLICT, "Primary worktree path does not exist.") from exc
    source_branch = task.worktreeBranch or project.get("branch")
    if not source_branch:
        raise ApiError(HTTPStatus.CONFLICT, "Source branch is unknown; cannot merge.")
    # Codex P1-3 round 16: hold the per-resource lock on the primary
    # worktree around the actual Git merge so different tasks whose
    # worktrees share the same primary repository cannot interleave
    # controlled merge-tree / commit-tree / CAS / materialisation sequences
    # against the same index and refs.  Reload
    # the task inside the lock so a concurrent task that merged while we
    # were waiting is observed and surfaced as a conflict.
    with _resource_operation_lock(primary_path):
        task = task_store.load(task_id)
        if task.archivedAt:
            raise ApiError(HTTPStatus.CONFLICT, "Archived tasks cannot be merged.")
        if task.deletedAt:
            raise ApiError(HTTPStatus.CONFLICT, "Trashed tasks cannot be merged.")
        if task.status in RUNNING_TASK_STATUSES:
            raise ApiError(HTTPStatus.CONFLICT, "Running tasks cannot be merged.")
        if not task.commitSha:
            raise ApiError(HTTPStatus.CONFLICT, "Task must be committed before merging.")
        if task.mergedAt:
            raise ApiError(HTTPStatus.CONFLICT, "Task has already been merged.")
        # Codex P1-3 round 19: re-check the reviewed baseline after
        # acquiring the resource lock — a concurrent flow that cleared
        # the reviewed snapshot while we were waiting on the resource
        # lock must not slip through.
        reviewed_block = _reviewed_base_block_reason(task)
        if reviewed_block is not None:
            task.add_history("MERGE_BLOCKED", reviewed_block)
            task_store.save(task)
            raise ApiError(HTTPStatus.CONFLICT, reviewed_block)
        # Recovery was attempted before taking this task lock.  Under the
        # resource lock, fail closed if any same-primary journal remains;
        # do not acquire another task lock from inside this lock span.
        pending_operations: list[str] = []
        recovery_dir = _merge_recovery_dir(task_store)
        if recovery_dir.is_dir():
            try:
                journal_files = sorted(recovery_dir.glob("*.json"))
            except OSError:
                journal_files = []
            for journal_file in journal_files:
                try:
                    pending = MergeRecoveryJournal(
                        recovery_dir, journal_file.stem
                    ).read()
                except ValueError:
                    continue
                if not pending:
                    continue
                try:
                    same_primary = _resource_lock_key(
                        Path(str(pending.get("primaryPath") or ""))
                    ) == _resource_lock_key(primary_path)
                except (OSError, ValueError):
                    same_primary = False
                if same_primary:
                    pending_operations.append(journal_file.stem)
        if pending_operations:
            reason = (
                "A durable merge recovery journal is still pending for this "
                "primary repository; refusing a new merge until recovery or "
                "manual reconciliation completes. Operations: "
                + ", ".join(pending_operations)
            )
            task.add_history("MERGE_BLOCKED", reason)
            task_store.save(task)
            raise ApiError(HTTPStatus.CONFLICT, reason)
        primary_identity = get_git_common_dir(primary_path)
        if not primary_identity:
            reason = (
                "Failed to resolve the primary repository identity; refusing "
                "to create a recovery journal for an unclassified repository."
            )
            task.add_history("MERGE_BLOCKED", reason)
            task_store.save(task)
            raise ApiError(HTTPStatus.CONFLICT, reason)
        operation_id = f"task-{task.id}-round-{task.round}-{int(time.time() * 1000)}"
        journal = MergeRecoveryJournal(recovery_dir, operation_id)
        try:
            # Pass the reviewed commit SHA so the merge refuses to fast-forward
            # over any commits the user added to the branch externally after the
            # controlled commit landed.  Pass the reviewed HEAD (captured at
            # artifact time) so the merge refuses to sweep any unreviewed commits
            # that pre-date the task into the trunk (Codex P1-1 round 12).
            # Codex P1-1 round 19: pass the durable recovery journal so a
            # crash between forward CAS and materialisation can be
            # deterministically recovered.
            result = controlled_merge_to_main(
                primary_path,
                source_branch,
                expected_commit_sha=task.commitSha,
                expected_base_sha=task.reviewedHeadSha,
                recovery_journal=journal,
                operation_id=operation_id,
                task_id=task.id,
                task_round=task.round,
                primary_identity=primary_identity,
            )
            journal_data = journal.read()
            if journal_data is None:
                raise MergeError(
                    "Merge materialised but its durable recovery journal is "
                    "missing or unreadable; manual reconciliation is required."
                )
            _persist_completed_merge_journal(
                task,
                task_store,
                journal,
                journal_data,
                project_id=task.projectId,
                head_drift_sha=result.get("headDriftSha"),
            )
            return task
        except (MergeError, GitError) as exc:
            # ``MergeError`` covers the explicit merge safety rejections
            # (dirty main, missing branch, conflict, branch moved, parent
            # mismatch).  ``GitError`` covers the underlying read-only
            # helpers (``get_branch_head``, ``is_ancestor``,
            # ``get_commit_parents``, ``git_status``, …) which Codex P2-1
            # round 14 noted would otherwise bypass the ``MERGE_BLOCKED``
            # history record.  Recording both under the same history event
            # keeps the audit trail consistent.
            task.add_history(
                "MERGE_BLOCKED", str(exc), operationId=operation_id
            )
            task_store.save(task)
            _write_merge_audit_once(
                "task.merge.blocked",
                task.id,
                operation_id,
                {
                    "projectId": task.projectId,
                    "primaryPath": str(primary_path),
                    "sourceBranch": source_branch,
                    "sourceCommitSha": task.commitSha,
                    "reviewedBaseSha": task.reviewedHeadSha,
                    "reason": str(exc),
                },
            )
            if journal.exists():
                try:
                    recovery = recover_pending_merge(primary_path, journal)
                    if recovery and recovery.get("action") == "rolled_back":
                        journal.advance("rollback_task_persisted")
                        journal.advance("rollback_audit_persisted")
                        journal.delete()
                    elif recovery and recovery.get("action") == "blocked":
                        _record_blocked_recovery(
                            task_store,
                            journal.read() or {},
                            operation_id,
                            str(recovery.get("reason") or exc),
                        )
                except Exception as recovery_exc:
                    _record_blocked_recovery(
                        task_store,
                        journal.read() or {},
                        operation_id,
                        f"Failed to finalize rolled-back merge journal: {recovery_exc}. "
                        "Manual reconciliation is required.",
                    )
            raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc

def _find_primary_project_for_repo(
    project_store: "ProjectStore",
    repo_id: str | None,
    fallback: dict[str, Any],
) -> dict[str, Any] | None:
    if not repo_id:
        if fallback.get("worktreeType") == "primary" and fallback.get("available") is not False:
            return fallback
        return None
    projects = project_store.list_projects()
    primary_match = None
    for project in projects:
        if project.get("repoId") != repo_id:
            continue
        if project.get("worktreeType") != "primary":
            continue
        if project.get("available") is False:
            continue
        primary_match = project
        break
    if primary_match is None and fallback.get("mainWorktreePath"):
        for project in projects:
            if ProjectStore._same_path(
                str(project.get("path", "")),
                str(fallback.get("mainWorktreePath", "")),
            ):
                if project.get("available") is not False:
                    return project
    return primary_match


def launch_claude_task(task_id: str, project_store: ProjectStore, task_store: TaskStore):
    """Launch the Claude CLI window for a task in ``WAITING_FOR_CLAUDE``.

    Codex P1-4 round 18: the full ``load → validate → launch → save``
    span runs under the per-task ``RLock`` (same as
    ``commit_task_changes`` / ``merge_task_to_main``).  Without
    serialisation, duplicate concurrent POSTs to
    ``/api/tasks/{id}/launch-claude`` can both load the task in
    ``WAITING_FOR_CLAUDE``, both validate, and both launch CLI
    windows — the loser then saves its ``Task`` object and overwrites
    the winner's ``claudeWindow`` metadata, leaving the user with two
    running CLI windows but only one recorded on the task.
    """
    with _task_operation_lock(task_id):
        return _launch_claude_task_locked(task_id, project_store, task_store)


def _launch_claude_task_locked(task_id: str, project_store: ProjectStore, task_store: TaskStore):
    task = task_store.load(task_id)
    validate_task_project(task, project_store)
    if task.status != Status.WAITING_FOR_CLAUDE:
        raise StateTransitionError(f"Claude can only be launched from {Status.WAITING_FOR_CLAUDE}.")
    task_dir = task_store.task_dir(task.id)
    prompt_path = task_dir / (
        "CLAUDE_IMPLEMENT_PROMPT.md" if task.round == 1 else f"FIX_PROMPT_ROUND_{task.round}.md"
    )
    if not prompt_path.is_file():
        raise ApiError(HTTPStatus.CONFLICT, "Claude prompt has not been generated for this round.")
    settings = load_settings(SETTINGS_FILE)
    adapter = ClaudeCliWindowAdapter(settings["claudeCommand"])
    task.claudeWindow = adapter.launch(task, task_dir, prompt_path)
    task.add_artifact(prompt_path.name, prompt_path.name)
    task.add_artifact(Path(task.claudeWindow["script"]).name, Path(task.claudeWindow["script"]).name)
    task.progress = 20
    task.stage = "claude_running"
    task.activeClient = "claude"
    task.lastActivityAt = utc_now_str()
    set_task_status(task, Status.CLAUDE_WINDOW_STARTED, "Claude CLI window launched.")
    task_store.save(task)
    return task


def complete_claude_task(task_id: str, project_store: ProjectStore, task_store: TaskStore):
    """Collect Claude artifacts and advance the task to ``WAITING_FOR_CODEX``.

    Codex P1-4 round 18: the full ``load → collect artifacts → save``
    span runs under the per-task ``RLock`` so a concurrent
    ``launch_claude`` / ``cancel`` / ``archive`` request cannot race
    the artifact-collection / state-transition flow.  Without the
    lock, two requests could both observe ``CLAUDE_WINDOW_STARTED``,
    both compute snapshots, and the loser would overwrite the winner's
    ``reviewedRound`` / ``reviewedHeadSha`` / etc. metadata with stale
    values captured mid-flight.
    """
    with _task_operation_lock(task_id):
        return _complete_claude_task_locked(task_id, project_store, task_store)


def _complete_claude_task_locked(task_id: str, project_store: ProjectStore, task_store: TaskStore):
    task = task_store.load(task_id)
    project_path = validate_task_project(task, project_store)
    if task.status != Status.CLAUDE_WINDOW_STARTED:
        raise StateTransitionError(f"Claude can only be completed from {Status.CLAUDE_WINDOW_STARTED}.")
    task_dir = task_store.task_dir(task.id)

    # Capture the reviewed snapshot BEFORE artifact collection starts.
    # ``collect_git_artifacts`` writes the diff/status files Codex will
    # review, so the snapshot must represent the worktree state at the
    # exact moment those files are generated.  After artifacts return we
    # re-compute the snapshot and compare: any drift between the two
    # means the artifacts Codex reviews and the snapshot we record as
    # the "reviewed baseline" diverge, in which case the snapshot is
    # discarded so a later PASS cannot be verified against an
    # unreviewed state (Codex P1-1 round 11).
    pre_snapshot: dict[str, str | None] | None
    try:
        pre_snapshot = compute_review_snapshot(project_path)
    except EnvFileChangedError as exc:
        pre_snapshot = None
        task.reviewedRound = None
        task.reviewedHeadSha = None
        task.reviewedStatusHash = None
        task.reviewedDiffHash = None
        task.reviewedTreeSha = None
        for name in (
            f"git_status_round_{task.round}.txt",
            f"git_diff_stat_round_{task.round}.txt",
            f"git_diff_round_{task.round}.diff",
        ):
            if (task_dir / name).exists():
                task.add_artifact(name, name)
        task.progress = 20
        task.activeClient = None
        task.stage = "git_collection_failed"
        task.lastActivityAt = utc_now_str()
        set_task_status(task, Status.FAILED, str(exc))
        task_store.save(task)
        return task
    except GitError as exc:
        pre_snapshot = None
        task.reviewedRound = None
        task.reviewedHeadSha = None
        task.reviewedStatusHash = None
        task.reviewedDiffHash = None
        task.reviewedTreeSha = None
        task.add_history(
            "REVIEW_SNAPSHOT_FAILED",
            f"Failed to capture reviewed Git snapshot before artifact collection: {exc}",
        )

    try:
        git_artifacts = collect_git_artifacts(project_path, task_dir, task.round)
        task.add_artifact(git_artifacts.status_path.name, git_artifacts.status_path.name)
        task.add_artifact(git_artifacts.diff_stat_path.name, git_artifacts.diff_stat_path.name)
        task.add_artifact(git_artifacts.diff_path.name, git_artifacts.diff_path.name)
    except EnvFileChangedError as exc:
        for name in (
            f"git_status_round_{task.round}.txt",
            f"git_diff_stat_round_{task.round}.txt",
            f"git_diff_round_{task.round}.diff",
        ):
            if (task_dir / name).exists():
                task.add_artifact(name, name)
        task.progress = 20
        task.activeClient = None
        task.stage = "git_collection_failed"
        task.lastActivityAt = utc_now_str()
        set_task_status(task, Status.FAILED, str(exc))
        task_store.save(task)
        return task
    except GitError as exc:
        task.progress = 20
        task.activeClient = None
        task.stage = "git_collection_failed"
        task.lastActivityAt = utc_now_str()
        set_task_status(task, Status.FAILED, f"Git artifact collection failed: {exc}")
        task_store.save(task)
        return task

    # Capture the reviewed snapshot again AFTER artifact collection and
    # verify the worktree did not mutate while artifacts were being
    # generated.  ``collect_git_artifacts`` only reads from the worktree
    # (never mutates), so on a quiescent worktree the pre- and
    # post-collection snapshots are identical.  If they differ, an
    # external editor / process changed the worktree mid-collection and
    # the artifacts Codex reviews no longer correspond to the recorded
    # snapshot.  Discard the snapshot in that case so PASS-time
    # verification blocks instead of approving unreviewed content
    # (Codex P1-1 round 11).
    snapshot_persisted = False
    if pre_snapshot is not None:
        try:
            post_snapshot = compute_review_snapshot(project_path)
        except GitError as exc:
            post_snapshot = None
            task.add_history(
                "REVIEW_SNAPSHOT_FAILED",
                f"Failed to re-verify reviewed Git snapshot after artifact collection: {exc}",
            )
        except EnvFileChangedError as exc:
            post_snapshot = None
            task.add_history(
                "REVIEW_SNAPSHOT_FAILED",
                f"A .env file appeared during artifact collection: {exc}",
            )
        else:
            if post_snapshot != pre_snapshot:
                post_snapshot = None
                task.add_history(
                    "REVIEW_SNAPSHOT_FAILED",
                    "Worktree mutated between pre-artifact and post-artifact "
                    "snapshot capture; the recorded snapshot cannot be trusted "
                    "to match what Codex reviewed. Re-run Claude completion on a "
                    "quiescent worktree.",
                )
        if post_snapshot is not None:
            task.reviewedRound = task.round
            task.reviewedHeadSha = post_snapshot["headSha"]
            task.reviewedStatusHash = post_snapshot["statusHash"]
            task.reviewedDiffHash = post_snapshot["diffHash"]
            task.reviewedTreeSha = post_snapshot.get("treeSha")
            snapshot_persisted = True
    if not snapshot_persisted:
        task.reviewedRound = None
        task.reviewedHeadSha = None
        task.reviewedStatusHash = None
        task.reviewedDiffHash = None
        task.reviewedTreeSha = None

    diff_content = ""
    if git_artifacts.diff_path.is_file():
        diff_content = git_artifacts.diff_path.read_text(encoding="utf-8", errors="replace").strip()

    status_has_changes = bool(git_artifacts.status.strip())
    diff_stat_nonempty = bool(git_artifacts.diff_stat.strip())

    if not diff_content and not status_has_changes and not diff_stat_nonempty:
        task.progress = 100
        task.activeClient = None
        task.stage = "no_changes"
        task.lastActivityAt = utc_now_str()
        task.add_history("NO_DIFF_DETECTED", "Claude 完成后未检测到实现改动（Git diff 为空），任务标记为失败。")
        set_task_status(task, Status.FAILED, "未检测到实现改动")
        task_store.save(task)
        return task

    test_result = run_tests(project_path, task_dir, task.round, task.testCommand)
    if test_result.path:
        task.add_artifact(test_result.path.name, test_result.path.name)
    review_prompt = write_codex_review_prompt(task, task_dir)
    task.add_artifact(review_prompt.name, review_prompt.name)
    task.progress = 50
    task.stage = "waiting_for_codex"
    task.activeClient = None
    task.lastActivityAt = utc_now_str()
    set_task_status(task, Status.WAITING_FOR_CODEX, "Git and test artifacts collected; Codex prompt generated.")
    task_store.save(task)
    return task


def launch_codex_task(task_id: str, project_store: ProjectStore, task_store: TaskStore):
    """Launch the Codex CLI window for a task in ``WAITING_FOR_CODEX``.

    Codex P1-4 round 18: the full ``load → validate → launch → save``
    span runs under the per-task ``RLock`` (same as
    ``launch_claude_task``) so duplicate concurrent POSTs cannot both
    write the marker file and launch CLI windows.
    """
    with _task_operation_lock(task_id):
        return _launch_codex_task_locked(task_id, project_store, task_store)


def _launch_codex_task_locked(task_id: str, project_store: ProjectStore, task_store: TaskStore):
    task = task_store.load(task_id)
    validate_task_project(task, project_store)
    if task.status != Status.WAITING_FOR_CODEX:
        raise StateTransitionError(f"Codex can only be launched from {Status.WAITING_FOR_CODEX}.")
    task_dir = task_store.task_dir(task.id)
    prompt_path = task_dir / "CODEX_REVIEW_PROMPT.md"
    if not prompt_path.is_file():
        raise ApiError(HTTPStatus.CONFLICT, "Codex review prompt has not been generated.")
    settings = load_settings(SETTINGS_FILE)
    adapter = CodexCliWindowAdapter(settings["codexCommand"])
    output_path = task_dir / "CODEX_REVIEW.json"
    marker_path = task_dir / f"codex_output_started_round_{task.round}.txt"
    marker_path.write_text(str(time.time()), encoding="utf-8")
    task.codexWindow = adapter.launch(task, task_dir, prompt_path, output_path)
    task.add_artifact(prompt_path.name, prompt_path.name)
    task.add_artifact(Path(task.codexWindow["script"]).name, Path(task.codexWindow["script"]).name)
    task.progress = 60
    task.stage = "codex_running"
    task.activeClient = "codex"
    task.lastActivityAt = utc_now_str()
    set_task_status(task, Status.CODEX_WINDOW_STARTED, "Codex CLI window launched.")
    task_store.save(task)
    return task


def _verify_review_snapshot_at_pass(task, project_store: ProjectStore) -> str | None:
    """Return a blocking reason if the worktree has drifted since artifact time.

    The reviewed snapshot is captured at artifact-collection time
    (``complete_claude_task``).  On Codex PASS we must verify the worktree
    still matches that snapshot so post-artifact edits cannot be smuggled
    into the "reviewed" state.  Returns ``None`` when the snapshot is
    present and matches; otherwise returns a short reason string suitable
    for the task history.  Never raises — Git/path failures are mapped to
    a blocking reason so the PASS is not silently allowed.

    ``EnvFileChangedError`` is treated as a hard block: the snapshot
    helper refuses to read ``.env`` bytes/diff content, so the PASS must
    be rejected with a clear reason instead of falling back to the
    generic drift / failure path.
    """
    if (
        task.reviewedRound is None
        or not task.reviewedHeadSha
        or not task.reviewedStatusHash
        or not task.reviewedDiffHash
        or not task.reviewedTreeSha
    ):
        return (
            "No reviewed snapshot exists for this round; cannot verify the "
            "worktree matches what Codex reviewed. Re-run Claude completion "
            "to capture a fresh snapshot."
        )
    if task.reviewedRound != task.round:
        return (
            f"Reviewed snapshot is from round {task.reviewedRound} but current "
            f"round is {task.round}; the artifacts under review are stale. "
            "Re-run Claude completion to capture a fresh snapshot."
        )
    try:
        project_path = validate_task_project(task, project_store)
        current = compute_review_snapshot(project_path)
    except EnvFileChangedError as exc:
        # The snapshot helper blocks before reading any .env content; surface
        # this as a PASS-blocking reason so the user must remove the .env
        # change before re-running the review cycle.
        return (
            f"A .env file is present in the worktree at PASS time; the "
            f"review snapshot cannot be recomputed without reading .env "
            f"content. Remove the .env change before PASS. ({exc})"
        )
    except (ApiError, GitError) as exc:
        return f"Failed to recompute review snapshot at PASS time: {exc}"
    drifts: list[str] = []
    if task.reviewedHeadSha and current.get("headSha") != task.reviewedHeadSha:
        drifts.append("HEAD moved since artifact collection")
    if current.get("statusHash") != task.reviewedStatusHash:
        drifts.append("git status changed since artifact collection")
    if current.get("diffHash") != task.reviewedDiffHash:
        drifts.append("diff changed since artifact collection")
    if task.reviewedTreeSha and current.get("treeSha") != task.reviewedTreeSha:
        drifts.append("staged tree changed since artifact collection")
    if drifts:
        return (
            "Worktree drifted from the reviewed PASS snapshot: "
            + "; ".join(drifts)
            + ". Revert the drift and re-run Claude completion before PASS."
        )
    return None


def complete_codex_task(task_id: str, project_store: ProjectStore, task_store: TaskStore):
    """Process the Codex review output and advance / loop the task.

    Codex P1-4 round 18: the full ``load → validate → mutate → save``
    span runs under the per-task ``RLock`` so a concurrent
    ``cancel_task`` / ``archive_task`` cannot race the state transition
    and overwrite the resulting ``Task`` object with stale metadata.
    """
    with _task_operation_lock(task_id):
        return _complete_codex_task_locked(task_id, project_store, task_store)


def _complete_codex_task_locked(task_id: str, project_store: ProjectStore, task_store: TaskStore):
    task = task_store.load(task_id)
    validate_task_project(task, project_store)
    if task.status != Status.CODEX_WINDOW_STARTED:
        raise StateTransitionError(f"Codex can only be completed from {Status.CODEX_WINDOW_STARTED}.")
    task_dir = task_store.task_dir(task.id)
    review_path = task_dir / "CODEX_REVIEW.json"
    marker_path = task_dir / f"codex_output_started_round_{task.round}.txt"
    stale_review = (
        review_path.is_file()
        and marker_path.is_file()
        and review_path.stat().st_mtime <= marker_path.stat().st_mtime
    )
    if not review_path.is_file() or stale_review:
        log_path = task_dir / f"codex_window_round_{task.round}.log"
        detail = "Codex did not create a fresh CODEX_REVIEW.json yet."
        if log_path.is_file():
            detail = f"{detail} Check {log_path.name} for the launcher output."
            task.add_artifact(log_path.name, log_path.name)
        task.add_history("CODEX_REVIEW_MISSING", detail)
        task.progress = 50
        task.stage = "waiting_for_codex"
        task.activeClient = None
        task.lastActivityAt = utc_now_str()
        set_task_status(task, Status.WAITING_FOR_CODEX, detail)
        task_store.save(task)
        return task
    try:
        review = load_review_report(review_path)
    except ReportValidationError as exc:
        task.add_history("CODEX_REVIEW_INVALID", str(exc))
        task.progress = 100
        task.activeClient = None
        task.stage = "review_invalid"
        task.lastActivityAt = utc_now_str()
        set_task_status(task, Status.FAILED, f"Codex review validation failed: {exc}")
        task_store.save(task)
        return task

    task.add_artifact(review_path.name, review_path.name)
    review_status = str(review["status"])
    if review_status in {Status.PASS, Status.BLOCKED, Status.FAILED}:
        # On PASS, do NOT overwrite the reviewed snapshot: it was captured
        # at artifact-collection time so it mirrors exactly what Codex
        # reviewed.  Verify the worktree still matches that snapshot BEFORE
        # transitioning out of CODEX_WINDOW_STARTED (the state machine
        # forbids transitions out of terminal statuses like PASS).  If it
        # has drifted since the artifacts were collected, the Codex PASS
        # is invalid for the current worktree state, so block the PASS and
        # require a fresh review cycle.  Missing / unreadable snapshots
        # are also blocked because they cannot be trusted.
        if review_status == Status.PASS:
            block_reason = _verify_review_snapshot_at_pass(task, project_store)
            if block_reason:
                task.progress = 100
                task.activeClient = None
                task.stage = "review_drift_blocked"
                task.lastActivityAt = utc_now_str()
                task.add_history("REVIEW_DRIFT_BLOCKED", block_reason)
                set_task_status(
                    task,
                    Status.FAILED,
                    f"PASS blocked: worktree drifted from reviewed snapshot. {block_reason}",
                )
                task_store.save(task)
                return task
        task.progress = 100
        task.activeClient = None
        task.stage = "review_complete"
        task.lastActivityAt = utc_now_str()
        set_task_status(task, review_status, f"Codex review completed with {review_status}.")
        task_store.save(task)
        return task

    set_task_status(task, Status.NEEDS_FIX, "Codex review requested fixes.")
    if task.round >= task.maxRounds:
        task.progress = 100
        task.activeClient = None
        task.stage = "max_rounds_exhausted"
        task.lastActivityAt = utc_now_str()
        set_task_status(task, Status.FAILED, "Maximum rounds exhausted after NEEDS_FIX.")
        task_store.save(task)
        return task

    next_round = task.round + 1
    write_fix_prompt(task, task_dir, review, next_round)
    task.round = next_round
    task.progress = 20
    task.stage = f"fix_round_{next_round}"
    task.activeClient = None
    task.lastActivityAt = utc_now_str()
    set_task_status(task, Status.WAITING_FOR_CLAUDE, f"Fix prompt generated for round {next_round}.")
    task_store.save(task)
    return task


def cancel_task(task_id: str, task_store: TaskStore):
    """Cancel a task and mark it as stopped.

    Codex P1-2 round 17: the full ``load → mutate → save`` span runs
    under the per-task ``RLock`` (same as ``commit_task_changes`` /
    ``merge_task_to_main``).  Without serialisation, duplicate
    concurrent POSTs to ``/api/tasks/{id}/cancel`` can both load the
    task in its pre-cancel state, both run the state transition, and
    the loser of the race then saves its stale ``Task`` object —
    overwriting any concurrent ``COMMITTED`` / ``MERGED`` metadata
    recorded by a request that did not acquire the lock.
    """
    with _task_operation_lock(task_id):
        return _cancel_task_locked(task_id, task_store)


def _cancel_task_locked(task_id: str, task_store: TaskStore):
    task = task_store.load(task_id)
    task.progress = 100 if task.progress == 0 else task.progress
    task.activeClient = None
    task.stage = "cancelled"
    task.lastActivityAt = utc_now_str()
    task.set_status(cancel_status(task.status), "Task cancelled by user.")
    task_store.save(task)
    return task


VALID_TERMINAL_CLIENTS = {"claude", "codex"}
RUNNING_TASK_STATUSES = {Status.CLAUDE_WINDOW_STARTED, Status.CODEX_WINDOW_STARTED}


def _resolve_terminal_log(task_store, task, client: str, fallback: bool = False) -> Path:
    """Construct and validate the terminal log path for a task and client.

    When fallback is True and the current round's log does not exist, try
    earlier rounds so the web UI can still show output from a completed
    round (e.g. after Codex returns NEEDS_FIX and the round is advanced).
    """
    if client not in VALID_TERMINAL_CLIENTS:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"Invalid terminal client: {client}")
    task_dir = task_store.task_dir(task.id)
    log_path = task_dir / f"{client}_window_round_{task.round}.log"
    if fallback and not log_path.is_file():
        for r in range(task.round - 1, 0, -1):
            candidate = task_dir / f"{client}_window_round_{r}.log"
            if candidate.is_file():
                log_path = candidate
                break
    try:
        from gui.orchestrator.path_safety import ensure_child_path
        return ensure_child_path(task_dir, log_path)
    except Exception as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"Unsafe log path: {exc}")


def _terminal_metadata(task_store, task, client: str) -> dict[str, Any]:
    # Only use fallback when round was bumped (WAITING_FOR_CLAUDE after NEEDS_FIX)
    # so the web UI can still show the previous round's output. For states like
    # WAITING_FOR_CODEX where the current client hasn't started, return the
    # current-round metadata with exists=false instead of stale earlier-round logs.
    should_fallback = task.status == Status.WAITING_FOR_CLAUDE
    log_path = _resolve_terminal_log(task_store, task, client, fallback=should_fallback)
    exists = log_path.is_file()
    active = (task.activeClient == client) and (task.status in RUNNING_TASK_STATUSES)
    mtime = log_path.stat().st_mtime if exists else None

    exit_code = None
    finished = False
    if exists:
        content = log_path.read_text(encoding="utf-8", errors="replace")
        for line in reversed(content.splitlines()):
            m = re.match(r"^CLI exit code:\s*(-?\d+)\s*$", line)
            if not m and not active:
                m = re.search(r"CLI exit code:\s*(-?\d+)\s*$", line)
            if m:
                exit_code = int(m.group(1))
                finished = True
                break

    return {
        "taskId": task.id,
        "client": client,
        "round": task.round,
        "logName": log_path.name,
        "exists": exists,
        "size": log_path.stat().st_size if exists else 0,
        "status": task.status,
        "active": active,
        "updatedAt": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") if mtime else None,
        "finished": finished,
        "exitCode": exit_code,
        "lastLogUpdateAt": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") if mtime else None,
    }


def _terminal_stream(task_store, task_id: str, client: str, wfile, flush):
    """SSE stream generator for terminal log output. Reloads task only for completion checks."""
    sent_bytes = 0
    waiting_sent = False
    task = task_store.load(task_id)
    log_path = _resolve_terminal_log(task_store, task, client)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    line_buffer = ""  # rolling buffer for incomplete last line across reads
    while True:
        current_size = log_path.stat().st_size if log_path.is_file() else 0
        if current_size > sent_bytes:
            with log_path.open("rb") as handle:
                handle.seek(sent_bytes)
                raw = handle.read(current_size - sent_bytes)
            chunk = decoder.decode(raw, final=False)
            sent_bytes = current_size
            if chunk:
                payload = json.dumps({"chunk": chunk, "offset": sent_bytes, "done": False}, ensure_ascii=False)
                wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                flush()
                # Check complete lines for the CLI exit sentinel
                combined = line_buffer + chunk
                lines = combined.split("\n")
                line_buffer = lines.pop()  # keep incomplete last line for next iteration
                exit_code_detected = None
                for line in lines:
                    m = re.match(r"^CLI exit code:\s*(-?\d+)\s*$", line)
                    if m:
                        exit_code_detected = int(m.group(1))
                        break
                if exit_code_detected is not None:
                    # Flush any bytes remaining in the decoder before closing
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        payload = json.dumps({"chunk": tail, "offset": sent_bytes, "done": False}, ensure_ascii=False)
                        wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                        flush()
                    payload = json.dumps({"chunk": "", "offset": sent_bytes, "done": True, "exitCode": exit_code_detected}, ensure_ascii=False)
                    wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    flush()
                    return
        elif not waiting_sent:
            waiting_sent = True
            payload = json.dumps({"chunk": "", "offset": 0, "done": False, "waiting": True}, ensure_ascii=False)
            wfile.write(f"data: {payload}\n\n".encode("utf-8"))
            flush()
        task = task_store.load(task_id)
        if task.status not in RUNNING_TASK_STATUSES:
            tail = decoder.decode(b"", final=True)
            if tail:
                payload = json.dumps({"chunk": tail, "offset": sent_bytes, "done": False}, ensure_ascii=False)
                wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                flush()
            done_payload = json.dumps({"chunk": "", "offset": sent_bytes, "done": True}, ensure_ascii=False)
            wfile.write(f"data: {done_payload}\n\n".encode("utf-8"))
            flush()
            return
        time.sleep(0.5)


def ensure_task_not_running(task) -> None:
    if task.status in RUNNING_TASK_STATUSES:
        raise ApiError(HTTPStatus.CONFLICT, "Running tasks cannot be archived or deleted.")


def archive_task(task_id: str, task_store: TaskStore):
    """Archive a task, moving it out of the active list without deleting it.

    Codex P1-2 round 17: serialised via the per-task ``RLock`` so a
    concurrent commit / merge / cancel / restore cannot race the archive
    and overwrite the resulting ``Task`` object with stale metadata.
    """
    with _task_operation_lock(task_id):
        return _archive_task_locked(task_id, task_store)


def _archive_task_locked(task_id: str, task_store: TaskStore):
    task = task_store.load(task_id)
    ensure_task_not_running(task)
    task = task_store.archive(task_id)
    write_audit_log("task.archive", task.id, {"projectId": task.projectId, "status": task.status})
    return task


def restore_archived_task(task_id: str, task_store: TaskStore):
    """Restore an archived task back to the active list.

    Codex P1-2 round 17: serialised via the per-task ``RLock`` so a
    concurrent archive / cancel / move_to_trash cannot race the restore
    and overwrite the resulting ``Task`` object with stale metadata.
    """
    with _task_operation_lock(task_id):
        return _restore_archived_task_locked(task_id, task_store)


def _restore_archived_task_locked(task_id: str, task_store: TaskStore):
    task = task_store.restore_archived(task_id)
    write_audit_log("task.restore_archive", task.id, {"projectId": task.projectId, "status": task.status})
    return task


def move_task_to_trash(task_id: str, task_store: TaskStore):
    """Move a task to the trash directory (soft delete, restorable).

    Codex P1-2 round 17: serialised via the per-task ``RLock`` so a
    concurrent commit / merge / cancel / archive cannot race the trash
    and overwrite the resulting ``Task`` object with stale metadata.
    """
    with _task_operation_lock(task_id):
        return _move_task_to_trash_locked(task_id, task_store)


def _move_task_to_trash_locked(task_id: str, task_store: TaskStore):
    task = task_store.load(task_id)
    ensure_task_not_running(task)
    task = task_store.move_to_trash(task_id)
    write_audit_log(
        "task.move_to_trash",
        task.id,
        {"projectId": task.projectId, "status": task.status, "trashPath": task.trashPath},
    )
    return task


def restore_task_from_trash(task_id: str, task_store: TaskStore):
    """Restore a trashed task back to the active list.

    Codex P1-2 round 17: serialised via the per-task ``RLock`` so a
    concurrent trash / cancel cannot race the restore and overwrite the
    resulting ``Task`` object with stale metadata.
    """
    with _task_operation_lock(task_id):
        return _restore_task_from_trash_locked(task_id, task_store)


def _restore_task_from_trash_locked(task_id: str, task_store: TaskStore):
    task = task_store.restore_from_trash(task_id)
    write_audit_log("task.restore_trash", task.id, {"projectId": task.projectId, "status": task.status})
    return task


def project_has_running_tasks(project_id_value: str, task_store: TaskStore) -> bool:
    for task in task_store.list_tasks():
        if task.projectId == project_id_value and task.status in RUNNING_TASK_STATUSES:
            return True
    for task in task_store.list_tasks(archived=True):
        if task.projectId == project_id_value and task.status in RUNNING_TASK_STATUSES:
            return True
    return False


def remove_project(project_id_value: str, project_store: ProjectStore, task_store: TaskStore, runs: RunManager):
    project = project_store.get_project(project_id_value)
    if runs.is_project_running(project_id_value) or project_has_running_tasks(project_id_value, task_store):
        raise ApiError(HTTPStatus.CONFLICT, "Project has running tasks and cannot be removed.")
    worktree_type = project.get("worktreeType")
    removed = project_store.remove_project(project_id_value)
    audit_details = {
        "name": removed.get("name"),
        "path": removed.get("path"),
        "localFilesDeleted": False,
        "worktreeType": worktree_type,
    }
    if worktree_type:
        audit_details["note"] = "Worktree record removed; local directory and Git branch were NOT deleted."
    write_audit_log("project.remove", project_id_value, audit_details)
    return project


class GuiHandler(BaseHTTPRequestHandler):
    store = ProjectStore()
    runs = RunManager(store)
    tasks = TaskStore(TASKS_DIR, TRASH_TASKS_DIR)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path == "/api/projects":
                self.send_json({"projects": self.store.list_projects()})
            elif path == "/api/tasks":
                archived = query.get("archived", ["0"])[0] in {"1", "true", "yes"}
                project = query.get("project", [None])[0] or None
                self.send_json({"tasks": [task.to_dict() for task in self.tasks.list_tasks(archived=archived, project_id=project)]})
            elif path == "/api/trash/tasks":
                self.send_json({"tasks": [task.to_dict() for task in self.tasks.list_trash_tasks()]})
            elif match := re.fullmatch(r"/api/tasks/([^/]+)", path):
                task = self.tasks.load(match.group(1))
                self.send_json({"task": task.to_dict()})
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/artifacts", path):
                self.send_json({"artifacts": self.tasks.read_artifacts(match.group(1))})
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/terminal/([^/]+)", path):
                task = self.tasks.load(match.group(1))
                self.send_json(_terminal_metadata(self.tasks, task, match.group(2)))
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/terminal/([^/]+)/stream", path):
                self.handle_terminal_stream(match.group(1), match.group(2))
            elif path == "/api/runs/current":
                self.send_json({"run": self.runs.snapshot()})
            elif path == "/api/runs/current/stream":
                self.handle_stream()
            elif match := re.fullmatch(r"/api/projects/([^/]+)/plan", path):
                project = self.store.get_project(match.group(1))
                self.send_json(read_plan(project))
            elif match := re.fullmatch(r"/api/projects/([^/]+)/artifacts", path):
                project = self.store.get_project(match.group(1))
                self.send_json({"artifacts": read_artifacts(project)})
            else:
                self.serve_static(path)
        except ApiError as exc:
            self.send_error_json(exc.status, exc.message)
        except TaskStoreError as exc:
            self.send_error_json(HTTPStatus.NOT_FOUND, str(exc))
        except (StateTransitionError, GitError, ReportValidationError) as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            body = self.read_json_body()
            if path == "/api/projects":
                project_path = normalize_path(str(body.get("path", "")))
                project = self.store.add_project(project_path, body.get("name"))
                write_audit_log("project.add", project["id"], {"name": project.get("name"), "path": project.get("path")})
                self.send_json({"project": project}, HTTPStatus.CREATED)
            elif match := re.fullmatch(r"/api/projects/([^/]+)/initialize", path):
                project = self.store.get_project(match.group(1))
                self.send_json({"project": initialize_project(project, self.store)})
            elif match := re.fullmatch(r"/api/projects/([^/]+)/worktrees", path):
                result = create_project_worktree(match.group(1), body, self.store)
                self.send_json(result, HTTPStatus.CREATED)
            elif path == "/api/tasks":
                task = create_task(body, self.store, self.tasks)
                self.send_json({"task": task.to_dict()}, HTTPStatus.CREATED)
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/launch-claude", path):
                task = launch_claude_task(match.group(1), self.store, self.tasks)
                self.send_json({"task": task.to_dict()})
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/claude-completed", path):
                task = complete_claude_task(match.group(1), self.store, self.tasks)
                self.send_json({"task": task.to_dict()})
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/launch-codex", path):
                task = launch_codex_task(match.group(1), self.store, self.tasks)
                self.send_json({"task": task.to_dict()})
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/codex-completed", path):
                task = complete_codex_task(match.group(1), self.store, self.tasks)
                self.send_json({"task": task.to_dict()})
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/commit", path):
                task = commit_task_changes(match.group(1), body, self.store, self.tasks)
                self.send_json({"task": task.to_dict()})
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/merge", path):
                task = merge_task_to_main(match.group(1), self.store, self.tasks)
                self.send_json({"task": task.to_dict()})
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/cancel", path):
                task = cancel_task(match.group(1), self.tasks)
                self.send_json({"task": task.to_dict()})
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/archive", path):
                task = archive_task(match.group(1), self.tasks)
                self.send_json({"task": task.to_dict()})
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/restore", path):
                task = restore_archived_task(match.group(1), self.tasks)
                self.send_json({"task": task.to_dict()})
            elif match := re.fullmatch(r"/api/trash/tasks/([^/]+)/restore", path):
                task = restore_task_from_trash(match.group(1), self.tasks)
                self.send_json({"task": task.to_dict()})
            elif path == "/api/runs":
                project = self.store.get_project(str(body.get("projectId", "")))
                run = self.runs.start(project, body.get("options") or {})
                self.send_json({"run": run}, HTTPStatus.CREATED)
            elif path == "/api/runs/current/stop":
                # P2-1: route through ``shutdown_and_snapshot`` so the
                # Job Object is closed even when the direct wrapper has
                # already exited but descendants (Claude / Codex CLI)
                # are still running inside the Job. The previous call to
                # ``stop()`` raised 409 CONFLICT in that case, leaving
                # the run visible as "running" while descendants kept
                # modifying the project until the desktop app exited.
                self.send_json({"run": self.runs.shutdown_and_snapshot()})
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "Endpoint not found.")
        except ApiError as exc:
            self.send_error_json(exc.status, exc.message)
        except DirtyWorkTreeError as exc:
            self.send_error_json(HTTPStatus.CONFLICT, str(exc))
        except WorktreeCreationError as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except (CommitError, MergeError) as exc:
            self.send_error_json(HTTPStatus.CONFLICT, str(exc))
        except TaskStoreError as exc:
            self.send_error_json(HTTPStatus.NOT_FOUND, str(exc))
        except (StateTransitionError, GitError, ReportValidationError) as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_DELETE(self) -> None:
        try:
            path = urlparse(self.path).path
            if match := re.fullmatch(r"/api/tasks/([^/]+)", path):
                task = move_task_to_trash(match.group(1), self.tasks)
                self.send_json({"task": task.to_dict()})
            elif match := re.fullmatch(r"/api/projects/([^/]+)", path):
                project = remove_project(match.group(1), self.store, self.tasks, self.runs)
                self.send_json({"project": project, "localFilesDeleted": False})
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "Endpoint not found.")
        except ApiError as exc:
            self.send_error_json(exc.status, exc.message)
        except TaskStoreError as exc:
            self.send_error_json(HTTPStatus.NOT_FOUND, str(exc))
        except Exception as exc:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_PUT(self) -> None:
        try:
            path = urlparse(self.path).path
            body = self.read_json_body()
            if match := re.fullmatch(r"/api/projects/([^/]+)/plan", path):
                project = self.store.get_project(match.group(1))
                self.send_json(write_plan(project, str(body.get("content", ""))))
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "Endpoint not found.")
        except ApiError as exc:
            self.send_error_json(exc.status, exc.message)
        except Exception as exc:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid JSON body.") from exc
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "JSON body must be an object.")
        return data

    def handle_stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            for event in self.runs.stream_events():
                payload = json.dumps(event, ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def handle_terminal_stream(self, task_id: str, client: str) -> None:
        try:
            task = self.tasks.load(task_id)
        except TaskStoreError as exc:
            self.send_error_json(HTTPStatus.NOT_FOUND, str(exc))
            return
        try:
            _resolve_terminal_log(self.tasks, task, client)
        except ApiError as exc:
            self.send_error_json(exc.status, exc.message)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            _terminal_stream(self.tasks, task_id, client, self.wfile, self.wfile.flush)
        except (BrokenPipeError, ConnectionResetError):
            return

    def serve_static(self, request_path: str) -> None:
        relative = unquote(request_path.lstrip("/")) or "index.html"
        if relative.startswith("api/"):
            self.send_error_json(HTTPStatus.NOT_FOUND, "Endpoint not found.")
            return
        file_path = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in file_path.parents and file_path != STATIC_DIR.resolve():
            self.send_error_json(HTTPStatus.FORBIDDEN, "Forbidden.")
            return
        if file_path.is_dir():
            file_path = file_path / "index.html"
        if not file_path.exists():
            file_path = STATIC_DIR / "index.html"
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(file_path.stat().st_size))
        self.end_headers()
        self.wfile.write(file_path.read_bytes())

    def send_json(self, data: Any, status: int = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json({"error": message}, status)


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), GuiHandler)


def configure_paths(state_dir: Path | str | None = None) -> Path:
    """Override the application state directory and rebind store singletons.

    Used by the desktop app to redirect task/project/settings writes to
    ``%LOCALAPPDATA%\\CodexClaudeDevLoop\\.gui`` without breaking
    source-mode invocations that continue to use the in-repo ``.gui``.

    Updates the module-level constants (``STATE_DIR`` and friends) **and**
    the ``GuiHandler`` class-level singletons so the request handler and
    any caller importing ``PROJECTS_FILE`` / ``TASKS_DIR`` / etc. see the
    same target directory. Returns the resolved state directory.
    """
    global STATE_DIR, PROJECTS_FILE, TASKS_DIR, TRASH_TASKS_DIR
    global SETTINGS_FILE, AUDIT_LOG_FILE, MERGE_RECOVERY_DIR
    if state_dir is None or str(state_dir) == "":
        state_dir = (
            Path(os.environ["CCDL_STATE_DIR"]).expanduser()
            if os.environ.get("CCDL_STATE_DIR")
            else ROOT / ".gui"
        )
    STATE_DIR = Path(state_dir).expanduser().resolve()
    PROJECTS_FILE = STATE_DIR / "projects.json"
    TASKS_DIR = STATE_DIR / "tasks"
    TRASH_TASKS_DIR = STATE_DIR / "trash" / "tasks"
    SETTINGS_FILE = STATE_DIR / "settings.json"
    AUDIT_LOG_FILE = STATE_DIR / "audit.log"
    MERGE_RECOVERY_DIR = STATE_DIR / "merge_recovery"
    GuiHandler.store = ProjectStore(PROJECTS_FILE)
    GuiHandler.runs = RunManager(GuiHandler.store)
    GuiHandler.tasks = TaskStore(TASKS_DIR, TRASH_TASKS_DIR)
    return STATE_DIR


def find_available_port(host: str = "127.0.0.1", preferred: int = 8765, attempts: int = 64) -> int:
    """Return a TCP port on ``host`` that is currently free.

    Tries ``preferred`` first, then increments. If none of the candidates
    are bindable the OS is asked to pick an ephemeral port (``port=0``)
    so the desktop app always has a workable address.
    """
    import socket as _socket

    for offset in range(attempts):
        candidate = preferred + offset
        if candidate < 1024 or candidate > 65535:
            continue
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        try:
            sock.bind((host, candidate))
        except OSError:
            continue
        else:
            sock.close()
            return candidate
        finally:
            try:
                sock.close()
            except OSError:
                pass
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        sock.bind((host, 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def create_server_on_free_port(
    host: str = "127.0.0.1",
    preferred_port: int = 8765,
) -> tuple[ThreadingHTTPServer, int]:
    """Bind ``ThreadingHTTPServer`` to an actually-free port.

    Unlike :func:`find_available_port` + :func:`create_server` this keeps
    the listener socket open between probe and serve, eliminating the
    TOCTOU window where another process could grab the port between the
    probe close and the server bind.
    """
    import socket as _socket

    last_error: Exception | None = None
    for offset in range(64):
        candidate = preferred_port + offset
        if candidate < 1024 or candidate > 65535:
            continue
        try:
            return ThreadingHTTPServer((host, candidate), GuiHandler), candidate
        except OSError as exc:
            last_error = exc
            continue
    # Last resort: let the OS pick.
    listener = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        listener.bind((host, 0))
        port = listener.getsockname()[1]
    finally:
        listener.close()
    try:
        return ThreadingHTTPServer((host, port), GuiHandler), port
    except OSError as exc:
        raise RuntimeError(f"Unable to bind GUI HTTP server: {exc}") from exc


def _recover_pending_merges_at_startup(
    task_store: TaskStore | None = None,
) -> None:
    """Best-effort recovery sweep for any pending merge journals.

    Codex P1-1 round 19.  Runs once at server startup so a crash
    between forward CAS and materialisation in a previous session is
    recovered before any new controlled merge attempt.  Each journal's
    primary path may differ; the recovery itself is per-primary and
    each journal is recovered independently via
    ``recover_pending_merge``.  Outcomes are recorded as audit events.
    """
    if task_store is None:
        task_store = GuiHandler.tasks
    recovery_dir = _merge_recovery_dir(task_store)
    if not recovery_dir.is_dir():
        return
    try:
        candidates = sorted(recovery_dir.glob("*.json"))
    except OSError:
        return
    for journal_file in candidates:
        operation_id = journal_file.stem
        try:
            journal = MergeRecoveryJournal(recovery_dir, operation_id)
        except ValueError:
            continue
        _recover_one_merge_journal(journal, task_store)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local web GUI for the Codex/Claude orchestrator.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--state-dir",
        default=None,
        help=(
            "Override the application state directory (.gui). "
            "Use this to redirect task/project/settings/logs to a writable "
            "user data folder when running outside the source tree "
            "(for example the packaged desktop app)."
        ),
    )
    args = parser.parse_args()

    if args.state_dir:
        configure_paths(args.state_dir)
    os.chdir(ROOT)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Codex P1-1 round 19: recover any pending merge journals before
    # the server starts serving requests so a crash between forward CAS
    # and materialisation in a previous session is handled before a
    # new merge can race it.
    _recover_pending_merges_at_startup()
    server = create_server(args.host, args.port)
    print(f"GUI running at http://{args.host}:{args.port}")
    if STATE_DIR != (ROOT / ".gui"):
        print(f"State directory: {STATE_DIR}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping GUI server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
