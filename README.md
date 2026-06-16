# Codex Claude Dev Loop

Codex Claude Dev Loop 是一个本地 AI 编程协作控制台，用来把 Codex 的计划和审查、Claude Code 的实现、测试命令、修复轮次、Git 变更收集串成一个可观察的开发循环。

## 这个工具解决什么

- Codex 写计划、写审查，Claude 负责实现，两者协作时切换窗口成本很高。
- 测试失败、需要修复的发现（findings）很难稳定地传到下一轮 Claude。
- 想看 Claude / Codex 当前的输出，又不想被一堆终端窗口淹没。
- 需要把每次运行的状态、产物、diff、审查结果沉淀到任务目录，方便事后回溯。

这个工具用一个本地 Web 工作台 + PowerShell CLI 窗口把上述流程串起来。

## 工作台布局

页面分为四块，最大化时默认显示：

- 左侧：项目列表 + 任务列表（含「当前 / 已归档 / 回收站」三个视图）。
- 中间上：项目顶栏（初始化、保存 PLAN、移除项目），`PLAN.md` 编辑器。
- 中间下：任务创建表单 + 任务控制按钮（启动 Claude、Claude 已完成、启动 Codex、Codex 已完成、取消任务、归档 / 恢复 / 删除）。
- 右侧：详情检查器（当前任务、历史、产物三个 Tab）。
- 底部：Claude / Codex 双 xterm.js 只读终端，会自动连接当前任务的日志流，并在 CLI 退出后提示用户点击「已完成」。

宽度不足 1200px 时右侧详情自动折叠为浮层，点顶栏「详情」按钮可调出。

## 快速开始

### 1. 启动 GUI

最简单的方式：在仓库根目录双击 `start.bat`，或在 PowerShell 里：

```powershell
.\start.bat
```

也可以直接调用 PowerShell 启动脚本：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-gui.ps1
```

默认地址：<http://127.0.0.1:8765/>

### 2. 指定端口

端口被占用时换一个：

```bat
start.bat 8787
```

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-gui.ps1 -Port 8787
```

### 3. 导入项目

打开网页控制台后：

1. 在左侧「导入项目目录」处粘贴一个 Git 仓库路径，点「导入」。
2. 在项目列表里点中刚导入的项目，中间区域显示项目信息。
3. 如果项目还不是协同项目（kind 不是 orchestrator），点顶栏「初始化项目」复制协同脚本和 `docs/` 模板。

### 4. 创建任务并运行

1. 在中间任务表单里填写标题、描述、验收标准、测试命令、最大轮次，点「创建任务」。
2. 在任务控制区点「启动 Claude CLI」，弹出的 PowerShell 窗口里 Claude 会按 prompt 实现。
3. Claude 实现完，回到网页；底部 Claude 终端在 CLI 退出后会提示「请点击 Claude 已完成」，点对应按钮即可推进任务。
4. 任务进入「等待 Codex」状态后，点「启动 Codex CLI」让 Codex 审查本轮变更。
5. Codex 退出后页面同样会提示「请点击 Codex 已完成」。
6. 如果审查结果是 `NEEDS_FIX`，工具会自动生成下一轮修复 prompt 并回到「等待 Claude」；如果是 `PASS`，任务结束。

详细的首次使用步骤见 [`docs/QUICK_START.md`](docs/QUICK_START.md)。

## CODEX_REVIEW.json

Codex 的审查结果通过 `docs/CODEX_REVIEW.json` 回传给编排器。需要修复时示例：

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

每个任务的产物都写在 `.gui/tasks/<task-id>/` 目录下，主要文件：

| 文件 | 用途 |
| --- | --- |
| `CLAUDE_IMPLEMENT_PROMPT.md` | 第一轮 Claude 的实现 prompt |
| `FIX_PROMPT_ROUND_<n>.md` | 第 n 轮修复 prompt |
| `CODEX_REVIEW_PROMPT.md` | Codex 审查 prompt |
| `claude_window_round_<n>.log` | Claude CLI 窗口日志 |
| `codex_window_round_<n>.log` | Codex CLI 窗口日志 |
| `git_status_round_<n>.txt` | 本轮 git status |
| `git_diff_stat_round_<n>.txt` | 本轮 git diff --stat |
| `git_diff_round_<n>.diff` | 本轮 git diff |
| `CODEX_REVIEW.json` | Codex 审查结果 |

项目侧的 `docs/` 目录还会保留一些可被外部工具引用的产物，如 `IMPLEMENTATION_REPORT.md`、`REVIEW_INPUT.md`、`CHANGES_STATUS.txt`、`CHANGES_DIFF.txt`。

任务状态、历史、归档、回收站都写入工具的 `.gui/` 目录，移除项目或删除任务记录都不会删本地代码。

## VS Code 扩展

仓库包含 `vscode-extension/`，用于在 VS Code 侧边栏查看任务。更新扩展源码后需要编译：

```powershell
cd vscode-extension
npm.cmd run compile
```

网页端仍是主要控制台；VS Code 扩展适合作为任务查看和辅助入口。

## 安全边界

- 网页终端是只读镜像，不提供任意命令输入。
- 终端日志路径由任务 ID、客户端和轮次派生，避免任意路径读取。
- `.env`、`.env.*` 出现在本轮 diff 中时，工具会拒绝推进任务并提示用户。
- dirty worktree 时不能创建新任务。
- 移除项目只会从工具配置中移除，不会删除本地项目文件。

## 测试

后端和 GUI 行为主要通过 pytest 覆盖：

```powershell
py -B -m pytest tests/test_gui_server.py tests/test_cli_window.py -q
py -B -m pytest -q -p no:cacheprovider
```

VS Code 扩展测试：

```powershell
cd vscode-extension
npm.cmd test
```

## 目录结构概览

```
codex-claude-dev-loop-vscode/
├─ start.bat                     一键启动入口
├─ scripts/
│  ├─ start-gui.ps1              GUI 启动脚本（含端口参数）
│  ├─ run-claude.ps1             Claude/Codex 修复循环
│  └─ test-orchestrator.ps1      编排器自测脚本
├─ gui/
│  ├─ server.py                  本地 Web 服务 + REST API + SSE
│  ├─ orchestrator/
│  │  ├─ models.py               Task 数据模型
│  │  ├─ state_machine.py        状态机
│  │  ├─ store.py                任务落盘 / 归档 / 回收站
│  │  ├─ git_tools.py            Git 工作树检查、diff 收集、.env 保护
│  │  ├─ prompts.py              Claude / Codex prompt 生成
│  │  ├─ adapters.py             Claude/Codex CLI 窗口适配器
│  │  ├─ report_parser.py        CODEX_REVIEW.json 解析
│  │  ├─ cli_window.py           PowerShell 窗口启动封装
│  │  ├─ path_safety.py          子路径校验
│  │  └─ test_runner.py          测试命令执行
│  └─ static/
│     ├─ index.html              网页工作台结构
│     ├─ app.js                  前端状态 / 渲染 / 终端流
│     ├─ styles.css              布局和视觉
│     └─ xterm/                  xterm.js 资源
├─ docs/
│  ├─ QUICK_START.md             首次使用步骤
│  ├─ PLAN.template.md           开发计划模板
│  ├─ IMPLEMENTATION_REPORT.template.md
│  ├─ CODEX_REVIEW.schema.json   CODEX_REVIEW.json 校验 schema
│  └─ ...
├─ tests/                        pytest 测试套件
└─ vscode-extension/             VS Code 侧边栏扩展
```
