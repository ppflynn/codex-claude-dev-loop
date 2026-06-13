from __future__ import annotations

import argparse
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
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
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
from gui.orchestrator.store import TaskStore, TaskStoreError
from gui.orchestrator.test_runner import run_tests


STATIC_DIR = ROOT / "gui" / "static"
STATE_DIR = ROOT / ".gui"
PROJECTS_FILE = STATE_DIR / "projects.json"
TASKS_DIR = STATE_DIR / "tasks"
TRASH_TASKS_DIR = STATE_DIR / "trash" / "tasks"
SETTINGS_FILE = STATE_DIR / "settings.json"
AUDIT_LOG_FILE = STATE_DIR / "audit.log"

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
    with AUDIT_LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


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


def make_project(path: Path, name: str | None = None) -> dict[str, Any]:
    kind = detect_project_kind(path)
    return {
        "id": project_id(path),
        "name": name.strip() if name and name.strip() else path.name,
        "path": str(path),
        "kind": kind,
        "lastResult": None,
        "lastExitCode": None,
        "lastRunAt": None,
    }


def clamp_max_rounds(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 3
    return max(1, min(10, parsed))


def build_run_command(project_path: Path, options: dict[str, Any]) -> list[str]:
    script_path = project_path / "scripts" / "run-claude.ps1"
    command = [
        "powershell",
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
            return projects

    def save_projects(self, projects: list[dict[str, Any]]) -> None:
        with self._lock:
            write_json_file(self.path, {"projects": projects})

    def add_project(self, path: Path, name: str | None = None) -> dict[str, Any]:
        project = make_project(path, name)
        projects = self.list_projects()
        existing = next((p for p in projects if p.get("id") == project["id"]), None)
        if existing:
            existing.update(
                {
                    "name": project["name"],
                    "path": project["path"],
                    "kind": project["kind"],
                }
            )
            self.save_projects(projects)
            return existing
        projects.append(project)
        self.save_projects(projects)
        return project

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


class RunManager:
    def __init__(self, store: ProjectStore):
        self.store = store
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self.current: dict[str, Any] | None = None
        self._process: subprocess.Popen[str] | None = None
        self._stopping = False

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

            try:
                self._process = subprocess.Popen(
                    command,
                    cwd=str(project_path),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            except OSError as exc:
                self.current["status"] = "failed"
                self.current["result"] = "START_FAILED"
                self.current["logs"].append(f"Failed to start process: {exc}")
                self._condition.notify_all()
                raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, f"Failed to start process: {exc}") from exc

            self._append_locked(f"Started: {' '.join(command)}")
            threading.Thread(target=self._read_process, daemon=True).start()
            return self.snapshot() or {}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self._process or self._process.poll() is not None:
                raise ApiError(HTTPStatus.CONFLICT, "No active run to stop.")
            self._stopping = True
            self._append_locked("Stop requested.")
            process = self._process

        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            with self._lock:
                self._append_locked("Process did not stop in time; killed.")
        return self.snapshot() or {}

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

    def _read_process(self) -> None:
        process = self._process
        if process and process.stdout:
            try:
                for line in process.stdout:
                    self._append(line)
            finally:
                process.stdout.close()
        exit_code = process.wait() if process else None
        with self._lock:
            stopped = self._stopping
            result = exit_code_to_result(exit_code, stopped=stopped)
            if self.current:
                self.current["status"] = "stopped" if stopped else "finished"
                self.current["endedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self.current["exitCode"] = exit_code
                self.current["result"] = result
                self.current["logs"].append(f"Finished with exit code {exit_code}: {result}")
                self.store.update_project(
                    self.current["projectId"],
                    {
                        "lastResult": result,
                        "lastExitCode": exit_code,
                        "lastRunAt": self.current["endedAt"],
                    },
                )
            self._condition.notify_all()


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
    path = Path(project["path"]) / "docs" / "PLAN.md"
    if not path.exists():
        return {"exists": False, "content": ""}
    return {"exists": True, "content": path.read_text(encoding="utf-8", errors="replace")}


def write_plan(project: dict[str, Any], content: str) -> dict[str, Any]:
    path = Path(project["path"]) / "docs" / "PLAN.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"exists": True, "content": content}


def read_artifacts(project: dict[str, Any]) -> dict[str, Any]:
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
    project_path = resolve_whitelisted_project(project)
    task_path = Path(task.projectPath).expanduser().resolve(strict=True)
    if task_path != project_path:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Task project path no longer matches the project whitelist.")
    return project_path


def create_task(body: dict[str, Any], project_store: ProjectStore, task_store: TaskStore):
    project = project_store.get_project(str(body.get("projectId", "")))
    project_path = resolve_whitelisted_project(project)
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
    task_dir = task_store.task_dir(task.id)
    write_claude_implementation_prompt(task, task_dir)
    set_task_status(task, Status.WAITING_FOR_CLAUDE, "Initial Claude prompt generated.")
    task_store.save(task)
    return task


def launch_claude_task(task_id: str, project_store: ProjectStore, task_store: TaskStore):
    task = task_store.load(task_id)
    validate_task_project(task, project_store)
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
    set_task_status(task, Status.CLAUDE_WINDOW_STARTED, "Claude CLI window launched.")
    task_store.save(task)
    return task


def complete_claude_task(task_id: str, project_store: ProjectStore, task_store: TaskStore):
    task = task_store.load(task_id)
    project_path = validate_task_project(task, project_store)
    if task.status != Status.CLAUDE_WINDOW_STARTED:
        raise StateTransitionError(f"Claude can only be completed from {Status.CLAUDE_WINDOW_STARTED}.")
    task_dir = task_store.task_dir(task.id)
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
        set_task_status(task, Status.FAILED, str(exc))
        task_store.save(task)
        return task
    except GitError as exc:
        set_task_status(task, Status.FAILED, f"Git artifact collection failed: {exc}")
        task_store.save(task)
        return task

    test_result = run_tests(project_path, task_dir, task.round, task.testCommand)
    if test_result.path:
        task.add_artifact(test_result.path.name, test_result.path.name)
    review_prompt = write_codex_review_prompt(task, task_dir)
    task.add_artifact(review_prompt.name, review_prompt.name)
    set_task_status(task, Status.WAITING_FOR_CODEX, "Git and test artifacts collected; Codex prompt generated.")
    task_store.save(task)
    return task


def launch_codex_task(task_id: str, project_store: ProjectStore, task_store: TaskStore):
    task = task_store.load(task_id)
    validate_task_project(task, project_store)
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
    set_task_status(task, Status.CODEX_WINDOW_STARTED, "Codex CLI window launched.")
    task_store.save(task)
    return task


def complete_codex_task(task_id: str, project_store: ProjectStore, task_store: TaskStore):
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
        set_task_status(task, Status.WAITING_FOR_CODEX, detail)
        task_store.save(task)
        return task
    try:
        review = load_review_report(review_path)
    except ReportValidationError as exc:
        task.add_history("CODEX_REVIEW_INVALID", str(exc))
        set_task_status(task, Status.FAILED, f"Codex review validation failed: {exc}")
        task_store.save(task)
        return task

    task.add_artifact(review_path.name, review_path.name)
    review_status = str(review["status"])
    if review_status in {Status.PASS, Status.BLOCKED, Status.FAILED}:
        set_task_status(task, review_status, f"Codex review completed with {review_status}.")
        task_store.save(task)
        return task

    set_task_status(task, Status.NEEDS_FIX, "Codex review requested fixes.")
    if task.round >= task.maxRounds:
        set_task_status(task, Status.FAILED, "Maximum rounds exhausted after NEEDS_FIX.")
        task_store.save(task)
        return task

    next_round = task.round + 1
    write_fix_prompt(task, task_dir, review, next_round)
    task.round = next_round
    set_task_status(task, Status.WAITING_FOR_CLAUDE, f"Fix prompt generated for round {next_round}.")
    task_store.save(task)
    return task


def cancel_task(task_id: str, task_store: TaskStore):
    task = task_store.load(task_id)
    task.set_status(cancel_status(task.status), "Task cancelled by user.")
    task_store.save(task)
    return task


RUNNING_TASK_STATUSES = {Status.CLAUDE_WINDOW_STARTED, Status.CODEX_WINDOW_STARTED}


def ensure_task_not_running(task) -> None:
    if task.status in RUNNING_TASK_STATUSES:
        raise ApiError(HTTPStatus.CONFLICT, "Running tasks cannot be archived or deleted.")


def archive_task(task_id: str, task_store: TaskStore):
    task = task_store.load(task_id)
    ensure_task_not_running(task)
    task = task_store.archive(task_id)
    write_audit_log("task.archive", task.id, {"projectId": task.projectId, "status": task.status})
    return task


def restore_archived_task(task_id: str, task_store: TaskStore):
    task = task_store.restore_archived(task_id)
    write_audit_log("task.restore_archive", task.id, {"projectId": task.projectId, "status": task.status})
    return task


def move_task_to_trash(task_id: str, task_store: TaskStore):
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
    removed = project_store.remove_project(project_id_value)
    write_audit_log(
        "project.remove",
        project_id_value,
        {"name": removed.get("name"), "path": removed.get("path"), "localFilesDeleted": False},
    )
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
                self.send_json({"tasks": [task.to_dict() for task in self.tasks.list_tasks(archived=archived)]})
            elif path == "/api/trash/tasks":
                self.send_json({"tasks": [task.to_dict() for task in self.tasks.list_trash_tasks()]})
            elif match := re.fullmatch(r"/api/tasks/([^/]+)", path):
                task = self.tasks.load(match.group(1))
                self.send_json({"task": task.to_dict()})
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/artifacts", path):
                self.send_json({"artifacts": self.tasks.read_artifacts(match.group(1))})
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
                self.send_json({"run": self.runs.stop()})
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "Endpoint not found.")
        except ApiError as exc:
            self.send_error_json(exc.status, exc.message)
        except DirtyWorkTreeError as exc:
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Local web GUI for the Codex/Claude orchestrator.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    os.chdir(ROOT)
    STATE_DIR.mkdir(exist_ok=True)
    server = create_server(args.host, args.port)
    print(f"GUI running at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping GUI server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
