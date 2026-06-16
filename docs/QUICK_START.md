# 快速开始

本文档面向第一次使用 Codex Claude Dev Loop 的用户。读完这一篇，你应该能：

- 在本机把网页控制台跑起来。
- 导入一个 Git 项目，初始化为协同项目。
- 创建任务，跑完一轮 Claude → Codex 循环。

> 适合系统：Windows 10 / 11，已安装 Python 3.9+ 和 PowerShell 5.1+。

---

## 0. 前置条件

| 依赖 | 说明 |
| --- | --- |
| Python 3.9+ | 用于运行 `gui/server.py`。推荐使用官方安装包，安装时勾选「Add python.exe to PATH」。 |
| PowerShell | Windows 自带的 Windows PowerShell 5.1 即可；想用 PowerShell 7 也可以。 |
| Git for Windows | 用于 `assert_git_work_tree`、diff 收集等。 |
| Claude CLI（可选） | 实际启动 Claude 时需要，仅做体验页面不需要。 |
| Codex CLI（可选） | 实际启动 Codex 时需要，仅做体验页面不需要。 |

检查依赖：

```powershell
py -3 --version
git --version
```

---

## 1. 启动网页控制台

仓库根目录有 `start.bat`。三种启动方式任选其一：

**方式 A：双击**

在资源管理器里双击 `start.bat`。

**方式 B：命令行（推荐）**

```powershell
cd E:\AI-Tools\codex-claude-dev-loop-vscode
.\start.bat
```

**方式 C：直接调 PowerShell**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-gui.ps1
```

启动成功后控制台会输出：

```
Codex Claude Dev Loop - GUI launcher
  Project: ...
  Server : ...gui\server.py
  Python : py
GUI running at http://127.0.0.1:8765/
Press Ctrl+C to stop.
```

浏览器打开 <http://127.0.0.1:8765/>。

### 启动失败怎么办

| 现象 | 原因 / 解决 |
| --- | --- |
| `ERROR: Python was not found on PATH.` | 没装 Python 或没加 PATH。重装并勾选 Add to PATH，或用 `py -3` 显式调用。 |
| `ERROR: gui\server.py was not found.` | 仓库结构变了或文件被删。`git restore gui/server.py` 即可。 |
| `ERROR: Port 8765 is already in use or unavailable.` | 端口被占。换端口：`.\start.bat 8787` 或 `... -Port 8787`。 |
| 浏览器打开是空白页 | 检查防火墙是否拦截了 127.0.0.1，或换一个端口再试。 |

---

## 2. 导入项目

1. 在左侧「导入项目目录」输入框粘贴一个本地 Git 仓库的绝对路径，例如 `E:\work\my-app`。
2. 点「导入」。项目会写入工具的 `.gui/projects.json` 白名单。
3. 在左侧项目列表里点中刚导入的项目。中间区域会显示项目名、路径、分支等信息。

> 移除项目（顶栏「移除项目」）只从工具白名单里删除，**不会**动本地文件。

---

## 3. 初始化项目（可选）

如果导入的项目本身不是 Codex Claude Dev Loop 协同项目（即项目列表里标签写的是「Git 仓库」而不是「协同项目」），点顶栏「初始化项目」按钮。

工具会复制这些文件到目标项目：

- `scripts/run-claude.ps1` — Claude/Codex 自动循环脚本
- `docs/PLAN.template.md`、`docs/IMPLEMENTATION_REPORT.template.md`
- `docs/CODEX_REVIEW.schema.json`
- `.claude/settings.json`（项目级安全设置）

复制完，项目类型会更新为「协同项目」，PLAN 编辑器和任务控制按钮才会激活。

---

## 4. 写开发计划

中间区域有一个 `PLAN.md` 编辑器。点「保存 PLAN」会写入 `<project>/docs/PLAN.md`。这个文件不是必须的，但写一份对 Claude 实现很有帮助。

模板见 `docs/PLAN.template.md`。

---

## 5. 创建任务

任务表单字段：

| 字段 | 含义 |
| --- | --- |
| 标题 | 短句，便于在任务列表里识别。 |
| 描述 | 要实现或修复的内容。会拼到 Claude 的实现 prompt 里。 |
| 验收标准 | 通过条件、边界行为、需要检查的内容。会拼到 Codex 的审查 prompt 里。 |
| 测试命令 | 留空时自动推断（项目里能找到 `pytest` 就用 `py -B -m pytest -q`，否则报错）。 |
| 最大轮次 | 1–15，默认 3。Claude → Codex 算一轮。 |

注意：**创建任务时要求工作树是干净的**，否则会返回 `DirtyWorkTreeError`。先把改动 commit、stash 或放弃再创建任务。

---

## 6. 跑一轮 Claude → Codex

1. 点「启动 Claude CLI」。会弹出一个 PowerShell 窗口运行 Claude 实现。
2. Claude 退出后，回到网页。底部 Claude 终端会显示「请点击 Claude 已完成」的 toast，并让按钮闪烁。
3. 点「Claude 已完成」。工具会：
   - 收集本轮 Git diff、status、diff --stat。
   - 运行测试命令。
   - 写入 `CODEX_REVIEW_PROMPT.md`。
   - 状态推进到「等待 Codex」。
4. 点「启动 Codex CLI」。新 PowerShell 窗口里 Codex 会审查本轮变更。
5. Codex 退出后，点「Codex 已完成」。工具会：
   - 读取 `CODEX_REVIEW.json`。
   - 如果 `status = PASS`，任务结束。
   - 如果 `status = NEEDS_FIX` 且未达最大轮次，生成 `FIX_PROMPT_ROUND_<n+1>.md`，状态回到「等待 Claude」。
   - 如果 `status = NEEDS_FIX` 且已达最大轮次，任务标记为 FAILED。

整个过程中，底部 Claude / Codex 终端会自动跟随当前任务和轮次，并显示退出码。

---

## 7. 归档、删除、恢复

任务列表顶部有三个 Tab：

- **当前**：活跃任务。
- **已归档**：用户主动归档的任务，可通过「恢复任务」回到当前。
- **回收站**：被「删除任务记录」移走的任务。任务目录会先移到 `.gui/trash/tasks/`，可通过「从回收站恢复」回到当前。

运行中的任务不能归档或删除。

---

## 8. 停止 GUI

在启动 GUI 的终端里按 `Ctrl+C`。

---

## 常见问题

**Q：Claude 实现完了，但点「Claude 已完成」时报「未检测到实现改动」。**

A：本轮 Git diff 为空。检查 Claude 是否真的改了文件，或者改动是否还在 stash 里。

**Q：Codex 已经写完 `CODEX_REVIEW.json`，但点「Codex 已完成」时返回「waiting for retry」。**

A：工具检测到当前的 `CODEX_REVIEW.json` 不是本轮新生成的（修改时间早于本轮的 marker 文件）。确保 Codex 真的写了新文件，或者删掉旧的 `CODEX_REVIEW.json` 再让 Codex 跑一遍。

**Q：能不能在网页里直接输入命令到 Claude/Codex 终端？**

A：不行。终端是只读镜像，避免任意命令执行。命令在 PowerShell 窗口里输入。

**Q：我想换端口怎么办？**

A：`.\start.bat 8787`，或者 `powershell -ExecutionPolicy Bypass -File scripts\start-gui.ps1 -Port 8787`。

**Q：`.env` 文件会怎么样？**

A：工具不会读取、输出、diff `.env` 或 `.env.*`。如果本轮 Git diff 里出现了 `.env`，工具会拒绝推进任务并提示。
