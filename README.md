# Codex Claude Dev Loop

Codex Claude Dev Loop 是一个本地 AI 编程协作控制台，用来把 Codex 的计划和审查、Claude Code 的实现、测试命令、修复轮次、Git 变更收集串成一个可观察的开发循环。

当前版本重点改进了网页工作台和运行终端：Claude/Codex 的输出不再挤在只能看一行的小区域里，而是使用更大的底部双终端面板，并用 xterm.js 呈现接近 VS Code 终端的观感。

## 当前更新

- 重做网页工作台布局：左侧放项目和任务列表，中间放 PLAN 与任务控制，右侧放任务详情、历史和产物，底部放运行终端。
- 扩大 Claude/Codex 终端显示区域，修复终端区域过小、只能显示一行的问题。
- Claude/Codex 终端改为 xterm.js 只读镜像，使用 VS Code 风格颜色、等宽字体、滚动缓冲和自动适配尺寸。
- 增加运行完成感知：CLI 日志出现退出码后，网页端会提示对应的“已完成”按钮，减少不知道该点哪里的问题。
- 增加任务状态自动刷新：运行中的任务会自动刷新状态、进度、活跃端和终端连接，减少手动刷新依赖。
- 最大任务轮次从 10 扩展到 15，适合更长的 Claude/Codex 修复循环。
- 保留任务归档、回收站、历史记录、产物查看和终端历史回看能力。

## 适合场景

- 让 Codex 先整理计划和验收标准，再让 Claude Code 按计划实现。
- Claude 完成后收集测试、diff 和实现报告，再交给 Codex 做结构化审查。
- Codex 返回 `NEEDS_FIX` 时，把 findings 注入下一轮 Claude 修复。
- 需要在网页里持续观察 Claude/Codex CLI 输出，而不是切换多个终端窗口。

## 启动网页控制台

在 Windows PowerShell 中运行：

```powershell
cd E:\AI-Tools\codex-claude-dev-loop
powershell -ExecutionPolicy Bypass -File scripts\start-gui.ps1
```

默认地址：

```text
http://127.0.0.1:8765/
```

端口被占用时可以指定端口：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-gui.ps1 -Port 8787
```

## 基本使用流程

1. 打开网页控制台。
2. 导入目标 Git 项目目录。
3. 在 `PLAN.md` 区域整理开发计划和验收标准。
4. 创建任务，设置标题、描述、验收标准、测试命令和最大轮次。
5. 点击启动 Claude CLI，让 Claude 实现或修复。
6. 在底部 Claude 终端观察输出；CLI 结束后点击 Claude 已完成。
7. 点击启动 Codex CLI，让 Codex 审查本轮变更。
8. 在底部 Codex 终端观察输出；CLI 结束后点击 Codex 已完成。
9. 如果审查结果是 `NEEDS_FIX`，继续下一轮；如果是 `PASS`，任务结束。

## CODEX_REVIEW.json

Codex 审查结果通过 `docs/CODEX_REVIEW.json` 传回编排器。需要修复时示例：

```json
{
  "status": "NEEDS_FIX",
  "findings": [
    {
      "id": "R1-1",
      "severity": "P1",
      "file": "src/example.py",
      "line": 12,
      "description": "这里描述需要修复的问题。",
      "fix_suggestion": "这里给出建议修复方式。"
    }
  ],
  "reviewed_at": "2026-06-16T00:00:00Z",
  "review_scope": "review1",
  "summary": "本轮审查发现 1 个需要修复的问题。"
}
```

通过时示例：

```json
{
  "status": "PASS",
  "findings": [],
  "reviewed_at": "2026-06-16T00:00:00Z",
  "review_scope": "review2",
  "summary": "本轮审查通过。"
}
```

## 运行产物

常用产物包括：

| 文件 | 用途 |
| --- | --- |
| `docs/IMPLEMENTATION_REPORT.md` | Claude 的实现报告 |
| `docs/REVIEW_INPUT.md` | 提供给 Codex 审查的输入包 |
| `docs/CODEX_REVIEW.json` | Codex 的结构化审查结果 |
| `docs/CHANGES_STATUS.txt` | 本轮 Git 状态 |
| `docs/CHANGES_DIFF.txt` | 本轮 Git diff |
| `.gui/tasks/` | 网页任务状态、历史和日志索引 |

Claude/Codex 窗口日志按任务轮次保存，例如：

```text
claude_window_round_1.log
codex_window_round_1.log
```

网页底部终端会根据当前任务和轮次自动连接对应日志流。

## VS Code 扩展

仓库包含 `vscode-extension/`，用于在 VS Code 侧边栏查看任务。更新扩展源码后需要编译：

```powershell
cd vscode-extension
npm.cmd run compile
```

当前网页端仍是主要控制台；VS Code 扩展适合作为任务查看和辅助入口。

## 安全边界

- 网页终端是只读镜像，不提供任意命令输入。
- 终端日志路径由任务 ID、客户端和轮次派生，避免任意路径读取。
- 移除项目只会从工具配置中移除，不会删除本地项目文件。
- 任务产物写入 `.gui/tasks` 和项目 `docs/`，便于审查和回溯。

## 测试

后端和 GUI 行为主要通过 pytest 覆盖：

```powershell
python -m pytest tests/test_gui_server.py tests/test_cli_window.py -q
python -m pytest -q
```

VS Code 扩展测试：

```powershell
cd vscode-extension
npm.cmd test
```