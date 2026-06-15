# 运行终端嵌入网页实施计划

## 任务定位

为 Agent 协同控制台增加“网页内运行终端”能力。Claude 和 Codex 启动后，各自固定一个可追踪的终端会话，网页端实时显示对应输出，让用户能看到实施、测试、审查过程，而不是只看到“运行中”状态。

本次任务只做只读终端镜像：网页显示 Claude/Codex CLI 输出、进度、活跃端和日志位置，不在网页里提供任意命令输入。

目标开发工作树：

```text
E:\AI-Tools\codex-claude-dev-loop-vscode
```

稳定版目录只负责保存任务控制记录和展示 GUI，不作为本次功能代码修改目标。

## 当前基础

- `gui/orchestrator/cli_window.py` 已为 Claude/Codex 生成独立 PowerShell 启动脚本。
- 启动脚本已把 stdout/stderr 按 chunk 写入 `claude_window_round_N.log` 和 `codex_window_round_N.log`。
- `gui/server.py` 已有旧版 `/api/runs/current/stream` SSE，但任务式 Claude/Codex 窗口没有对应的网页日志流。
- `Task` 已有 `progress`、`stage`、`activeClient`、`lastActivityAt`、`history`，可以承接运行状态展示。
- 前端已有任务列表、任务详情、任务历史和任务产物面板，但没有实时终端区域。

## 本次范围

1. 后端为任务窗口增加终端会话元数据。
2. 后端提供安全的任务终端读取/流式接口。
3. 前端增加 Claude/Codex 双终端面板，并随选中任务自动加载和追踪。
4. 启动 Claude/Codex 后，网页能立即显示对应日志流。
5. 任务完成、失败、等待 Codex、进入修复轮时，终端面板保持可回看。
6. 增加测试覆盖日志流、路径安全、缺失日志和任务状态展示数据。

## 不做事项

- 不在网页端提供任意 shell 输入框。
- 不把用户输入拼接进系统命令。
- 不实现完整交互式 TTY、PTY 或 ConPTY。
- 不读取 `.env`、密钥文件或 task 目录外的任意路径。
- 不自动 commit、push、merge 或切换分支。
- 不删除历史任务日志。

## 建议修改文件

后端：

- `gui/orchestrator/models.py`
  - 可选增加 `terminalSessions` 字段，或在 API 层由现有 `claudeWindow`、`codexWindow` 派生。
  - 保持旧任务 JSON 兼容。

- `gui/orchestrator/cli_window.py`
  - 在 `launch_cli_window` 返回值中明确包含 `kind`、`round`、`logFile`、`startedAt`、`pid`。
  - 保持当前 chunk 写日志机制，确保输出实时 flush。

- `gui/server.py`
  - 新增终端元数据 API，例如 `GET /api/tasks/{task_id}/terminals`。
  - 新增安全日志流 API，例如 `GET /api/tasks/{task_id}/terminals/{client}/stream?offset=0`。
  - `client` 只允许 `claude` 或 `codex`。
  - 日志文件只能解析为当前任务目录下的固定命名：`claude_window_round_N.log` 或 `codex_window_round_N.log`。
  - SSE 事件建议包含 `client`、`round`、`offset`、`text`、`done`、`pidAlive`、`updatedAt`。
  - 如果日志不存在，返回空终端状态，而不是 500。

前端：

- `gui/static/index.html`
  - 在右侧检查器中增加“运行终端”面板。
  - 提供 Claude/Codex 两个 tab 或分栏，默认跟随当前 `activeClient`。

- `gui/static/app.js`
  - 选中任务后加载终端元数据。
  - 使用 `EventSource` 订阅当前任务的终端输出。
  - 切换任务、切换 tab、任务结束时正确关闭旧连接。
  - 自动滚动到底部，同时允许用户向上查看历史。
  - 当没有日志时显示“尚未启动”，不要只显示空白。

- `gui/static/styles.css`
  - 增加紧凑的终端面板样式。
  - Claude/Codex tab、运行中状态、断开重连状态要可扫描。
  - 保持现有控制台风格，不做落地页或装饰性大改。

测试：

- `tests/test_gui_server.py`
  - 覆盖终端元数据 API。
  - 覆盖 SSE 或增量读取时的 offset 行为。
  - 覆盖非法 client、路径越界、缺失日志文件。
  - 覆盖旧任务没有 terminal 字段时 API 仍可返回安全默认值。

- `tests/test_cli_window.py`
  - 覆盖 launch 返回 logFile/kind/round。
  - 覆盖生成脚本仍按 chunk 写入日志，不退化为一次性 ReadToEnd。

## 状态与任务控制

任务进入 `CLAUDE_WINDOW_STARTED`：

- `activeClient = "claude"`
- `stage = "claude_running"`
- `progress` 至少为 `20`
- Claude 终端 tab 显示运行中，开始追踪 `claude_window_round_N.log`

任务进入 `WAITING_FOR_CODEX`：

- Claude 终端保留历史输出。
- 任务详情显示 Git/Test 已收集。
- Codex 终端显示待启动。

任务进入 `CODEX_WINDOW_STARTED`：

- `activeClient = "codex"`
- `stage = "codex_running"`
- `progress` 至少为 `60`
- Codex 终端 tab 显示运行中，开始追踪 `codex_window_round_N.log`

任务进入 `NEEDS_FIX` 或下一轮 `WAITING_FOR_CLAUDE`：

- 保留上一轮 Claude/Codex 终端历史。
- 新一轮启动后根据 round 切到新的日志文件。

任务进入 `PASS`、`FAILED`、`BLOCKED`、`CANCELLED`：

- 停止实时追踪。
- 保留终端输出可回看。
- 页面明确显示最终状态和最后日志更新时间。

## 验收标准

- 网页端选中任务后能看到 Claude 和 Codex 的终端区域。
- 启动 Claude CLI 后，网页端无需等待完成即可看到 `claude_window_round_N.log` 的新增输出。
- 启动 Codex CLI 后，网页端无需等待完成即可看到 `codex_window_round_N.log` 的新增输出。
- 终端输出来自任务目录内固定日志文件，不允许用户通过 URL 读取任意文件。
- 非法 client、越界路径、缺失日志都不会导致服务崩溃。
- 任务切换时不会继续向旧任务终端写 UI。
- 任务完成后终端输出仍可查看。
- 旧任务 JSON 没有新字段时，任务列表和详情仍能正常渲染。
- 不新增网页命令输入能力。
- 新增或更新测试，并运行通过。

## 验证命令

```powershell
python -m pytest tests/test_gui_server.py tests/test_cli_window.py -q
python -m pytest -q
cd vscode-extension
npm.cmd run compile
npm.cmd test
```

如果 `npm.cmd test` 在当前环境不可用，需要在实施报告中说明实际原因，并至少完成 `npm.cmd run compile`。

## 实施报告要求

Claude 完成后必须在目标工作树写入：

```text
docs/IMPLEMENTATION_REPORT.md
```

报告必须包含：

- 实际修改文件列表。
- 后端 API 设计和路径安全说明。
- 前端终端面板交互说明。
- 测试命令和结果。
- 未完成事项或下一步建议。
