# Codex-Claude Dev Loop

Codex-Claude Dev Loop 是一个本地运行的 AI 编程协作控制台，用来管理 Claude Code 实施、测试执行、Codex 审查、多轮修复和运行日志。当前版本重点增强了网页端任务体验和运行终端可见性。

## 当前版本重点

- 网页端任务控制台：创建任务、保存计划、推进 Claude/Codex 生命周期。
- Claude/Codex 独立 CLI 窗口：每一轮运行都有独立日志和任务产物。
- xterm.js 运行终端：网页内显示 Claude/Codex 输出，视觉接近 VS Code Terminal。
- CLI 完成感知：识别 `CLI exit code: N`，显示已退出、退出码和下一步提示。
- 自动刷新：运行中任务会周期性刷新状态，同时避免 xterm 闪烁和重复输出。
- 更大的终端区域：Claude/Codex 双终端窗口更适合观察长输出。
- VS Code 插件：提供 AI Dev Loop 任务树、状态、Prompt 和报告入口。
- 多轮修复闭环：Codex 返回 `NEEDS_FIX` 后可进入下一轮 Claude 修复。

## 最新更新内容

### Runtime Terminal

- 将旧的 `<pre>` 日志显示升级为本地静态 `xterm.js` 渲染。
- 每个任务同时提供 Claude/Codex 两个终端区域。
- 增强终端高度，避免终端只显示一行。
- 支持缺失日志、等待输出、实时连接、已完成、退出码等状态。
- SSE 日志流保留 UTF-8 增量解码，改善中文和多字节字符显示。

### CLI Completion Awareness

- 后端 metadata 现在能返回：`finished`、`exitCode`、`lastLogUpdateAt`。
- SSE `done` 事件携带 `exitCode`。
- 前端在 CLI 退出后提示用户点击对应的“Claude 已完成”或“Codex 已完成”。
- launcher 在 command-not-found 和异常路径中也会写入 `CLI exit code` sentinel，避免页面一直显示运行中。

### Task Workflow

- 任务状态继续由用户手动确认推进，不自动点击完成按钮。
- 自动刷新不会自动推进状态，只负责让 UI 更及时。
- Codex 审查结果仍使用 `CODEX_REVIEW.json`，支持 `PASS`、`NEEDS_FIX`、`BLOCKED`、`FAILED`。

## 快速启动

```powershell
cd E:\AI-Tools\codex-claude-dev-loop
powershell -ExecutionPolicy Bypass -File scripts\start-gui.ps1
```

浏览器打开：

```text
http://127.0.0.1:8765/
```

如果端口被占用：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-gui.ps1 -Port 8787
```

## 基本使用流程

1. 在网页端导入本地 Git 项目或选择已有项目。
2. 创建任务，填写标题、描述和验收标准。
3. 点击“启动 Claude CLI”，等待 Claude 实施。
4. Claude 退出后，点击“Claude 已完成”。
5. 工具收集 Git diff、运行测试并生成 Codex prompt。
6. 点击“启动 Codex CLI”。
7. Codex 退出后，点击“Codex 已完成”。
8. 如果 Codex 返回 `NEEDS_FIX`，继续下一轮 Claude 修复。
9. 如果 Codex 返回 `PASS`，任务完成。

## VS Code 插件

插件源码位于 `vscode-extension/`。开发或更新插件后编译：

```powershell
cd vscode-extension
npm.cmd run compile
```

插件默认连接：

```text
http://127.0.0.1:8765
```

## 安全边界

- 网页端不提供任意 shell 输入。
- 不通过 URL 读取任务目录外日志。
- 不自动执行 `git commit`、`git push`、`git reset --hard` 或 `git clean`。
- 不读取或展示 `.env`、密钥、token 文件。
- Claude/Codex 的任务推进需要用户确认。
- 测试失败或 Codex 审查未通过时不会判定任务通过。

## 常用验证命令

```powershell
py -3 -m pytest tests/test_gui_server.py tests/test_cli_window.py -q
py -3 -m pytest -q
cd vscode-extension
npm.cmd run compile
npm.cmd test
```

## 项目结构

```text
gui/                 Web GUI 和本地 HTTP API
gui/static/          前端页面、样式、xterm 静态资源
gui/orchestrator/    任务模型、状态机、CLI launcher、测试和 Git 工具
scripts/             GUI 启动和协同脚本
vscode-extension/    VS Code 侧边栏插件
tests/               Python 自动化测试
docs/                计划、报告、审查和运行产物
```

## License

MIT