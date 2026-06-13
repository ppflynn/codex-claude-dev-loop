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
## Review Requirements
- Review the implementation using the Git and test artifacts below.
- Focus on correctness, regressions, missing tests, and security-sensitive behavior.
- Do not edit files. The launcher will save your final response to:
  `{output_path}`
- Return only the same JSON object as the final response. Do not wrap it in Markdown fences or add any prose.
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
