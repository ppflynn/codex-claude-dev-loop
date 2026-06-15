当前项目已经具备：
Claude/Codex 任务生命周期管理。
网页端任务控制台。
Claude/Codex 独立 PowerShell 窗口启动。
SSE 日志流。
xterm.js 终端显示。
VSCode-like 终端视觉。
VS Code 插件任务树。
当前网页终端已经从 <pre> 升级为 xterm.js，但还有两个体验问题需要继续完善：
终端区域太小
Claude/Codex 两个终端上下排列后，每个窗口高度不足。
实际使用时可能只能看到一行或极少输出。
终端虽然已接入 xterm.js，但容器高度太小，无法有效观看运行过程。

运行完成感知不足
Claude/Codex CLI 是否已经退出，目前主要靠用户观察终端日志。
日志中虽然已有 CLI exit code: N，但 UI 没有充分利用这个信号。
用户可能不知道什么时候该点击“Claude 已完成”或“Codex 已完成”。
页面任务状态刷新不够主动，运行过程中状态反馈偏静态。

因此，本次更新应同时解决：
终端窗口可观看性。
CLI 退出完成感知。
运行中任务自动刷新。
下一步操作提示。
总体目标
让网页端运行终端真正可用、可看、可判断下一步：
Claude/Codex 两个终端窗口有足够高度。
不再出现终端只能显示一行的问题。
xterm 在 resize 后正常 fit。
Claude/Codex CLI 退出后，UI 能识别退出码。
active client 退出后，页面提示用户可以收集结果或处理审查结果。
运行中任务自动刷新状态。
不自动推进任务状态，仍由用户手动确认。
不新增网页命令输入能力。
当前技术基础
后端：
gui/server.py已有 /api/tasks/{taskId}/terminal/{client} metadata API。
已有 /api/tasks/{taskId}/terminal/{client}/stream SSE API。
_terminal_stream() 会读取日志 chunk。
日志中存在结束标记：CLI exit code: N。

前端：
gui/static/app.js已有 terminalConnections。
已有 EventSource 连接逻辑。
已有 stale task 防护。
已有 subKey 防重复重连逻辑。
已有 xterm.js 实例管理。
已有 terminal placeholder、connect、load history 等逻辑。

布局：
gui/static/index.html已有 runtime-terminal-panel。

gui/static/styles.css.inspector 右侧栏通过 grid 分配任务详情、运行终端、任务历史、任务产物高度。
当前运行终端区域高度不足，需要调整布局优先级。

实施策略
本次优先做低风险增强：
先扩大终端布局
主要修改 CSS。
保证两个终端同时显示时，每个都能看到多行。
让运行终端区域成为右侧 inspector 的主要区域之一。
任务详情、历史、产物区域可以压缩或内部滚动。

再增强完成感知
后端从日志中解析 CLI exit code: N。
metadata 返回 finished、exitCode、lastLogUpdateAt。
SSE done 事件携带 exitCode。
前端终端标题和状态 badge 显示“已退出 / 退出码 N”。

最后增加自动刷新
当存在运行中任务时，周期性刷新任务状态。
刷新不能导致 xterm 重建、闪烁、重复输出。
保留现有 subKey 判断。
不自动调用“Claude 已完成”或“Codex 已完成”。

不做事项
不实现完整 PTY / ConPTY。
不实现 WebSocket 双向终端。
不新增网页命令输入框。
不自动推进任务状态。
不自动点击完成按钮。
不自动 kill 进程。
不自动 commit、push、merge。
不读取 .env 或密钥文件。
不允许通过 URL 读取任务目录外日志。