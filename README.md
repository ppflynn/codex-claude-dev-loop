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

### 1a. 桌面应用（Windows EXE）

如果想要一个独立桌面窗口而不是浏览器标签页，可以直接运行打包好的
`CodexClaudeDevLoop.exe`（构建步骤见 [`packaging/README.md`](packaging/README.md)），
或在源码模式下用桌面入口启动：

```powershell
py -3 desktop_app.py
```

桌面入口会自动：

- 选择可用端口（默认 8765，被占用会自动往后找）；
- 在后台启动 `gui/server.py`，不打开浏览器；
- 用 pywebview 打开 1400x900 的原生窗口（未安装 pywebview 时回退到默认浏览器）；
- 把 `.gui`、settings、logs 重定向到 `%LOCALAPPDATA%\CodexClaudeDevLoop`；
- 启动时检测 Git / PowerShell（缺失会阻止启动）和 Claude CLI / Codex CLI
  （缺失只提示功能不可用）；
- 关闭窗口时优雅停止后端服务。

源码模式仍会把 `.gui` 写到仓库目录；要让源码模式也写到用户数据目录，设置
环境变量 `CCDL_STATE_DIR` 或传 `--state-dir` / `--user-data-dir`。

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
4. 如果导入的是主工作树或任一 worktree，工具会自动发现同仓库的其它 worktree，并在项目列表里按「主干 → 分支/worktree」的树形结构展示。同一个仓库的主工作树和所有 worktree 拥有稳定的 `repoId` 标识，方便切换。

### 4. 创建任务并运行

1. 在中间任务表单里填写标题、描述、验收标准、测试命令、最大轮次，点「创建任务」。
2. 在任务控制区点「启动 Claude CLI」，弹出的 PowerShell 窗口里 Claude 会按 prompt 实现。
3. Claude 实现完，回到网页；底部 Claude 终端在 CLI 退出后会提示「请点击 Claude 已完成」，点对应按钮即可推进任务。
4. 任务进入「等待 Codex」状态后，点「启动 Codex CLI」让 Codex 审查本轮变更。
5. Codex 退出后页面同样会提示「请点击 Codex 已完成」。
6. 如果审查结果是 `NEEDS_FIX`，工具会自动生成下一轮修复 prompt 并回到「等待 Claude」；如果是 `PASS`，任务结束。

详细的首次使用步骤见 [`docs/QUICK_START.md`](docs/QUICK_START.md)。

### 5. Worktree 工作流与 PASS 后一键提交/合并

工具内置完整的 Git worktree 开发流，所有变更类 Git 操作只能由 GUI 后端在用户点击明确按钮时触发：

1. 选中一个主工作树（type=primary）项目后，左侧出现「从当前主干创建 worktree」表单。填写分支名（如 `feature/short-task`）和目标路径，点「创建 worktree」即可新建一个开发工作区，新 worktree 会自动出现在项目树中。工具会校验：
   - 分支名合法（不含 `.env`、不以 `..` 开头、不与主干同名）；
   - 目标路径不存在且不在主工作树目录下；
   - 主工作树当前是干净的，否则拒绝创建。
2. 在 worktree 节点上创建任务、运行 Claude/Codex 流程与普通项目一致。PLAN、任务列表、终端展示都会绑定到该 worktree。
3. 任务进入 `PASS` 状态后，下方会出现「一键提交」按钮。点击后填写提交信息（即 Git 节点名），工具会：
   - 校验任务状态必须是 `PASS`，且未在运行、未归档、未在回收站；
   - 校验 worktree 确实有改动（拒绝空提交）；
   - 校验改动里没有 `.env`、`.env.*` 或路径段为 `.env` 的文件；
   - 执行 `git add -A` 和 `git commit -m "<message>"`，把 commit SHA、提交信息和提交时间写入任务 JSON 和 history。
4. 提交完成后会出现「一键合并主干」按钮。点击后工具会：
   - 校验任务已提交且未合并；
   - 校验任务记录里有当前轮次的 reviewed base（`reviewedRound == round` 且 `reviewedHeadSha` 非空），否则拒绝合并；
   - 找到同 `repoId` 下可用的主工作树；
   - 校验主工作树必须干净、源分支必须存在，且本轮受影响路径没有自定义合并驱动或 smudge / process 过滤；
   - 通过 `git merge-tree --write-tree` 计算合并结果树，`git commit-tree` 直接构造合并提交，再用 `git update-ref HEAD <new> <expected>` 做 compare-and-swap 推进 HEAD，最后用 `git read-tree -m -u <old> <new>` 同步 index / 工作区；
   - 持久化合并恢复日志：依次覆盖 CAS 前后、guarded materialization、任务元数据与审计落盘；全部一致后才删除。崩溃后在 `task → repo` 锁序下核对 ref / index / worktree 与不可变操作身份，能证明一致时完成或反向 CAS，出现 drift 或用户编辑时保留日志并提示人工对账；
   - 合并被拒绝时不会推进 HEAD；冲突时 `git merge-tree` 直接失败，不需要 `--abort`；
   - 成功时记录 `mergeCommitSha`、`mergeTargetBranch`、`mergedAt`。

工具**永远不会**执行：`git push`、`git branch -D`、`git reset`、`git clean`、`git worktree remove`、自动解决冲突或自动删除 worktree/分支。需要推送或清理时由用户在自己的终端里手动完成。

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
- `.env`、`.env.*` 出现在本轮 diff 中时，工具会拒绝推进任务并提示用户；提交按钮也会拒绝任何包含 `.env` 路径段的变更。
- dirty worktree 时不能创建新任务；主工作树 dirty 时不能合并主干。
- Claude/Codex 的 prompt 中明确禁止 AI 自行运行 `git commit`、`git merge`、`git rebase`、`git push`、`git reset`、`git clean`、`git checkout`、`git switch`、`git restore`、`git branch -D`、`git worktree remove` 等变更类操作；提交、合并、worktree 生命周期只能由 GUI 后端在用户点击明确按钮时触发。
- 一键合并主干在 `git merge-tree` 阶段就能检测到冲突并直接拒绝，仓库不会被推进到 half-merged 状态；如果 HEAD 已推进但 `read-tree` 同步失败，会反向 CAS 回滚到合并前；进程崩溃时下一次合并或服务重启会按持久化恢复日志自动恢复或回滚，无法判定时保留现场并要求人工对账。
- 工具不自动执行 `git push`、不自动删除 worktree、不自动删除 Git 分支。
- 移除项目只会从工具配置中移除，不会删除本地项目文件、worktree 目录或 Git 分支。

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
├─ desktop_app.py                桌面 EXE 入口（pywebview + PyInstaller）
├─ packaging/                    Windows EXE 打包脚本
│  ├─ CodexClaudeDevLoop.spec    PyInstaller spec（onedir）
│  ├─ build-exe.ps1              打包入口脚本
│  ├─ installer.iss              Inno Setup 安装包脚本
│  └─ README.md                  打包流程说明
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
