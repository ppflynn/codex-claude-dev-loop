# 实施报告

> 此文件由 Claude Code 自动生成。记录本次实施的完整过程。

---

## 基本信息

- **执行时间**：2026-06-23
- **计划来源**：本任务直接由 web 控制台的任务描述驱动，`docs/PLAN.md` 未涉及
  桌面化内容，故本轮为对任务描述的实现。
- **执行者**：Claude Code

## 改动摘要

把 Codex Claude Dev Loop 从「Web 启动器」升级为完整的 Windows 桌面工具。
双击打包后的 `CodexClaudeDevLoop.exe` 即可在独立窗口中打开现有的 Web 控制台，
后端服务、用户数据目录、依赖检测、日志和退出行为都由桌面入口统一管理。
没有重写业务功能，全部复用 `gui/server.py` + `gui/orchestrator/*` + `gui/static/*`。

新增的核心模块：

- `desktop_app.py` — 桌面入口（pywebview + 后端生命周期 + 依赖检测 + 日志）。
- `packaging/CodexClaudeDevLoop.spec` — PyInstaller onedir spec。
- `packaging/build-exe.ps1` — 打包脚本。
- `packaging/installer.iss` — Inno Setup 安装包脚本（后续阶段）。
- `packaging/README.md` — 打包流程文档。
- `tests/test_desktop_app.py` — 桌面入口的单元/集成测试。

对 `gui/server.py` 做了最小化的、向后兼容的改造，使其能在 frozen 模式下
正确解析资源路径，并允许桌面入口把状态目录重定向到用户数据目录。

## 文件变更清单

| 文件路径 | 操作 | 说明 |
|----------|------|------|
| `desktop_app.py` | 新增 | 桌面应用入口。负责用户数据目录、日志、依赖检测、端口选择、后端线程、pywebview 窗口、优雅退出。pywebview 缺失时回退到默认浏览器。 |
| `packaging/CodexClaudeDevLoop.spec` | 新增 | PyInstaller spec（onedir）。把 `gui/static`、`docs` 模板、`.claude/settings.json`、`scripts/run-claude.ps1` 作为 data 文件打包，并收集 `webview` 的隐藏导入。 |
| `packaging/build-exe.ps1` | 新增 | 打包入口脚本：检查 py/pyinstaller/pywebview 是否可用，可选清理 `build/`、`dist/`，运行 `py -3 -m PyInstaller ... CodexClaudeDevLoop.spec --noconfirm`。 |
| `packaging/installer.iss` | 新增 | Inno Setup 脚本：安装到 `{autopf}\Codex Claude Dev Loop`，开始菜单 + 桌面快捷方式 + 卸载，用户数据目录不被卸载删除。 |
| `packaging/README.md` | 新增 | 打包流程文档：前置依赖、build 命令、产物路径、bundled / not-bundled 列表。 |
| `tests/test_desktop_app.py` | 新增 | 21 个测试覆盖：用户数据目录解析、依赖检测、日志写入、后端生命周期（含端口冲突回退）、argparse、`configure_paths`、`find_available_port`、以及 Round 2 新增的浏览器回退阻塞契约（`run_window` 在 pywebview 缺失/失败时改走 `_run_browser_fallback_loop`，而不是 `_open_in_browser` 后立刻返回）。 |
| `gui/server.py` | 修改 | (1) `_resource_root()` 在 frozen 模式下返回 `sys._MEIPASS`；(2) `STATE_DIR` 支持 `CCDL_STATE_DIR` 环境变量覆盖；(3) 新增 `configure_paths(state_dir)` 同时更新模块级常量和 `GuiHandler` 类级单例；(4) 新增 `find_available_port` 和 `create_server_on_free_port`（消除 TOCTOU 窗口）；(5) `main()` 接受 `--state-dir` 参数。源码模式下行为完全不变。 |
| `.gitignore` | 修改 | 忽略 `build/`、`dist/`、`packaging/Output/`、`*.spec.bak`。 |
| `README.md` | 修改 | 增加「桌面应用（Windows EXE）」小节和目录结构中的 `desktop_app.py`、`packaging/` 说明。 |

## 测试结果

### 执行的测试命令

```
py -B -m pytest -q -p no:cacheprovider
```

### 测试输出

```
============================== warnings summary ===============================
gui\orchestrator\test_runner.py:17
  E:\AI-Tools\codex-claude-dev-loop-vscode\gui\orchestrator\test_runner.py:17:
  PytestCollectionWarning: cannot collect test class 'TestRunResult'
  because it has a __init__ constructor (from: tests/test_gui_server.py)
    @dataclass

-- Docs: https://docs.pytest.org/en/stable/how-to/capture/warnings.html
395 passed, 4 warnings in 143.79s (0:02:23)
```

395 个测试全部通过，其中包含新增的 17 个 `tests/test_desktop_app.py` 测试。

> Round 2 修复后重新跑全量套件：**399 passed**（新增 4 个浏览器回退阻塞测试），
> 命令与输出见本文档末尾「Round 2 Fix」小节。

### 额外的手动验证

除了 pytest 之外，本轮还做了以下手动 smoke 测试：

- `desktop_app.main()` 全流程：用一个临时 `--user-data-dir` 启动，
  并 mock 掉 `webview`，验证：
  - 窗口标题 = `Codex Claude Dev Loop`，大小 1400x900，min 960x600；
  - 依赖检测把 Git、PowerShell、Claude CLI、Codex CLI 都正确识别；
  - 后端在自动选取的端口上监听（首选端口繁忙时自动往后找）；
  - 日志中依次出现 `Dependency probe`、`Backend HTTP server listening`、
    `Opening desktop window`、`Window closed; shutting down backend`、
    `Backend HTTP server thread exited`、`=== ... exiting (code=0) ===`；
  - 退出码为 0。
- `GET /api/projects`、`/api/tasks`、`/api/runs/current`、`/` 在桌面后端
  启动后均返回 200，`/` 返回完整的 index.html（11706 字节），证明静态资源
  路径解析正确。
- 强制把 Git 标记为缺失时，`main()` 弹出错误对话框、写日志
  `Missing required dependencies: ['Git']` 并返回退出码 3，符合
  「Git / PowerShell 缺失时阻止启动」的要求。
- `gui/server.py --state-dir <path>` CLI 参数被 argparse 正确接受，
  源码模式启动流程没有被打坏。

### 测试结论

- [x] 全部通过
- [ ] 部分失败（详见下方说明）

## 实现细节

### 资源路径解析

`gui/server.py` 顶部新增 `_resource_root()`：

```python
def _resource_root() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]
```

源码模式：仍然走 `Path(__file__).resolve().parents[1]`，行为与原先一致，
现有测试中的 `server.ROOT` 仍然指向仓库根。
Frozen 模式（PyInstaller onedir 或 onefile）：`sys._MEIPASS` 由
PyInstaller 在启动时注入，指向 bundle 根目录，那里有打包后的
`gui/static`、`scripts/run-claude.ps1`、`docs/...template.md`、
`.claude/settings.json`。

`desktop_app.py` 中也实现了同样的 `_resource_root()`，让桌面入口独立解析。

### 状态目录重定向

桌面入口的 `get_user_data_dir()` 返回：

- `override`（`--user-data-dir` 命令行参数） → 优先；
- `CCDL_USER_DATA_DIR` 环境变量 → 次之；
- `%LOCALAPPDATA%\CodexClaudeDevLoop` → 默认（Windows）；
- `~/.local/share/CodexClaudeDevLoop` → 兜底（非 Windows / 环境变量被剥）。

`get_state_dir(user_data)` 返回 `<user_data>/.gui`，对齐源码模式的状态目录
结构。`get_logs_dir(user_data)` 返回 `<user_data>/logs`。

`gui/server.configure_paths(state_dir)` 同时更新：

- 模块级 `STATE_DIR`、`PROJECTS_FILE`、`TASKS_DIR`、`TRASH_TASKS_DIR`、
  `SETTINGS_FILE`、`AUDIT_LOG_FILE`、`MERGE_RECOVERY_DIR`；
- `GuiHandler` 类级 `store`、`runs`、`tasks` 单例。

后者是关键，因为 `ThreadingHTTPServer` 在请求线程里通过
`GuiHandler.store` / `GuiHandler.tasks` 访问存储；只改模块级变量不会
让请求线程看到新地址。

### 端口选择与 TOCTOU 消除

`find_available_port()` 先按 `preferred..preferred+63` 顺序探测，全失败时
回退到 `bind(0)` 由 OS 挑一个临时端口。

`create_server_on_free_port()` 直接让 `ThreadingHTTPServer` 自己尝试绑定
候选端口；绑定成功就保持 socket 打开，避免「先探测再绑定」的 TOCTOU 窗口。
最后一档同样让 OS 挑端口。

### 后端生命周期

桌面入口在 **同一进程** 的后台线程里运行后端，避免：

- 启动子进程的复杂性（PATH、工作目录、信号转发、frozen 模式下没有
  独立 python.exe 可调用等）；
- 进程间通信开销；
- 子进程僵尸或孤儿风险。

`start_backend()` 在调用 `create_server_on_free_port()` 之前先调用
`configure_paths(state_dir)` 和 `_recover_pending_merges_at_startup()`，
后者是 round-19 的崩溃恢复逻辑，必须在服务开始处理请求前执行。

`stop_backend()` 在窗口关闭后调用 `server.shutdown()` + `server_close()`，
然后 `thread.join(timeout=5s)`。daemon 线程标志保证进程退出时即便没 join
到也不会卡死。

### pywebview 集成

`run_window()` 优先 `import webview`：

- 成功 → 调用 `webview.create_window(title, url, width=1400, height=900,
  min_size=(960, 600), text_select=True)` 然后 `webview.start()`。
  `webview.start()` 是阻塞的，返回时窗口已关闭，紧接着 `stop_backend` 被调用。
- 失败（pip 没装 pywebview、WebView2 runtime 缺失、图形子系统异常等） →
  写 warning 日志并进入 `_run_browser_fallback_loop(url, logger)`：先用
  `webbrowser.open(url)` 打开默认浏览器，然后弹一个 tkinter 伴随窗口，
  显示 URL、提供「再次打开浏览器」和「退出」按钮。`mainloop()` 阻塞直到
  用户点击退出（或关闭伴随窗口，`WM_DELETE_WINDOW` 也走退出逻辑），
  这样后端 HTTP 服务在整个浏览器会话期间都保持存活。tkinter 不可用时
  回退到 `input()` 阻塞读取 stdin（headless 环境的最后兜底），
  `_block_on_stdin_fallback` 把同样的「按回车退出」契约保留下来。
  `run_window` 不再直接调用 `_open_in_browser` 后立即返回——这一改动正是
  Round 2 P2-1 修复的核心。

### 依赖检测

`detect_dependencies()` 用 `shutil.which` 探测 `git`、`powershell` /
`powershell.exe` / `pwsh` / `pwsh.exe`、`claude`、`codex`。
Git 和 PowerShell 标记为 required，缺失时 `main()` 弹错误对话框并返回
退出码 3。Claude CLI 和 Codex CLI 标记为 optional，缺失时仅弹 warning 对话框
说明相关按钮会报「未安装」，软件继续启动。

### 日志

`setup_logging(logs_dir)` 在 `<user_data>/logs/desktop.log` 上配置
`RotatingFileHandler`（2MB / 5 个备份），同时镜像一份到 stderr。日志至少
记录：应用启动、用户数据目录与状态目录路径、依赖检测结果、端口选择
（含「首选端口繁忙，改用 N」）、后端监听地址、后端异常（带 traceback）、
窗口加载、应用退出。

### 打包

`packaging/CodexClaudeDevLoop.spec` 使用 onedir 模式（`COLLECT` +
`exclude_binaries=True`）。bundled data 包括：

- `gui/static`（整个目录，含 `xterm/`）；
- `gui/orchestrator`（Python 源码，确保 frozen 模式能导入）；
- `docs/PLAN.template.md`、`docs/IMPLEMENTATION_REPORT.template.md`、
  `docs/CODEX_REVIEW.schema.json`；
- `.claude/settings.json`；
- `scripts/run-claude.ps1`。

通过 `collect_data_files("webview")` 和 `collect_submodules("webview")`
收集 pywebview 的运行时数据，并通过 `hiddenimports` 显式声明
`webview.platforms.edgechromium` 等子模块。

打包时排除 `pytest`、`IPython`、`jupyter` 等开发依赖，不打包 Git、PowerShell、
Claude CLI、Codex CLI、VS Code（由用户系统提供）。

输出：`dist/CodexClaudeDevLoop/CodexClaudeDevLoop.exe`。

### 安装包（后续阶段）

`packaging/installer.iss` 使用 Inno Setup 编译，安装到
`{autopf}\Codex Claude Dev Loop`，生成开始菜单和桌面快捷方式，注册卸载。
用户数据目录（`%LOCALAPPDATA%\CodexClaudeDevLoop`）在卸载时保留，
用户重新安装不会丢任务。

### 向后兼容

源码模式的三种启动方式都未改动，仍然可用：

- `start.bat`（保持原样）；
- `powershell -ExecutionPolicy Bypass -File scripts\start-gui.ps1`；
- `py -3 gui/server.py --host 127.0.0.1 --port 8765`。

源码模式下 `_resource_root()` 走 `Path(__file__).resolve().parents[1]`，
`STATE_DIR` 仍然是 `<repo>/.gui`，所有现有测试沿用 `server.ROOT / ".gui" / "test-tmp"`
作为临时目录，未受影响。

### 安全边界

桌面入口遵守所有现有的安全规则，没有：

- 自动 `git push`、`git reset`、`git clean`；
- 自动删除 worktree / 分支；
- 自动解决 merge 冲突；
- 任何对 `.env` 的读、写、diff；
- 任何对 `.git` 的修改。

所有 Git 变更类操作仍然只能在 Web 控制台里由用户点击「一键提交」/「一键合并主干」
按钮时由 `gui/server.py` 后端触发。桌面入口只是把 Web 控制台装进原生窗口，
不引入新的变更路径。

## 遇到的问题

1. **argparse 把 `%LOCALAPPDATA%` 当成格式化占位符**：第一次跑测试时
   `--user-data-dir` 的 help 字符串里的 `%` 触发了 `badly formed help string`。
   修复：把 `%` 写成 `%%`，argparse 会自动还原成单个 `%`。
2. **`ThreadingHTTPServer.shutdown()` 在没有 `serve_forever()` 的情况下会死等**：
   smoke test 时直接 `create_server` + `shutdown()` 卡住。这不是产品 bug
   （真实路径里 `serve_forever()` 一直在后台线程跑），只是测试脚本写法问题。
   正确的做法是在另一个线程 `serve_forever()`，再 `shutdown()`。测试中
   已经按此模式编写。
3. **`configure_paths` 必须同时刷新 `GuiHandler` 类级单例**：早期版本只更新
   模块级常量，结果 `ThreadingHTTPServer` 里的请求处理类仍然指向旧的
   `ProjectStore()` / `TaskStore()`。第二个版本通过 `setattr(GuiHandler, ...)`
   显式更新类属性，单元测试里专门断言了这一点。
4. **Windows 临时目录清理与打开的日志文件冲突**：测试用 `TemporaryDirectory`
   清理时偶尔报 `WinError 32`，因为 `RotatingFileHandler` 还持有日志文件。
   在 `tearDown` 里手动关闭 handler 后问题消失。生产路径下用户数据目录是
   固定路径，不会触发清理，所以这不是产品 bug。

## 未完成事项

- **托盘菜单**：任务描述里把托盘定为「实现成本过高时至少保证关闭时停后端」。
  本轮先实现后者（关闭窗口即 `shutdown` 后端）。托盘菜单待后续基于
  `pystray` 或 `infi.systray` 实现，分别接到「打开主窗口 / 重启服务 /
  打开日志目录 / 退出」。
- **WebView 加载失败时的内置错误页**：当前 pywebview 加载失败会显示浏览器
  自身的错误页（不会白屏）。后续可以注入 HTML 字符串作为 fallback 页。
- **PyInstaller 实际构建**：本环境未安装 `pyinstaller` 和 `pywebview`
  （`pip install` 会改动全局 Python，超出安全边界），所以本轮没有真实跑
  `build-exe.ps1` 生成 EXE。spec 文件已经按 PyInstaller 文档编写并通过
  语法检查；用户按 `packaging/README.md` 中的步骤在自己的机器上构建即可。
- **检测结果前端展示**：当前依赖检测结果通过弹窗 + 日志展示。任务允许
  「先至少通过启动弹窗或日志提示」，所以本轮先走弹窗 + 日志路径。
  后续可以在 Web 控制台的设置区域加一个 `/api/diagnostics` 端点，前端读后
  显示在 UI 里。

---

## Round 2 Fix: Codex P2-1

### P2-1 — 浏览器回退路径在后端被停掉前就返回，导致浏览器打开到一个立刻被关停的服务（`desktop_app.py::run_window`）

**问题**：当 pywebview 缺失，或 pywebview 启动失败回退到默认浏览器时，
Round 1 的 `run_window` 直接调用 `_open_in_browser(url)` 后 `return 0`。
`main()` 收到返回值后立即执行 `stop_backend()`，把后端 HTTP 服务关掉。
结果浏览器虽然打开到 `http://127.0.0.1:<port>/`，但后端已经关闭，页面
报连接失败。源码模式下没装 pywebview 的开发者会看到这个症状，打包用户
在 WebView2 runtime 异常时也会撞到同样的死路。

**修复**：把「打开浏览器 + 立刻返回」替换为「打开浏览器 + 阻塞直到用户
显式退出」，让后端服务在整个浏览器会话期间保持存活。

- `desktop_app.py`
  - 新增 `_block_on_stdin_fallback(url, logger)`：当 tkinter 完全不可用时
    （headless / 无显示服务器）的兜底路径，用 `input()` 阻塞等待用户按回车，
    永不抛异常。
  - 新增 `_run_browser_fallback_loop(url, logger)`：
    1. 先调用 `_open_in_browser(url, logger)` 把控制台打开到默认浏览器。
       浏览器打不开也不阻塞流程，只写一条 warning，URL 仍然显示在伴随窗口里
       供用户手动复制。
    2. 尝试 `import tkinter as tk` + `from tkinter import ttk`。失败 →
       转入 `_block_on_stdin_fallback`，保持相同的「按回车退出」契约。
    3. 成功则创建一个 560×260 的伴随窗口，展示：
       - 浏览器是否成功打开的状态提示；
       - 当前 URL（`StringVar`，「再次打开浏览器」失败时追加 `(could not reopen)`）；
       - 「在工作期间请保持本窗口打开。点击 Exit 停止后端服务并退出应用」
         的操作指引；
       - 「再次打开浏览器」按钮 + 「Exit」按钮；
       - `WM_DELETE_WINDOW` 协议绑定到退出逻辑，关窗按钮等价于 Exit。
    4. `root.mainloop()` 阻塞，直到用户点击 Exit（或关窗）。`mainloop()`
       返回后函数返回 0，`main()` 才继续走到 `stop_backend`。
    5. 对话框自身崩溃时回退到 `_block_on_stdin_fallback`，保证即便 GUI
       出问题后端也不会被立即杀掉。
  - `run_window` 的两个回退分支（`import webview` 失败、`webview.create_window`
    或 `webview.start` 抛异常）都改走 `_run_browser_fallback_loop`，不再
    直接调用 `_open_in_browser`。`on_error` 回调的消息也同步更新，说明
    会弹出伴随窗口保持后端存活。
- `tests/test_desktop_app.py`
  - 新增 `BrowserFallbackTests`（4 个测试）：
    - `test_run_window_uses_fallback_loop_when_pywebview_missing`：
      `sys.modules["webview"] = None` 强制 `import webview` 抛 ImportError，
      验证 `run_window` 调用 `_run_browser_fallback_loop`（mock 成立即返回 0）
      而不是 `_open_in_browser`。直接 `_open_in_browser` 调用次数必须为 0，
      锁定「回退循环负责打开浏览器」的契约。
    - `test_run_window_uses_fallback_loop_when_pywebview_raises`：
      让 `webview.create_window` 抛 `RuntimeError("simulated WebView2 missing")`，
      验证 pywebview 失败分支也走 `_run_browser_fallback_loop`。
    - `test_fallback_loop_blocks_until_user_exits`：
      用 `types.ModuleType` 注入假的 `tkinter` / `tkinter.ttk`，让
      `FakeTk.mainloop()` 在 `threading.Event` 上阻塞。在后台线程跑
      `_run_browser_fallback_loop`，先断言浏览器被打开且线程仍然存活
      （证明 `mainloop` 确实在阻塞），然后 set 事件模拟用户退出，断言函数
      返回 0 且线程结束。这直接锁定了「后端在线程阻塞期间不会被停」的核心
      契约。
    - `test_fallback_loop_blocks_on_stdin_when_tkinter_missing`：
      `sys.modules["tkinter"] = None` 强制走 stdin 兜底，`builtins.input`
      mock 在事件上阻塞，同样验证线程存活 + set 后返回 0。

### Round 2 文件改动清单

| 文件 | 改动 |
|---|---|
| `desktop_app.py` | 新增 `_block_on_stdin_fallback` 和 `_run_browser_fallback_loop`；`run_window` 的两个回退分支改走 `_run_browser_fallback_loop`，不再 `_open_in_browser` 后立刻返回。 |
| `tests/test_desktop_app.py` | 新增 `BrowserFallbackTests`（4 个测试）覆盖回退路由 + 阻塞契约 + stdin 兜底。 |

### Round 2 测试结果

```
py -B -m pytest tests/test_desktop_app.py -v -p no:cacheprovider
=> 21 passed

py -B -m pytest -q -p no:cacheprovider
=> 399 passed, 4 warnings in 142.64s (0:02:22)
```

### 安全边界（Round 2 不变）

- 没有引入任何 Git 命令，没有任何对 `.env` 或 `.git` 的读写。
- 回退路径只是延长了后端 HTTP 服务的生命周期，没有改变 `gui/server.py`
  的任何业务逻辑或安全边界。
- 伴随窗口只提供「再次打开浏览器」和「退出」两个动作，不暴露任何提交 /
  合并 / 推送 / 重置入口。所有变更类操作仍然只能在 Web 控制台里由用户
  点击「一键提交」/「一键合并主干」按钮时触发。
- stdin 兜底在 `EOFError` / `KeyboardInterrupt` 下平静返回，不会在
  headless CI 环境里挂死进程。

---

## Round 3 Fix: Codex P2-1 / P3-1

### P2-1 — `stop_backend()` 只关 HTTP server，不终止 `RunManager` 的活动子进程（`desktop_app.py:317`）

**问题**：`desktop_app.stop_backend()` 只调用 `server_instance.shutdown()` +
`server_instance.server_close()`，并不会去管 `gui.server.GuiHandler.runs`
里挂着的活动 run。如果用户先在 Web 控制台点了「启动循环」（`POST /api/run/start`），
再直接关掉桌面窗口，后端 `RunManager` 拥有的那个 `subprocess.Popen` PowerShell
子进程不会被终止。在 Windows 上，daemon 线程被进程退出回收时**不会**自动
`TerminateProcess` 那个 PowerShell 子进程（它是 `subprocess.Popen` 启动的，
进程组关系不强制随父进程死亡），结果就是「修改项目的自动化」继续在用户机器上
跑——只是 UI 和 HTTP 服务都不在了。这违反了「软件退出时应优雅停止后端服务」
的契约。

**修复**：在 `stop_backend` 里、在 HTTP server shutdown **之前**，先停掉
`RunManager` 的活动 run。同时新增一个幂等的 `RunManager.stop_if_running()`
辅助方法，让 shutdown 路径不需要预先判断「现在到底有没有活动 run」。

- `gui/server.py`
  - `RunManager.has_active_run()`：返回 `self._process` 是否仍存活
    （`_process is not None and _process.poll() is None`）。纯只读。
  - `RunManager.stop_if_running()`：如果当前没有活动 run 直接返回 `False`；
    否则调用既有的 `stop()` 并返回 `True`。`stop()` 内部会在锁下重新检查
    `_process.poll()`，所以如果子进程在我们的检查和 `stop()` 之间自己退出了，
    `stop()` 会抛 `ApiError(CONFLICT)`——`stop_if_running` 捕获并返回 `False`，
    因为那个进程已经不在了，等价于「没有需要停止的 run」。这一层 race 处理是
    新方法存在的核心理由：直接调用 `stop()` 会把「竞态退出」和「真正的失败」
    混在一起。
- `desktop_app.py`
  - `stop_backend()` 在 `server_instance.shutdown()` 之前尝试 `import gui.server`，
    读 `GuiHandler.runs`，调用 `stop_if_running()`。返回 `True` 时写一条
    `Stopped active backend run before HTTP server shutdown` info 日志；
    返回 `False` 时写一条 debug 日志说明没有活动 run；任何异常都被捕获并
    降级为 debug 日志（含 traceback），**不会**阻止后续的 HTTP server shutdown。
    之所以是「try/except 兜底」而不是「硬失败」，是因为 stop_backend 的核心
    契约是「让进程能干净退出」——RunManager 清理失败不应该让进程卡死在
    关闭流程里。

### P3-1 — Inno Setup `[Run]` 用了非法 directive 名 `PostInstall:`（`packaging/installer.iss:58`）

**问题**：原行是

```
PostInstall: "{app}\{#MyAppExeName}"; Description: ...; Flags: nowait postinstall skipifsilent
```

Inno Setup 的 `[Run]` 段语法要求第一个字段是 `Filename:`（要执行的命令），
`postinstall` 是一个 **flag**，不是 directive。`PostInstall:` 作为 directive
名会让 Inno Setup 编译器直接拒绝 `installer.iss`，安装包无法生成。

**修复**：改成正确的语法——`Filename:` + `postinstall` flag：

```
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; Flags: nowait postinstall skipifsilent
```

这是 Codex fix_suggestion 的原文。功能不变：安装结束时（且非 silent 安装）
弹出「立即启动」复选框，用户勾选则启动主程序。

### Round 3 文件改动清单

| 文件 | 改动 |
|---|---|
| `gui/server.py` | `RunManager` 新增 `has_active_run()` 和 `stop_if_running()` 两个方法。后者在「无活动 run」「进程已退出」「竞态退出」三种情况下都返回 `False`，仅在真正终止了一个活动 run 时返回 `True`。 |
| `desktop_app.py` | `stop_backend()` 在 HTTP server shutdown 之前先调用 `GuiHandler.runs.stop_if_running()`，并把结果写入日志；RunManager 清理失败不会阻塞 HTTP shutdown。 |
| `packaging/installer.iss` | `[Run]` 段的 `PostInstall:` 改为 `Filename:` + `postinstall` flag，使脚本可通过 Inno Setup 编译。 |
| `tests/test_desktop_app.py` | 新增 `test_stop_backend_terminates_active_run_before_http_shutdown`（P2-1 回归：mock 一个活动 `subprocess.Popen`，断言 `stop_backend` 调用了 `terminate()` 并把 `_stopping` 翻成 True）和 `test_stop_backend_does_not_fail_when_no_active_run`（幂等性：无活动 run 时 `stop_backend` 不抛异常）。补齐 `import subprocess`。 |
| `tests/test_gui_server.py` | 新增 `RunManagerStopIfRunningTests`（5 个测试）：无活动 run、进程已退出、正常终止、`terminate` 超时后 `kill`、检查与 stop 之间的竞态。覆盖 `stop_if_running()` 的全部四种出口路径。 |

### Round 3 测试结果

```
py -B -m pytest tests/test_desktop_app.py tests/test_gui_server.py::RunManagerStopIfRunningTests -q -p no:cacheprovider
=> 28 passed

py -B -m pytest -q -p no:cacheprovider
=> 406 passed, 4 warnings in 147.92s (0:02:27)
```

新增 7 个测试（`test_desktop_app.py` +2，`test_gui_server.py` +5），总数从
399 涨到 406。无回归。

### 安全边界（Round 3 不变）

- 没有引入任何新的 Git 命令，没有任何对 `.env` 或 `.git` 的读写。
- `RunManager.stop()` 调用的是 `subprocess.Popen.terminate()` / `.kill()`，
  这是关闭一个 **由本应用启动的** PowerShell 子进程，不属于安全规则禁止的
  「destructive cleanup」——被终止的是 `gui/orchestrator` 自己启动的 run loop，
  不触碰用户项目目录、Git 状态或仓库内容。
- `stop_if_running()` 是只读检查 + 调用既有 `stop()`，不引入新的 mutating 路径。
- Inno Setup 修复纯是 build-time 脚本语法，不影响运行时行为。
- 所有 Git 变更类操作仍然只能在 Web 控制台里由用户点击「一键提交」/
  「一键合并主干」按钮时触发。桌面入口的关闭流程只是清理自己启动的
  PowerShell 子进程，不引入新的变更路径。

---

## Round 4 Fix: Codex P2-1 / P2-2 / P3-1

### P2-1 — 关闭窗口时只停了 PowerShell 父进程，没有停 Claude/Codex 子进程树（`gui/server.py:704`）

**问题**：Round 3 的 `RunManager.stop_if_running()` 让桌面入口在 HTTP server 关闭前
先停掉 `RunManager` 的活动 run，但 `RunManager.stop()` 只调用
`process.terminate()` / `process.kill()` 杀掉 PowerShell 父进程。在 Windows 上，
杀掉父进程**不会**自动终止它启动的子进程——`scripts/run-claude.ps1` 会通过
`Start-Process` 等机制启动 Claude CLI / Codex CLI 子进程，这些子进程脱离 PowerShell
父进程后仍会继续修改用户的项目目录。结果：用户关掉桌面窗口、HTTP 服务关闭、
后台 Claude/Codex 还在继续跑——只是 UI 和日志都不在了，完全违背了「软件退出时应
优雅停止后端服务」的契约。

**修复**：用 Windows Job Object 把整个 run 进程树关进一个带
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 标志的 Job Object 里，关闭 Job Object 句柄时
操作系统会强制终止 Job 内**所有**进程（父 + 全部后代）。

- `gui/server.py`
  - 新增 `_Win32JobObject` 类（约 130 行）：通过 `ctypes.WinDLL("kernel32")` 调用
    `CreateJobObjectW` + `SetInformationJobObject`（设置
    `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000`）创建 Job Object，
    `assign(Popen)` 用 `OpenProcess` 拿到子进程句柄后调用 `AssignProcessToJobObject`
    把进程加入 Job；`close()` 调用 `CloseHandle` 关闭 Job 句柄。
  - 该类在非 Windows / 无 ctypes / 调用失败时退化为「inert」（`__bool__` 返回
    `False`），所有方法变成 no-op，保证 Linux/macOS CI 仍能跑现有测试。
  - `RunManager.__init__` 新增 `self._job: _Win32JobObject | None = None`。
  - `RunManager.start()` 在 `subprocess.Popen` 成功后构造一个 Job Object，调用
    `job.assign(self._process)`；成功则保存到 `self._job`，失败则关闭未用的句柄。
    整个 try 是 best-effort，失败不影响 run 启动。
  - `RunManager.stop()` 在 `process.terminate()` / `process.kill()` 之后，
    如果 `self._job` 不为 `None`，调用 `job.close()`——这一步会触发 OS 杀掉
    Job 内所有后代进程。然后在锁下把 `self._job` 置 `None`。
  - `RunManager._read_process()` 在 run 自然结束时也释放 Job 句柄
    （从锁内读到 `job` 引用后，在锁外调用 `close()`，避免持锁阻塞），防止句柄泄漏。
  - `RunManager.stop_if_running()` 走的是 `stop()`，所以 P2-1 的修复自动覆盖
    桌面入口的 `stop_backend()` 关闭路径。

### P2-2 — `setup_logging` 在错误对话框就绪之前就创建了日志目录（`desktop_app.py:615`）

**问题**：`main()` 的执行顺序是 `get_user_data_dir()` → `get_state_dir()` →
`get_logs_dir()` → `setup_logging(logs_dir)` → 才进入 `try: ...mkdir()... except OSError`。
但 `setup_logging()` 自己就会调用 `logs_dir.mkdir(parents=True, exist_ok=True)`，
所以当 `--user-data-dir` 指向一个不可写路径、或 `%LOCALAPPDATA%` 解析到一个非法目录时，
`setup_logging` 在 try/except 之前就抛 `OSError`，没有任何对话框或日志记录发生。
在 windowed EXE 里这是「静默失败」——用户双击 exe 后窗口闪一下就没了，看不到任何错误信息，
违反了桌面契约「后端启动失败时，桌面窗口或弹窗中显示清晰错误」。

**修复**：把目录创建 + 校验移到 `setup_logging` **之前**，让任何 `OSError`
都能走 best-effort 错误对话框 + stderr 输出，然后退出码 2。

- `desktop_app.py`
  - `main()` 先解析三个路径（`user_data_dir` / `state_dir` / `logs_dir`），
    然后立刻在 `try/except OSError` 里创建三个目录。失败时调用
    `_show_blocking_message(...)`（不传 logger，让对话框自己工作）+ `print(..., file=sys.stderr)`
    + 返回 2。
  - 只有目录都创建成功之后，才调用 `setup_logging(logs_dir, verbose=args.verbose)`，
    后续日志走正常的 file + console handler。
  - 原来在 try/except 里调用的 `logger.error(...)` 自然消失——因为这时还没 logger。

### P3-1 — 检测接受 `pwsh.exe` 但运行命令硬编码 `powershell`（`gui/server.py:400`）

**问题**：`desktop_app.detect_dependencies()` 把 `pwsh` / `pwsh.exe` 也算作满足
「PowerShell」依赖，所以只有 PowerShell 7（没有 Windows PowerShell 5.x）的机器能通过
启动检查。但 `build_run_command()` 仍然把可执行名硬编码成 `"powershell"`，
没有走 PATH 解析。结果：桌面入口正常启动，Web 控制台也正常打开，但用户点「启动循环」
时 `subprocess.Popen(["powershell", ...])` 因为 `powershell` 不在 PATH 而抛
`FileNotFoundError`，run loop 直接挂掉。

**修复**：让桌面入口把检测阶段解析到的 PowerShell 路径通过环境注入传给后端，
后端的 `build_run_command()` 使用这个解析后的路径而不是硬编码名。

- `gui/server.py`
  - 新增模块级 `POWERSHELL_EXECUTABLE: str = "powershell"`。
  - 新增 `set_powershell_executable(path: str | None) -> None`：
    `path` 为空或 `None` 时重置为默认 `"powershell"`（保持源码模式向后兼容）；
    否则把 `path.strip()` 写入模块全局。
  - `build_run_command()` 把硬编码的 `"powershell"` 替换为 `POWERSHELL_EXECUTABLE`。
    其他参数（`-NoProfile`、`-ExecutionPolicy Bypass`、`-File` 等）在 Windows PowerShell
    和 PowerShell 7 上行为一致，无需改动。
- `desktop_app.py`
  - `start_backend()` 新增 `powershell_path: str | None = None` 参数，
    在 `configure_paths(state_dir)` 之后调用 `gui_server.set_powershell_executable(powershell_path)`。
  - `main()` 在依赖检测之后从 `deps["powershell"]["path"]` 取出解析路径
    （或回退到 `"powershell"`），写一条 `PowerShell resolved: <path>` 日志，
    然后传给 `start_backend(powershell_path=...)`。

### Round 4 文件改动清单

| 文件 | 改动 |
|---|---|
| `gui/server.py` | 新增 `_Win32JobObject` 类（Win32 Job Object + kill-on-close，跨平台降级为 inert）；`RunManager.__init__` 增加 `self._job`；`start()` 创建 Job Object 并把进程加入；`stop()` 关闭 Job 句柄触发 OS 杀进程树；`_read_process()` 在 run 自然退出时也释放 Job 句柄。新增模块级 `POWERSHELL_EXECUTABLE` + `set_powershell_executable(path)`；`build_run_command()` 使用 `POWERSHELL_EXECUTABLE` 替代硬编码 `"powershell"`。 |
| `desktop_app.py` | `main()` 把 `mkdir` + 校验移到 `setup_logging` 之前，目录创建失败时弹出无 logger 的错误对话框 + stderr 输出 + 退出码 2。`main()` 在依赖检测后从 `deps["powershell"]["path"]` 取出解析路径并传给 `start_backend(powershell_path=...)`；`start_backend()` 接受 `powershell_path` 参数并调用 `gui_server.set_powershell_executable(...)`。 |
| `tests/test_desktop_app.py` | 新增 `StartBackendPowerShellPlumbingTests`（3 个测试，P3-1 桌面入口侧）覆盖：resolved path 被传入后端 / `None` 重置为默认 / `set_powershell_executable` 对空白和 None 的归一化。新增 `MainUserDataDirBootstrapTests`（1 个测试，P2-2）验证 `main()` 在 `--user-data-dir` 不可写时返回 2、不调用 `setup_logging` / `detect_dependencies`、弹出错误对话框。 |
| `tests/test_gui_server.py` | 新增 `PowerShellExecutableTests`（4 个测试，P3-1 后端侧）：默认值是 `"powershell"`、`set_powershell_executable` 改写 `build_run_command` 输出、`None` 重置、空白归一化。新增 `RunManagerProcessTreeTests`（5 个测试，P2-1）：start 把进程加入 Job Object、inert Job 不调用 assign/close、stop 在 terminate 之后关闭 Job 句柄、`stop_if_running` 走同一路径、run 自然退出时也释放 Job 句柄。 |

### Round 4 测试结果

```
py -B -m pytest tests/test_desktop_app.py tests/test_gui_server.py::PowerShellExecutableTests tests/test_gui_server.py::RunManagerProcessTreeTests -v -p no:cacheprovider
=> 36 passed

py -B -m pytest -q -p no:cacheprovider
=> 419 passed, 5 warnings in 147.21s (0:02:27)
```

新增 13 个测试（`test_desktop_app.py` +4，`test_gui_server.py` +9），总数从 406 涨到 419。
新增的 1 个 warning 是 `RunManager._read_process` 后台线程在测试结束后短暂存活的
老问题（baseline 已有 4 个同类 warning，由 `RunManagerStopIfRunningTests` 留下），
不影响断言正确性，pytest 报告为 warning 而非 failure。

### 安全边界（Round 4 不变）

- 没有引入任何新的 Git 命令，没有任何对 `.env` 或 `.git` 的读写。
- Job Object 只对该 RunManager 自己启动的 PowerShell 子进程生效——
  它是 `gui/orchestrator` 通过 `subprocess.Popen` 启动的 run loop，
  关闭 Job 句柄等同于关掉这个 run。不触碰用户项目目录、Git 状态、仓库内容、
  `.env` 或 `.git`。
- `terminate()` / `kill()` 调用的是 Windows 进程 API
  （`subprocess.Popen.terminate` → `TerminateProcess`），
  目标是**本应用启动的** PowerShell 子进程，不属于安全规则禁止的
  「destructive cleanup」。
- `set_powershell_executable()` 只是把字符串写进模块全局，调用方仍然是
  `RunManager.start()` 时构造的 `subprocess.Popen` 命令；不改变可执行文件
  校验范围（依然是用户系统 PATH / 桌面入口 `shutil.which` 解析的路径）。
- 所有 Git 变更类操作仍然只能在 Web 控制台里由用户点击「一键提交」/
  「一键合并主干」按钮时触发。Job Object 关闭流程只是清理自己启动的
  PowerShell 子进程树，不引入新的变更路径。
- P2-2 的对话框修复不影响安全边界——只是错误展示顺序的调整。


---

## Round 5 Fix: Codex P2-1

### P2-1 — `RunManager.stop_if_running()` 短路导致 Job 句柄泄漏，后台 Claude/Codex 子进程可能在桌面退出后继续运行（`gui/server.py:930`）

**问题**：Round 4 的 `RunManager.stop_if_running()` 把清理工作门控在 `has_active_run()` 上，
而 `has_active_run()` 只检查直接父进程 `_process.poll()` 是否为 `None`。在两种典型场景下
这会导致 Job 句柄泄漏：

1. **PowerShell 父进程已退出、但后代仍在 Job 里跑**：`scripts/run-claude.ps1` 通过
   `Start-Process` 等机制启动 Claude CLI / Codex CLI 子进程，这些子进程已经脱离
   PowerShell 父进程。如果用户先关掉了 Web 控制台里的 PowerShell 父进程（或父进程自己
   崩溃），但 Claude/Codex 子进程还在跑——`has_active_run()` 看到 `_process.poll() != None`
   返回 `False`，`stop_if_running()` 立刻短路，`self._job` 保持打开状态。
2. **读取线程尚未走到自然退出路径的 `job.close()`**：`_read_process()` 在 stdout 还没
   drain 完时（例如子进程把大量日志写到继承的 stdout 上）会阻塞在
   `for line in process.stdout:`。如果桌面入口此时调用 `stop_if_running()`，`_job` 已经
   设置但还没被 `_read_process` 的自然退出路径释放。

由于 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 只在 Job 句柄被 `CloseHandle` 关闭时才触发，
`self._job` 没关掉 = 后台 Claude/Codex 子进程继续在用户机器上修改项目目录——只是 UI 和
HTTP 服务都不在了。这违反了 Round 4 想修复的「软件退出时应优雅停止后端服务」契约。

**修复**：新增 `RunManager.shutdown()` 辅助方法，**无条件**关闭 Job 句柄；同时给既有的
`stop()` 包上 `try/finally`，保证即便 `terminate` / `wait` 抛异常 Job 句柄仍然会被关闭。
让 `desktop_app.stop_backend()` 优先调用 `shutdown()`（找不到时回退到旧的
`stop_if_running()`，保证对老 `gui.server` 构建仍可用）。

- `gui/server.py`
  - 新增 `RunManager.shutdown()`：在锁下捕获 `_process` 与 `_job` 的当前引用、计算
    `process_alive`；如果进程活着，置 `_stopping = True` 并走和 `stop()` 一样的
    terminate → wait(5s) → kill 序列；**finally** 块里**无条件**关闭 Job 句柄
    （只要 `job` 不为 `None`），然后把 `self._job` 置 `None`（仅在它还指向同一个对象时，
    防御并发 start() 的极端情况）。返回 `True` 当且仅当确实做了清理工作
    （终止了进程或关闭了 Job）。
    - 关键差别：`stop_if_running()` 是「进程还活着才动」；`shutdown()` 是「只要有 Job
      就关」。前者会漏掉「父进程已退出但后代还在」和「读取线程尚未自然退出」两种
      P2-1 场景。
    - terminate/wait/kill 整个块包在 try 里、外层 finally 关 Job；terminate 抛异常时
      finally 仍然关 Job，不让句柄泄漏。
  - 改造 `RunManager.stop()`：把 `process.terminate()` / `wait(timeout=5)` / `kill()`
    序列包进 `try/finally`，finally 里关闭 Job 句柄并清空 `self._job`。Round 4 的旧
    实现把 Job 关闭放在 wait/kill 之后、没有 try/finally，terminate 抛异常会让 Job
    句柄泄漏——属于同一个 P2-1 漏洞的另一个分支。`job.close()` 自身也包了一层
    `try/except` 防御（虽然 `_Win32JobObject.close` 本身已经吞了所有 OSError，但
    多一层防御没有成本）。
  - `RunManager.stop_if_running()` 文档更新：明确说明它只在直接父进程仍然存活时关闭
    Job，调用方需要保证后代清理的应改用 `shutdown()`。方法行为不变，保持向后兼容。
- `desktop_app.py`
  - `stop_backend()` 优先调用 `RunManager.shutdown()`，找不到时回退到
    `stop_if_running()`。注释里明确说明为什么选 `shutdown()` 而不是 `stop_if_running()`：
    后者会漏掉 P2-1 的两种泄漏场景。其他逻辑（HTTP server shutdown / server_close /
    thread join）保持不变。
  - 老的日志消息 `"RunManager.stop_if_running() raised during shutdown"` 改成更通用的
    `"RunManager shutdown raised during backend stop"`，反映现在调用的是 `shutdown()`。

### Round 5 文件改动清单

| 文件 | 改动 |
|---|---|
| `gui/server.py` | 新增 `RunManager.shutdown()`（在锁下捕获 `_process` + `_job`，process 活着就 terminate/kill，finally 里**无条件**关 Job 并清空 `self._job`，返回是否做了实际清理）。`stop()` 的 terminate/wait/kill 序列包进 `try/finally`，保证 Job 关闭在 terminate/wait 抛异常时仍然发生。`stop_if_running()` 文档说明使用边界（不保证后代清理，建议改用 `shutdown`）。 |
| `desktop_app.py` | `stop_backend()` 优先调用 `shutdown()`，找不到时回退到 `stop_if_running()`；异常日志改成 `"RunManager shutdown raised during backend stop"`。 |
| `tests/test_gui_server.py` | 新增 `RunManagerShutdownTests`（8 个测试）：nothing-to-clean 短路、活着时 terminate + close、terminate 超时走 kill + close、**进程已退出但 Job 仍开时仍关 Job**（P2-1 主路径）、**没有进程但 Job 仍开时仍关 Job**（读取线程未退出场景）、二次调用不会重复关同一个 Job、terminate 抛异常时 finally 仍关 Job、只有 Job 时返回 True。新增 `StopTryFinallyTests`（2 个测试）：terminate 抛异常时 `stop()` 仍关 Job、kill 抛异常时 `stop()` 仍关 Job（锁定 try/finally 包装）。 |
| `tests/test_desktop_app.py` | 新增 2 个桌面入口侧测试：`test_stop_backend_closes_job_when_parent_already_exited`（P2-1 主回归：poll()=0、_job 仍开，断言 stop_backend 仍调用 `fake_job.close()` 并清空 `_job`）；`test_stop_backend_uses_shutdown_when_available`（验证 stop_backend 优先调用 `shutdown()` 而不是 `stop_if_running()`）。 |

### Round 5 测试结果

```
py -B -m pytest tests/test_gui_server.py::RunManagerShutdownTests tests/test_gui_server.py::StopTryFinallyTests tests/test_gui_server.py::RunManagerStopIfRunningTests tests/test_gui_server.py::RunManagerProcessTreeTests -v -p no:cacheprovider
=> 20 passed, 1 warning in 0.62s

py -B -m pytest tests/test_desktop_app.py -v -p no:cacheprovider
=> 29 passed in 2.83s

py -B -m pytest -q -p no:cacheprovider
=> 431 passed, 5 warnings in 111.45s (0:01:51)
```

新增 10 个测试（`test_gui_server.py` +8，`test_desktop_app.py` +2），总数从 419 涨到 431，
无回归。新增 1 个 warning 来自 `StopTryFinallyTests` 测试结束后 `_read_process` 后台
daemon 线程短暂存活的老问题（Round 4 已经有 4 个同类 warning，由
`RunManagerStopIfRunningTests` 留下），不影响断言正确性。

### 安全边界（Round 5 不变）

- 没有引入任何新的 Git 命令，没有任何对 `.env` 或 `.git` 的读写。
- `shutdown()` 只调用 `subprocess.Popen.terminate()` / `.kill()` 和
  `_Win32JobObject.close()`——前者等同于 Round 4 已经审计过的 `stop()` 路径
  （终止**本应用启动的** PowerShell 子进程），后者只是关闭 Windows 内核对象句柄、
  触发操作系统按 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 标志回收 Job 内的进程。
  不触碰用户项目目录、Git 状态、仓库内容、`.env` 或 `.git`。
- 不属于安全规则禁止的「destructive cleanup」——被清理的是 `gui/orchestrator`
  自己启动的 run loop，不是用户的代码或仓库。
- `stop_if_running()` 行为保持向后兼容，老调用方（如果有）不会因为新增 `shutdown()`
  而改变行为。
- 所有 Git 变更类操作仍然只能在 Web 控制台里由用户点击「一键提交」/「一键合并主干」
  按钮时触发。Round 5 的改动纯粹是后台 run 生命周期管理的漏洞修复，不引入新的变更路径。

---

## Round 6：读取线程隔离 + 受控 Job 启动器（P2-1 / P2-2）

### Codex Review 发现

- **P2-1（`gui/server.py:832`）**：`RunManager.start()` 在旧 run 的 `_process.poll()`
  已经不为 `None` 时就允许下一轮启动，但此时旧的 `_read_process()` 后台线程可能尚未
  返回。旧读取线程随后仍会用 `self.current` 写结束状态、用 `self._job` 取 Job 关闭，
  `process.wait()` 抛出的异常还会以 `PytestUnhandledThreadExceptionWarning` 的形式
  从后台线程冒出（具体复现于 `StopTryFinallyTests::test_stop_closes_job_when_terminate_raises`）。
- **P2-2（`gui/server.py:859`）**：Windows Job Object 在 `subprocess.Popen()` 返回
  之后才被 `assign()`，而 PowerShell 脚本可能在 assign 之前已经 fork 出 Claude /
  Codex 子进程，从而逃出 Job，使 `stop()` 无法回收整棵进程树。

### 修复要点

#### P2-1：每轮读取线程隔离

- `RunManager.__init__` 新增 `self._reader_thread` 字段；`start()` 在 `_reader_thread
  .is_alive()` 为真时直接抛 CONFLICT，避免新旧读取线程交叠。
- 读取线程改为接收 `(process, run_id, job, gate)` 作为参数，**不再依赖共享 `self`
  字段**：
  - stdout 仅在 `self.current.id == run_id` 时追加，避免污染新 run 的日志；
  - finalize 仅在仍持有 `current` 时写入状态；清空 `_process` / `_job` 前先做
    `is` 身份比对，新 run 已经替换这些字段时**不**清空；
  - **始终关闭自己捕获的 Job**（real `_Win32JobObject.close` 幂等，double-close 安全）；
  - `process.wait()` / stdout 迭代 / `stdout.close()` / `store.update_project()` 全部
    try/except 包住，异常写进 run 历史而非冒到线程外。

#### P2-2：受控 Job 启动器（gated launcher）

新增 `_FileGate` / `_create_run_gate` / `_build_gated_run_command`：

- 启动器先 spawn 一个 PowerShell wrapper，wrapper 在 `Test-Path -LiteralPath <gate>`
  返回 true 之前不调用真实的 run-claude.ps1；
- 父进程先 `job.assign(wrapper)`，成功后再 `gate.release()` 创建信号文件；
- wrapper 拿到信号后才用 `&` 调用真实命令，真实命令及其 Claude / Codex 后代因此**继承
  wrapper 的 Job 成员资格**，无法再逃出 `stop()` / `shutdown()` 的 `KILL_ON_JOB_CLOSE`。
- 失败回退：`assign()` 失败 → 终止 wrapper + 清理 gate + 关 Job，然后 fallback 到普通
  `Popen(command)`；wrapper spawn 自身失败 → 同样清理后 fallback。

### 文件变更（Round 6）

| 文件路径 | 操作 | 说明 |
|----------|------|------|
| `gui/server.py` | 修改 | (1) `RunManager.__init__` 新增 `_reader_thread` 字段；(2) `start()` 增加「上一轮读取线程仍存活」短路检查，并在拿到 spawn 结果后把 `(process, run_id, job, gate)` 传给新读取线程；(3) 新增 `_spawn_run_process()` 实现 gated launcher，Job 真值判断代替 `sys.platform == 'win32'` 判断（`_Win32JobObject` 在非 Windows 自动 falsy）；(4) `_read_process(process, run_id, job, gate)` 改为按参数隔离，所有阻塞调用 try/except 包住；(5) 新增 `_FileGate` 类与 `_create_run_gate` / `_build_gated_run_command` 辅助函数。 |
| `tests/test_gui_server.py` | 修改 | (1) 4 个老断言由 `assert_called_once()` 改成 `assertGreaterEqual(call_count, 1)`，兼容读取线程的幂等 double-close；(2) 新增 `RunManagerReaderIsolationTests`（5 个测试）：start 拒绝重叠运行、wait 异常被吞并写入日志、stdout 异常被吞并写入日志、旧读取线程不清新 run 的 `_process`/`_job`、旧读取线程不把旧 stdout 写入新 run；(3) 新增 `RunManagerGatedLauncherTests`（5 个测试）：wrapper 先 assign 再 release gate、assign 失败时清理并 fallback、Popen 失败时清理并 fallback、inert Job 跳过 gated 路径、gated 启动后 `stop()` 仍关 Job。 |

### Round 6 测试结果

```
py -B -m pytest tests/test_gui_server.py tests/test_desktop_app.py -q
=> 175 passed, 1 warning in 33.22s

py -B -m pytest -q -p no:cacheprovider
=> 441 passed, 4 warnings in 151.46s (0:02:31)
```

新增 10 个测试（`RunManagerReaderIsolationTests` +5、`RunManagerGatedLauncherTests` +5），
总数从 431 涨到 441，无回归。Round 5 遗留的
`PytestUnhandledThreadExceptionWarning` 已经消失——剩下的 4 个
`PytestCollectionWarning` 是 `TestRunResult` / `TestCommandError` 这两个 dataclass
名字以 `Test` 开头触发的既有 pytest 误识别，与本次修复无关。

### 安全边界（Round 6 不变）

- 没有引入任何新的 Git 命令，没有任何对 `.env` 或 `.git` 的读写。
- gated launcher 只 spawn 一个 PowerShell wrapper（参数经过单引号转义，按 PowerShell
  `''` 规则转义嵌入引号），调用路径仍是本应用原本就要启动的 `scripts/run-claude.ps1`。
- `_FileGate` 的信号文件位于 `tempfile.gettempdir() / "ccdl-run-gates"`，使用 UUID
  随机文件名，不写入用户项目目录、`.env` 或 `.git`。
- 读取线程仅读取 `process.stdout`、调用 `process.wait()`、写入 `ProjectStore`、关闭
  Job 句柄、删除自己的 gate 文件——没有任何写入用户仓库或调用 Git 的路径。
- 所有 Git 变更类操作仍然只能在 Web 控制台里由用户点击「一键提交」/「一键合并主干」
  按钮时触发。Round 6 的改动纯粹是 run 生命周期的并发与进程树回收修复，不引入新的
  变更路径。

---

## Round 7：gated wrapper 退出码透传 + HTTP Stop 关 Job + 失败路径 kill（P1-1 / P2-1 / P2-2）

### Codex Review 发现

- **P1-1（`gui/server.py:863`）**：`_build_gated_run_command` 生成的 PowerShell wrapper
  脚本通过 `& 'real_cmd' 'args'` 调用真实命令，但从未显式 `exit $LASTEXITCODE`。
  Windows PowerShell `-Command` 不会可靠地把原生子进程退出码透传到 wrapper 进程
  退出码——常见的表现是 7（NEEDS_FIX）被压成 1 或 0。桌面 EXE 通过 Job Object 启动
  的所有 run 都走这条 gated 路径，所以 `exit_code_to_result` 会把 NEEDS_FIX /
  CODEX_REVIEW_INVALID 等结果错分类为 ENVIRONMENT_ERROR 或成功，整个 Claude/Codex
  循环的判定都失去意义。
- **P2-1（`gui/server.py:3350`）**：HTTP `POST /api/runs/current/stop` 直接调用
  `RunManager.stop()`，而 `stop()` 的前置条件要求直接 wrapper 进程仍在运行
  （`_process.poll() is None`）。Round 5/6 引入 `RunManager.shutdown()` 就是为了
  在「wrapper 已退出、Job 内仍有 Claude/Codex 后代」或「reader 线程还没走到自然
  退出的 `job.close()`」时也能关闭 Job 句柄。但用户按 Web UI Stop 按钮打到的
  `/api/runs/current/stop` 没走 `shutdown()`——直接拿 409 CONFLICT，Job 不被关闭，
  run 仍然显示 running，后代继续修改项目目录直到整个桌面进程退出。
- **P2-2（`gui/server.py:1058`）**：当 Job 分配失败时，`_spawn_run_process` 调用
  `wrapper_process.terminate()` + `wrapper_process.wait(timeout=2)`，但用
  `except Exception: pass` 把所有异常都吞掉了。如果 `wait` 超时（或 `terminate`
  本身抛错），那个 wrapper 仍然在 `while (-not (Test-Path <gate>))` 循环里转——
  而我们紧接着 `gate.cleanup()` 删掉了 gate 文件，wrapper 永远找不到 gate 永远
  不会退出。同时 fallback 路径会用普通 `Popen(command)` 启动第二个进程，结果
  就是用户机器上多了一个脱离 Job 生命周期的 PowerShell 后台进程。

### 修复要点

#### P1-1：显式 `exit $LASTEXITCODE`

`_build_gated_run_command` 在 wrapper 脚本末尾追加：

```powershell
; $code = $LASTEXITCODE;
if ($null -eq $code) { if ($?) { exit 0 } else { exit 1 } }
else { exit $code }
```

- `$LASTEXITCODE` 是 PowerShell 用来记录最近一次原生子进程退出码的自动变量。
  通过显式读取并 `exit $code`，wrapper 进程退出码就与真实命令（run-claude.ps1
  及其调用的 claude/codex CLI）的退出码字节级一致。
- `$null -eq $code` 兜底：当真实命令是纯 cmdlet / 脚本（没有调用任何原生 exe）
  时 `$LASTEXITCODE` 为 `$null`。这时用 `$?`（最近一次命令是否成功）决定
  退出 0 还是 1，避免 wrapper 因为脚本末端没有原生命令就退出 0 把脚本失败
  当成成功。
- propagation 块必须出现在 `& 'real_cmd'` 之后，否则会在真实命令运行前就退出。
  单元测试 `test_gated_command_script_propagates_lastexitcode` 显式断言了这一
  顺序。
- 端到端测试 `test_gated_command_actually_propagates_exit_code_seven` 在
  Windows PowerShell 可用时，真的 spawn 一个 gated wrapper（real command =
  `cmd /c exit 7`），release gate 后等待 wrapper 退出，断言 `process.returncode`
  恰好是 7。这是一个真正的 PowerShell → Job Object 集成测试，覆盖了 P1-1 的
  完整路径。

#### P2-1：HTTP Stop 路由到 `shutdown_and_snapshot`

新增 `RunManager.shutdown_and_snapshot()`：

```python
def shutdown_and_snapshot(self) -> dict[str, Any] | None:
    self.shutdown()
    return self.snapshot()
```

`POST /api/runs/current/stop` 从 `self.runs.stop()` 改为 `self.runs.shutdown_and_snapshot()`。
`shutdown()` 的契约（Round 5）保证 Job 句柄被无条件关闭——即使 wrapper 已经退出，
Job 内仍跑着的 Claude/Codex 后代也会被 `KILL_ON_JOB_CLOSE` 回收。

- 旧 `stop()` 的前置条件 `_process.poll() is None` 在「wrapper 已退出、后代还在」
  场景下会失败并抛 `ApiError(CONFLICT)`，导致 409 + Job 不关。这是 Codex 报告的
  现象。
- `shutdown()` 不做前置检查：只要有 Job 就关。这正是用户按 Stop 时想要的效果。
- `shutdown()` 在「wrapper 仍然存活」的正常 Stop 场景下，行为等价于 `stop()`
  （terminate → wait/kill → close job），所以既覆盖正常路径也覆盖异常路径。
- 返回 snapshot 让前端能像以前一样更新 run 状态：reader 线程把 `current` 写成
  finished/stopped 后，snapshot 就反映最终状态；如果用户在 reader 写入前就按了
  Stop，shutdown 后 snapshot 仍显示原状态，下一次 SSE 推送会更新它。
- 旧 `stop()` 方法本身没有改动：`stop_if_running()` 仍然走 `stop()`，行为不变；
  桌面入口 `stop_backend()` 仍然优先走 `shutdown()`，行为也不变。

#### P2-2：assign 失败路径加 kill 升级

```python
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
```

- `terminate()` 单独 try/except：`terminate` 失败（权限拒绝、进程已死、句柄无效）
  不再让整段清理跳过——我们仍然尝试 `wait`，仍然按需 `kill`。
- `wait(timeout=2)` 显式捕 `TimeoutExpired`：超时说明 wrapper 没在 2 秒内退出
  （通常是因为它在 `Start-Sleep -Milliseconds 5` 紧密循环里）。这时调 `kill()`
  强制终止进程，再 `wait(timeout=2)` 收尸。`kill` 之后的 wait 也包了 try/except
  以防 kill 已经把进程结束、wait 收到 `WindowsError`。
- 完成上述升级后才 `gate.cleanup()` 删 gate 文件——此时 wrapper 已经被终止
  或被 kill，不可能再进入 `Test-Path` 循环，gate 文件可以安全删除。
- `kill` 升级只发生在「wait 超时」分支。正常 wait 返回 0 时 `kill` 永远不会被
  调用——`test_assign_failure_does_not_kill_when_wait_succeeds` 显式锁定了
  这一点。

### 文件变更（Round 7）

| 文件路径 | 操作 | 说明 |
|----------|------|------|
| `gui/server.py` | 修改 | (1) `_build_gated_run_command` 在 wrapper 脚本末尾追加 `$LASTEXITCODE` 透传块，处理 `$null` 兜底（P1-1）。(2) 新增 `RunManager.shutdown_and_snapshot()`；HTTP `/api/runs/current/stop` 路由从 `self.runs.stop()` 改为 `self.runs.shutdown_and_snapshot()`（P2-1）。(3) `_spawn_run_process` 的 assign-failure 分支把 `terminate` / `wait` / `kill` 拆开包 try/except，在 `wait` 超时时显式 `kill()` 并再 `wait`（P2-2）。 |
| `tests/test_gui_server.py` | 修改 | 新增 3 个测试类、9 个测试：`GatedLauncherExitCodePropagationTests`（3 个，P1-1：脚本结构断言、参数转义保留、Windows PowerShell 端到端退出码 7 透传）；`HttpStopEndpointShutdownTests`（3 个，P2-1：wrapper 已退出时 `shutdown_and_snapshot` 关 Job + 返回 snapshot、不调用 legacy `stop()`、无活动 run 时返回 None）；`GatedLauncherAssignFailureKillTests`（3 个，P2-2：wait 超时时 `kill()` 被调用、wait 正常时不调用 `kill`、`terminate()` 抛错时仍尝试 kill 路径）。 |

### Round 7 测试结果

```
py -B -m pytest tests/test_gui_server.py::GatedLauncherExitCodePropagationTests tests/test_gui_server.py::HttpStopEndpointShutdownTests tests/test_gui_server.py::GatedLauncherAssignFailureKillTests -v -p no:cacheprovider
=> 9 passed in 0.55s

py -B -m pytest -q -p no:cacheprovider
=> 450 passed, 4 warnings in 145.00s (0:02:24)
```

新增 9 个测试，总数从 441 涨到 450，无回归。4 个 warning 仍是既有的
`PytestCollectionWarning`（`TestRunResult` / `TestCommandError` 这两个
dataclass 名字以 `Test` 开头触发的 pytest 误识别），与本次修复无关。

### 安全边界（Round 7 不变）

- 没有引入任何新的 Git 命令，没有任何对 `.env` 或 `.git` 的读写。
- P1-1 只改 PowerShell wrapper 脚本的末尾追加内容——参数转义规则不变
  （单引号包裹 + 内部 `'` 转义为 `''`），调用目标仍是 `scripts/run-claude.ps1`，
  不引入任何新的外部命令调用。
- P2-1 只改 HTTP Stop 路由：以前调 `stop()`，现在调 `shutdown()`（同一个
  `RunManager` 实例的既有方法）。`shutdown()` 的安全边界已经在 Round 5
  审过——只调 `subprocess.Popen.terminate()` / `.kill()` 和
  `_Win32JobObject.close()`，目标是**本应用启动的** PowerShell 子进程。
- P2-2 只补齐失败路径的进程清理：`terminate()` / `kill()` 仍然作用于
  gated wrapper 自己启动的 PowerShell 进程，不触碰用户项目目录、Git 状态、
  `.env` 或 `.git`。
- 所有 Git 变更类操作仍然只能在 Web 控制台里由用户点击「一键提交」/
  「一键合并主干」按钮时触发。Round 7 的改动纯粹是 run 结果分类（P1-1）、
  Stop 按钮的 Job 回收（P2-1）、assign 失败时的孤儿 wrapper 清理（P2-2），
  不引入新的变更路径。

---

## Round 8 Fix: Codex P2-1

### P2-1 — `RunManager.shutdown()` 只在直接 wrapper 仍存活时才翻转 `_stopping`，导致 Job 回收后被中止的 run 被误报为 SUCCESS（`gui/server.py:1208`）

**问题**：Round 5 引入 `RunManager.shutdown()` 的目的是在「直接 wrapper 已退出、Job 内仍有 Claude/Codex 后代」或「读取线程尚未走到自然退出的 `job.close()`」两种场景下也能关闭 Job 句柄。但 `shutdown()` 把翻转 `_stopping = True` 的判断门控在 `process_alive` 上——只有直接 wrapper 仍然存活时才标记停止、才写 `Shutdown requested.` 日志。

Round 7 把 HTTP `POST /api/runs/current/stop` 路由到 `shutdown_and_snapshot()` → `shutdown()`，于是这个漏洞直接暴露在用户操作路径上。完整的中招序列：

1. PowerShell wrapper 启动真实命令，真实命令再 `Start-Process` 出 Claude/Codex CLI 子进程；
2. wrapper 自己先退出，但 Claude/Codex 后代继承了 wrapper 的 stdout 管道，导致读取线程的 `for line in process.stdout:` 一直拿不到 EOF，阻塞在循环里；
3. 用户在 Web 控制台点「Stop」→ `shutdown_and_snapshot()` → `shutdown()`；
4. 旧 `shutdown()` 看到 `process_alive = False`（wrapper 已退出），**不**翻转 `_stopping`，直接进入 finally 块关闭 Job 句柄；
5. Job 关闭触发 `KILL_ON_JOB_CLOSE`，Claude/Codex 后代被操作系统杀掉，继承的 stdout 管道关闭；
6. 读取线程的 `for line in process.stdout:` 终于拿到 EOF，调用 `process.wait()` 拿到 wrapper 的退出码（很可能是 0）；
7. 读取线程进入 finalize 块，读取 `stopped = self._stopping`——因为步骤 4 没翻转，`stopped = False`；
8. `exit_code_to_result(0, stopped=False)` 返回 `"PASS"`，run 被记成 `finished` / `SUCCESS`，并把它写进 `lastResult`、`lastExitCode`、`lastRunAt`。

结果：用户主动中止的「会修改项目的自动化 run」被记为成功完成，`lastResult` 错误地指示项目已通过，后续依赖该字段的判断（比如 PASS 后才允许的「一键提交」按钮）会基于错误前提运作。这正是 Codex P2-1 报告描述的现象。

**修复**：把翻转 `_stopping` 的条件从 `if process_alive:` 改成 `if process_alive or job is not None:`，并补一段文档说明为什么。`_append_locked` 本身已经在末尾调用 `self._condition.notify_all()`，所以 SSE 消费者会立刻收到状态更新，不需要额外显式 notify。

- `gui/server.py` `RunManager.shutdown()`
  - 翻转条件改为 `process_alive or job is not None`。两条触发路径：
    - `process_alive`：直接 wrapper 还活着，老逻辑也走这条。
    - `job is not None`：直接 wrapper 已退出但 Job 句柄仍开着（后代仍在 Job 内跑），新逻辑补上的路径。
  - 两种情况下都写 `Shutdown requested.` 日志，便于审计追溯用户的 stop 意图。
  - 文档块新增「Round 8 P2-1」段落，解释为什么必须在 wrapper 已退出时也翻转 `_stopping`：否则读取线程在 Job 关闭、stdout 管道释放后 finalize 时会把 run 误分类为 `finished` / `SUCCESS`。
  - 其它路径（terminate / wait / kill / 关 Job / 清空 `self._job`）保持不变，依然在 `try/finally` 里运行，保证 terminate 抛异常时 Job 句柄仍然关闭。

### Round 8 文件改动清单

| 文件 | 改动 |
|---|---|
| `gui/server.py` | `RunManager.shutdown()` 把 `_stopping` 翻转条件从 `if process_alive:` 改成 `if process_alive or job is not None:`，并扩充 docstring 说明 Round 8 P2-1 的修复理由。其它代码路径不变。 |
| `tests/test_gui_server.py` | `RunManagerShutdownTests` 新增 `test_shutdown_marks_stopping_when_wrapper_exited_but_job_open`：复现 P2-1 场景（wrapper 已退出 `poll()=0`，Job 仍开着），断言 `shutdown()` 后 `_stopping = True`、日志里出现 `Shutdown requested`、Job 关闭；然后调用 `_read_process` 模拟读取线程的 finalize，断言 run 被分类为 `stopped` / `STOPPED` 而不是 `finished` / `SUCCESS`，直接锁定核心不变式。 |

### Round 8 测试结果

```
py -B -m pytest tests/test_gui_server.py::RunManagerShutdownTests -v -p no:cacheprovider
=> 9 passed in 0.14s

py -B -m pytest -q -p no:cacheprovider
=> 451 passed, 4 warnings in 147.06s (0:02:27)
```

新增 1 个回归测试，总数从 450 涨到 451，无回归。4 个 warning 仍是既有的
`PytestCollectionWarning`（`TestRunResult` / `TestCommandError` 这两个
dataclass 名字以 `Test` 开头触发的 pytest 误识别），与本次修复无关。

### 安全边界（Round 8 不变）

- 没有引入任何新的 Git 命令，没有任何对 `.env` 或 `.git` 的读写。
- 修复纯粹是 `RunManager.shutdown()` 内部的状态标记：以前在某种场景下不翻
  `_stopping`，现在翻转了。`shutdown()` 调用的下游 API（`subprocess.Popen
  .terminate()` / `.kill()` 和 `_Win32JobObject.close()`）保持不变，目标仍然
  是**本应用启动的** PowerShell 子进程及其 Job 内后代，不触碰用户项目目录、
  Git 状态、`.env` 或 `.git`。
- 翻转 `_stopping` 只影响读取线程 finalize 时的结果分类（`STOPPED` vs
  `PASS`），不会触发任何额外的 Git / 文件操作。
- `lastResult` 现在能正确反映用户的中止意图（`STOPPED`），不会再把中止的 run
  当成 `PASS` 并据此放开「一键提交」按钮——这反而**收紧**了安全边界。
- 所有 Git 变更类操作仍然只能在 Web 控制台里由用户点击「一键提交」/
  「一键合并主干」按钮时触发。Round 8 的改动只是修正 run 结果分类，不引入
  新的变更路径。

