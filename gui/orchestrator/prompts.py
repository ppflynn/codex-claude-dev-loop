from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Task


REVIEW_SCHEMA_EXAMPLE = {
    "status": "NEEDS_FIX",
    "reviewed_at": "2026-06-11T00:00:00Z",
    "summary": "Short review summary",
    "findings": [
        {
            "id": "P1-1",
            "severity": "P1",
            "file": "relative/path.py",
            "line": 42,
            "description": "Concrete issue",
            "fix_suggestion": "Concrete fix",
        }
    ],
}


SAFETY_RULES = """## Safety Rules
- Do not read, write, print, summarize, or diff `.env`, `.env.*`, or any file under a path segment named `.env`.
- Do not modify `.git`.
- Do not run `git commit`, `git push`, `git reset`, `git clean`, `git checkout`, `git switch`, or `git restore`.
- Do not use destructive cleanup commands.
- Keep all target-project edits inside the target project root.
"""


RUNTIME_PROGRESS_PROTOCOL = """## Runtime Terminal Progress Protocol
The runtime terminal parses your streaming output for single-line status events and renders them as user-readable progress cues (for example `[修改中] 正在更新 prompt 生成逻辑`).

Emit each event on its own line in this exact shape:

`::task-status{phase="<phase>" message="<message>"}`

Allowed phases (use the exact token, lowercase):
- `planning` — analyzing the task or designing an approach.
- `reading` — about to read a file, diff, test log, or other artifact.
- `running` — about to execute a shell command.
- `editing` — about to modify a file.
- `testing` — about to run the test command or verify behavior.
- `reviewing` — assessing correctness, regressions, or quality.
- `writing` — about to write a report, prompt, or other document.
- `waiting` — paused for an external condition.
- `blocked` — cannot proceed without intervention.
- `done` — finished the requested work.

Protocol rules:
- Each event MUST occupy its own line. Do not embed `::task-status{...}` inside code blocks, JSON, Markdown fences, or surrounded by other prose on the same line.
- The `::task-status{phase=...` prefix and closing `}` MUST use plain ASCII. The `message` value MAY contain non-ASCII characters (for example Chinese).
- Inside `message`, escape `"` as `\\"` and `\\` as `\\\\` so the line can be parsed as a single token. Keep `message` short and concrete.
- Do not emit `::task-status{...}` events that mention `CLI exit code:` — the launcher uses that exact text as a sentinel to detect CLI completion.
- Status events are advisory only: they do not replace your normal output, tool calls, or final response.
"""


CLAUDE_PROGRESS_RULES = """## Claude Stage Reporting
While working, emit `::task-status{...}` events at these points so the runtime terminal can show your current phase:
- `planning` when you start analyzing the task.
- `reading` immediately before each Read tool call or artifact load.
- `editing` immediately before each Edit / Write / NotebookEdit tool call.
- `running` immediately before each Bash tool call that executes a command.
- `testing` immediately before running the test command or verification step.
- `writing` immediately before writing `docs/IMPLEMENTATION_REPORT.md`.
- `done` once, when you consider the implementation complete.

Keep `message` concrete (for example `Updating prompt generation logic`, `Running pytest`). Emit a new event each time you transition phases so the user can follow progress in real time. Preserve all other Safety Rules and Required Work below.
"""


CODEX_PROGRESS_RULES = """## Codex Stage Reporting
While reviewing, emit `::task-status{...}` events so the runtime terminal can show your current phase:
- `reading` when you load the Git diff, test results, or another artifact.
- `reviewing` while you assess correctness, regressions, missing tests, or security-sensitive behavior.
- `blocked` if you cannot complete the review (for example, missing artifacts).
- `done` once, immediately before emitting your final JSON response.

Critical constraint: the `::task-status{...}` lines are advisory output for the streaming terminal only. Your FINAL response MUST be a single JSON object that matches the schema below. The final JSON MUST NOT contain any `::task-status` events, Markdown fences, code blocks, or prose. The launcher writes only your final response to `CODEX_REVIEW.json`, so any status event that leaks into the final response will corrupt the review file.
"""


def write_claude_implementation_prompt(task: Task, task_dir: Path) -> Path:
    path = task_dir / "CLAUDE_IMPLEMENT_PROMPT.md"
    content = f"""# Claude Implementation Prompt

## Task
Title: {task.title}

Description:
{task.description or "(empty)"}

Target project:
{task.projectPath}

Round:
{task.round} / {task.maxRounds}

Acceptance criteria:
{task.acceptance or "(empty)"}

Test command:
{task.testCommand or "(auto-detect)"}

{SAFETY_RULES}
{RUNTIME_PROGRESS_PROTOCOL}
{CLAUDE_PROGRESS_RULES}
## Required Work
- Modify the target project to satisfy the task and acceptance criteria.
- Write the implementation report to `docs/IMPLEMENTATION_REPORT.md` in the target project.
- After you finish, return to the web page and let the user click "Claude completed".
"""
    task_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    task.add_artifact("CLAUDE_IMPLEMENT_PROMPT.md", path.name)
    return path


def write_codex_review_prompt(task: Task, task_dir: Path) -> Path:
    path = task_dir / "CODEX_REVIEW_PROMPT.md"
    status = _read_artifact(task_dir / f"git_status_round_{task.round}.txt")
    diff_stat = _read_artifact(task_dir / f"git_diff_stat_round_{task.round}.txt")
    diff = _read_artifact(task_dir / f"git_diff_round_{task.round}.diff")
    tests = _read_artifact(task_dir / f"test_results_round_{task.round}.txt")
    output_path = task_dir / "CODEX_REVIEW.json"
    schema = json.dumps(REVIEW_SCHEMA_EXAMPLE, ensure_ascii=False, indent=2)
    content = f"""# Codex Review Prompt

## Task
Title: {task.title}

Description:
{task.description or "(empty)"}

Current round:
{task.round} / {task.maxRounds}

Target project:
{task.projectPath}

{SAFETY_RULES}
{RUNTIME_PROGRESS_PROTOCOL}
{CODEX_PROGRESS_RULES}
## Review Requirements
- Review the implementation using the Git and test artifacts below.
- Focus on correctness, regressions, missing tests, and security-sensitive behavior.
- Do not edit files. The launcher will save your final response to:
  `{output_path}`
- Return only the same JSON object as the final response. Do not wrap it in Markdown fences or add any prose.
- The final JSON MUST NOT contain `::task-status` events. Status events belong only in the streaming terminal output.
- Allowed statuses: `PASS`, `NEEDS_FIX`, `BLOCKED`, `FAILED`.
- `PASS` requires an empty `findings` array.
- `NEEDS_FIX` requires at least one finding.
- Every finding file must be relative to the target project and must not reference `.env`.

## JSON Shape Example
```json
{schema}
```

## Git Status
```text
{status}
```

## Git Diff Stat
```text
{diff_stat}
```

## Git Diff
```diff
{diff}
```

## Test Results
```text
{tests}
```
"""
    path.write_text(content, encoding="utf-8")
    task.add_artifact("CODEX_REVIEW_PROMPT.md", path.name)
    return path


def write_fix_prompt(task: Task, task_dir: Path, review: dict[str, Any], next_round: int) -> Path:
    path = task_dir / f"FIX_PROMPT_ROUND_{next_round}.md"
    findings = json.dumps(review.get("findings", []), ensure_ascii=False, indent=2)
    diff = _read_artifact(task_dir / f"git_diff_round_{task.round}.diff")
    tests = _read_artifact(task_dir / f"test_results_round_{task.round}.txt")
    content = f"""# Claude Fix Prompt Round {next_round}

## Task
Title: {task.title}

Description:
{task.description or "(empty)"}

Target project:
{task.projectPath}

Round:
{next_round} / {task.maxRounds}

{SAFETY_RULES}
{RUNTIME_PROGRESS_PROTOCOL}
{CLAUDE_PROGRESS_RULES}
## Fix Requirements
- Fix only the concrete issues reported by Codex unless a small adjacent change is required.
- Preserve unrelated user changes.
- Update `docs/IMPLEMENTATION_REPORT.md` with what changed in this fix round.
- After you finish, return to the web page and let the user click "Claude completed".

## Codex Findings
```json
{findings}
```

## Current Git Diff
```diff
{diff}
```

## Current Test Results
```text
{tests}
```
"""
    path.write_text(content, encoding="utf-8")
    task.add_artifact(path.name, path.name)
    return path


def _read_artifact(path: Path) -> str:
    if not path.exists():
        return "(not generated)"
    return path.read_text(encoding="utf-8", errors="replace")
