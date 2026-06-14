# Implementation Report: Task Console MVP

## Summary

实现了任务控制台 MVP，在网页端和 VS Code 插件端共享同一套后端 API 数据源，新增进度展示、阶段状态、运行端标识、空 diff 检查等功能。

## Modified Files (12 files)

### Backend

#### `gui/orchestrator/models.py`
- Task dataclass 新增 4 个字段：
  - `progress: int` — 任务进度百分比 (0-100)
  - `stage: str` — 当前阶段标识 (created/claude_running/waiting_for_codex/codex_running/no_changes/review_complete 等)
  - `activeClient: str | None` — 当前运行端 ("claude" / "codex" / None)
  - `lastActivityAt: str | None` — 最后活动时间 (ISO 时间戳)
- `create()` 工厂方法初始化新字段
- `from_dict()` / `to_dict()` 支持新字段序列化

#### `gui/server.py`
- 新增 `utc_now` 导入用于时间戳
- `launch_claude_task()`: 设置 progress=20, stage="claude_running", activeClient="claude"
- `complete_claude_task()`:
  - **空 diff 检查**: 读取 diff 文件内容，若为空则设置 status=FAILED, stage="no_changes", progress=100，记录历史事件 "NO_DIFF_DETECTED"，不生成 Codex review prompt
  - 有 diff 时设置 progress=50, stage="waiting_for_codex", activeClient=None
- `launch_codex_task()`: 设置 progress=60, stage="codex_running", activeClient="codex"
- `complete_codex_task()`: 各分支设置 progress=100 或 progress=20，activeClient=None
- `cancel_task()`: 设置 activeClient=None, stage="cancelled"

### Web Frontend

#### `gui/static/app.js`
- 新增 `clientLabels` 和 `stageLabels` 中文映射
- `taskMeta()`: 显示轮次、运行端、进度百分比、更新时间
- `renderTasks()`: 任务列表项添加进度条和运行端标识，运行中任务高亮左边框
- `renderTaskDetails()`: 显示阶段、进度、运行端、更新时间
- 运行中端显示脉冲动画效果

#### `gui/static/styles.css`
- 新增 progress-bar / progress-fill 进度条样式
- 新增 task-running 边框高亮
- 新增 client-pill 运行端标识
- 新增 client-running 脉冲动画
- running 状态 pill 脉冲动画

#### `gui/static/index.html`
- 任务详情面板新增：进度（含进度条）、阶段、运行端、更新时间字段

### VS Code Extension

#### `vscode-extension/src/types.ts`
- Task 接口新增：`progress`, `stage`, `activeClient`, `lastActivityAt`, `history`
- 新增 `TaskHistoryItem` 接口

#### `vscode-extension/src/apiClient.ts`
- 新增 `fetchTaskDetail(taskId)` 函数，调用 GET /api/tasks/<id>

#### `vscode-extension/src/taskTreeProvider.ts`
- TaskTreeItem 描述格式：`R {round}/{max}  {status_label}  {activeClient}`
- 工具提示包含进度、阶段、项目名、运行端、更新时间
- 运行中状态使用 `sync~spin` 旋转图标

#### `vscode-extension/src/extension.ts`
- 新增 `openTaskDetail` 命令：获取完整任务数据，生成 Markdown 文档展示任务详情和历史
- 导入 `STATUS_LABELS` 和 `fetchTaskDetail`

#### `vscode-extension/package.json`
- 注册 `codexClaudeDevLoop.openTaskDetail` 命令
- 添加到命令面板和任务项右键菜单

### Tests

#### `tests/test_gui_server.py`
- `test_empty_diff_after_claude_marks_task_failed`: 验证空 diff 后任务标记为 FAILED，stage="no_changes"，不生成 Codex prompt
- `test_task_api_returns_progress_fields`: 验证 Task.to_dict() 包含 progress/stage/activeClient/lastActivityAt 字段，且 save/load 后字段保持

#### `vscode-extension/test/apiClient.test.ts`
- 新增 "Task type compatibility" 测试套件：
  - 验证 Task 接口包含新 progress 字段
  - 验证运行中任务的 activeClient 和 progress 字段
  - 验证空 diff 任务的 FAILED + no_changes 状态

## Test Results

### Full test suite: 98/98 passed (Round 3)

```
$ py -3 -m pytest
============================= test session starts =============================
platform win32 -- Python 3.14.0, pytest-9.0.3, pluggy-1.6.0
collected 98 items

demo-project\test_calculator.py .................                        [ 17%]
tests\test_cli_window.py ....                                            [ 21%]
tests\test_git_tools.py ......                                           [ 28%]
tests\test_gui_server.py ......................                          [ 48%]
tests\test_report_parser.py ......                                       [ 54%]
tests\test_state_machine.py ......                                       [ 61%]
tests\test_system_flow.py ..                                             [ 63%]
tests\test_task_store.py ......                                          [ 69%]
tests\test_test_runner.py ....                                           [ 73%]
tests\test_worktree.py ...........................                       [100%]

======================= 98 passed, 4 warnings in 15.01s =======================
```

### TypeScript extension tests

```
ApiError: 3 passing
STATUS_LABELS: 1 passing
STATUS_ICONS: 1 passing
Task type compatibility: 3 passing  (新增)
```

## Acceptance Criteria Verification

| 标准 | 状态 |
|------|------|
| 后端 API 包含 progress/stage/activeClient/lastActivityAt 字段 | 已实现，测试通过 |
| 网页端任务列表显示状态、轮次、进度、更新时间、运行端 | 已实现 |
| 网页端任务详情显示阶段、进度、运行端、历史 | 已实现 |
| VS Code 插件从同一 API 读取并显示任务状态、项目名、更新时间 | 已实现 |
| VS Code 插件提供 "Open Task Detail" 命令 | 已实现 |
| 网页端和插件端共享同一后端 API 数据 | 架构不变，均为 API 客户端 |
| CLAUDE_WINDOW_STARTED / CODEX_WINDOW_STARTED 时 UI 明确显示运行中 | 已实现（脉冲动画 + 运行端标识） |
| 空 Git diff 后不进入 Codex 审查，标记为 FAILED | 已实现，测试通过 |
| 测试覆盖空 diff、progress 字段、类型兼容 | 已实现，全部通过 |

## Fix Round 3: Codex Findings Resolution

### P2-1: Stale progress/stage after missing Codex review (`gui/server.py`)

**Issue**: When Codex review output is missing, `complete_codex_task` cleared `activeClient` but left `progress` at 60 and `stage` as `codex_running`. The API then reported status `WAITING_FOR_CODEX` with contradictory stage `codex_running`.

**Fix**: In the CODEX_REVIEW_MISSING branch, now also sets `progress=50` and `stage="waiting_for_codex"` for consistent state.

**Test**: `test_missing_codex_review_sets_consistent_progress_and_stage` — verifies status=WAITING_FOR_CODEX, progress=50, stage="waiting_for_codex", activeClient=None.

### P2-2: XSS vulnerability in renderTasks badge (`gui/static/app.js`)

**Issue**: `renderTasks` built badge markup with `innerHTML` and inserted `task.activeClient` via `clientLabels[task.activeClient] || task.activeClient`. A malformed task record could inject HTML when `activeClient` was not in the known labels map.

**Fix**: Replaced `innerHTML` string concatenation with DOM `createElement` + `textContent` for both the status badge and the activeClient badge, eliminating HTML injection surface.

### P1-1: Empty diff check misses staged-only changes (`gui/server.py`)

**Issue**: `complete_claude_task` treated an empty diff file as proof of no changes, but `git diff` excludes staged changes. Staged-only or untracked-file-only work could be falsely marked FAILED.

**Fix**: Enhanced the no-change check to also examine `git_artifacts.status` and `git_artifacts.diff_stat`. The task is now only marked as no-changes when all three signals (diff content, git status, diff stat) are empty.

**Test**: `test_staged_only_changes_not_treated_as_no_diff` — verifies that when git status shows staged changes but diff is empty, the task proceeds to tests instead of being marked FAILED.

### Test Results (Round 3)

```
$ py -3 -m pytest
======================= 98 passed, 4 warnings in 15.01s =======================
```

2 new tests added, all existing tests continue to pass. Total: 98 passed (up from 96 in Round 2).

## Fix Round 4: Codex Findings Resolution (P2-1, P2-2)

### P2-1: EnvFileChangedError handler leaves stale progress fields (`gui/server.py`)

**Issue**: The `EnvFileChangedError` handler in `complete_claude_task` (line 688) marked the task FAILED but did not clear `progress`, `stage`, `activeClient`, or update `lastActivityAt`. After a Claude launch (progress=20, stage="claude_running", activeClient="claude"), an `EnvFileChangedError` would leave these fields in their running state, despite the terminal FAILED status — the same class of bug as the GitError handler that was fixed in Round 3.

**Fix**: Added progress/stage/activeClient/lastActivityAt cleanup (progress=20, stage="git_collection_failed", activeClient=None, lastActivityAt=utc_now) before marking FAILED, mirroring the GitError handler.

**Note on P2-1 scope**: The finding also mentioned a `GitError` path in `complete_codex_task`, but `complete_codex_task` does not perform any git operations (`collect_git_artifacts` is only called in `complete_claude_task`). All existing error paths in `complete_codex_task` (CODEX_REVIEW_MISSING, ReportValidationError, terminal review, NEEDS_FIX, max rounds) already properly clear progress fields as of Round 3.

### P2-2: Legacy task JSON loading without progress/stage/activeClient (`gui/orchestrator/models.py`)

**Issue**: `Task.from_dict()` loaded legacy task records (saved before the progress/stage/activeClient fields were added) with `progress=0`, `stage=""`, `activeClient=None` regardless of status. Existing `.gui/tasks` records with status `CLAUDE_WINDOW_STARTED` or `CODEX_WINDOW_STARTED` would therefore show no running client or stage in both the web UI and VS Code extension.

**Fix**: Added three helper functions — `_progress_for_status()`, `_stage_for_status()`, `_client_for_status()` — that derive sensible defaults from the task status when the corresponding field is missing from the JSON. Modified `Task.from_dict()` to detect missing keys (`"progress" not in data`, etc.) and use the derived defaults instead of hardcoded values. Explicit values in the JSON are always preserved.

Status-to-default mapping:
| Status | Progress | Stage | ActiveClient |
|--------|----------|-------|-------------|
| CLAUDE_WINDOW_STARTED | 20 | claude_running | claude |
| WAITING_FOR_CODEX | 50 | waiting_for_codex | None |
| CODEX_WINDOW_STARTED | 60 | codex_running | codex |
| PASS / BLOCKED | 100 | review_complete | None |
| FAILED | 100 | no_changes | None |
| CANCELLED | 100 | cancelled | None |
| NEEDS_FIX / WAITING_FOR_CLAUDE | 20 | fix_round | None |
| CREATED (default) | 0 | created | None |

### Test Results (Round 4)

```
$ py -3 -m pytest
====================== 107 passed, 4 warnings in 14.54s =======================
```

9 new tests added (1 EnvFileChangedError + 7 legacy from_dict + 1 Codex completion flow), all existing tests continue to pass. Total: 107 passed (up from 98 in Round 3).

### Files Modified

| File | Change |
|------|--------|
| `gui/server.py` | Added progress/stage/activeClient cleanup in EnvFileChangedError handler |
| `gui/orchestrator/models.py` | Added `_progress_for_status`, `_stage_for_status`, `_client_for_status` helpers; modified `from_dict` to derive defaults from status for legacy records |
| `tests/test_gui_server.py` | Added 9 tests: EnvFileChangedError cleanup, 7 legacy from_dict derivation tests (CLAUDE_RUNNING, CODEX_RUNNING, WAITING_FOR_CODEX, FAILED, CANCELLED, CREATED, explicit preservation), Codex completion PASS flow |
