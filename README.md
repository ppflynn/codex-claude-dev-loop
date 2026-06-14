# AI Coding Collaboration CLI Orchestrator

## 日常使用指南（推荐：图形界面）

这个项目是一个本地 agent 协同工具：**Codex 负责写计划和审查，Claude Code 负责按计划写代码，脚本负责跑测试、收集变更并触发修复轮次**。

### 1. 启动图形界面

在 PowerShell 中进入本项目目录：

```powershell
cd E:\AI-Tools\codex-claude-dev-loop
powershell -ExecutionPolicy Bypass -File scripts\start-gui.ps1
```

浏览器打开：

```text
http://127.0.0.1:8765/
```

如果端口被占用，可以换端口：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-gui.ps1 -Port 8787
```

### 1.1 VS Code 插件

本仓库自带 VS Code 侧边栏插件，源码在 `vscode-extension/`。插件默认连接：

```text
http://127.0.0.1:8765
```

开发或更新插件后，需要先编译 TypeScript，让 VS Code 实际加载的 `out/` 产物同步到最新代码：

```powershell
cd vscode-extension
npm.cmd run compile
```

如果使用的是已安装到本机的插件，还需要把 `package.json`、`resources/` 和编译后的 `out/` 同步到 VS Code 扩展目录，然后在 VS Code 中执行 **Developer: Reload Window** 重新加载扩展宿主。

当前插件能力：

- Activity Bar 显示 **AI Dev Loop** 侧边栏。
- 支持刷新任务、创建任务、打开 `PLAN.md`、Claude prompt、Codex prompt 和实施报告。
- 任务树会显示轮次、状态、运行端和进度；Claude/Codex 运行中会显示旋转图标。
- 右键任务可打开 **Open Task Detail**，查看任务 ID、状态、进度、阶段、运行端、描述、验收标准和历史记录。

### 2. 导入或初始化项目

在界面左侧的 **“导入项目目录”** 输入项目文件夹路径，例如：

```text
E:\AI-Tools\codex-claude-dev-loop
```

然后点击 **“导入”**。

- 如果导入的是已经包含 `scripts/run-claude.ps1` 和 `docs/` 的协同项目，可以直接使用。
- 如果导入的是普通 Git 仓库，界面会显示为 **“待初始化 Git 仓库”**，需要先点击 **“初始化项目”**。初始化会复制协同脚本、`docs` 模板和 `.claude/settings.json` 到目标仓库。

### 3. Codex 写 PLAN

中间的 **“PLAN.md 计划区”** 对应项目里的：

```text
docs/PLAN.md
```

日常用法是：

1. 你把需求告诉 Codex。
2. Codex 把需求整理成 `PLAN.md`。
3. 你检查计划是否清楚。
4. 点击 **“保存 PLAN”**。

建议 `PLAN.md` 至少写清楚：

- 要做什么功能或修什么问题
- 允许修改哪些文件
- 不允许修改哪些文件
- 验收命令，例如 `pytest demo-project -q` 或 `npm test`
- 完成后需要生成什么报告

### 4. Claude Code 写代码

点击 **“开始协同”** 后，GUI 会调用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run-claude.ps1
```

Claude Code 会读取 `docs/PLAN.md`，然后：

1. 按计划修改代码。
2. 写入 `docs/IMPLEMENTATION_REPORT.md`。
3. 自动运行发现到的测试命令。
4. 如果测试失败，在最大轮次内继续让 Claude 修复。

右侧的 **“Claude Code CLI：写代码 / 修复 / 跑测试”** 会显示 Claude 执行、测试和修复相关输出。

### 5. Codex 审查怎么填

界面里的 **“Codex 审查命令”** 不是填写 `review1` / `review2` 的地方。它只在你有自动审查脚本时才填写，例如：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\your-reviewer.ps1
```

这个审查脚本必须读取：

```text
docs/REVIEW_INPUT.md
```

并写出：

```text
docs/CODEX_REVIEW.json
```

如果你是手动审查，**“Codex 审查命令” 留空即可**。

手动审查的流程：

1. 点击 **“开始协同”**。
2. Claude 写完代码并跑完测试后，脚本会生成 `docs/REVIEW_INPUT.md`。
3. 你让 Codex 审查 `docs/REVIEW_INPUT.md` 里的变更、测试结果和 diff。
4. Codex 输出审查 JSON。
5. 把审查结果保存为 `docs/CODEX_REVIEW.json`。
6. 再次点击 **“开始协同”**。

脚本每一轮只认固定文件：

```text
docs/CODEX_REVIEW.json
```

你平时说的：

```text
review1 -> 第一轮审查
review2 -> 第二轮审查
```

在这个项目里对应为：

```text
review1 -> 写入 docs/CODEX_REVIEW.json，并在 JSON 里写 "review_scope": "review1"
review2 -> 下一轮重新写入 docs/CODEX_REVIEW.json，并在 JSON 里写 "review_scope": "review2"
```

如果第一轮审查结果是 `NEEDS_FIX`，脚本会消费这个文件，并把它移动为：

```text
docs/CODEX_REVIEW.json.previous
```

然后把 findings 注入下一轮 Claude 修复。第二轮修复完成后，你需要重新生成新的 `docs/CODEX_REVIEW.json`。

### 6. CODEX_REVIEW.json 示例

需要修复时：

```json
{
  "status": "NEEDS_FIX",
  "findings": [
    {
      "id": "R1-1",
      "severity": "P1",
      "file": "demo-project/calculator.py",
      "line": 12,
      "description": "divide 没有正确处理除数为 0 的情况。",
      "fix_suggestion": "当 b == 0 时抛出 ValueError。"
    }
  ],
  "reviewed_at": "2026-06-11T00:00:00Z",
  "review_scope": "review1",
  "summary": "第一轮审查发现 1 个需要修复的问题。"
}
```

审查通过时：

```json
{
  "status": "PASS",
  "findings": [],
  "reviewed_at": "2026-06-11T00:00:00Z",
  "review_scope": "review2",
  "summary": "第二轮审查通过。"
}
```

字段说明：

- `status`: 只能是 `PASS`、`FAIL`、`NEEDS_FIX`
- `findings`: 问题列表；通过时为空数组
- `reviewed_at`: 审查时间，ISO 格式
- `severity`: 只能是 `P0`、`P1`、`P2`、`P3`
- `file`: 相对项目根目录的文件路径
- `description`: 问题说明
- `fix_suggestion`: 建议修复方式

### 7. 常用运行参数

GUI 中的运行参数对应脚本参数：

| GUI 选项 | 脚本参数 | 用途 |
| --- | --- | --- |
| 最大轮次 | `-MaxRounds` | Claude 最多自动修复几轮，范围 1-10 |
| 跳过测试 | `-SkipTests` | 不运行自动测试 |
| 无测试也通过 | `-AllowNoTests` | 没发现测试命令时也允许通过 |
| 跳过 Codex 审查 | `-SkipCodexReview` | 不要求 `CODEX_REVIEW.json` |
| Codex 审查命令 | `-ReviewCommand` | 自动执行外部 reviewer 脚本 |

### 8. 运行产物在哪里看

GUI 右侧 **“运行产物”** 会显示这些文件：

| 文件 | 作用 |
| --- | --- |
| `docs/IMPLEMENTATION_REPORT.md` | Claude 的实施报告 |
| `docs/claude-run.log` | 完整执行日志 |
| `docs/CHANGES_STATUS.txt` | 本轮 git status |
| `docs/CHANGES_DIFF.txt` | 本轮 git diff |
| `docs/CODEX_REVIEW.json` | Codex 结构化审查结果 |
| `docs/REVIEW_INPUT.md` | 提供给 Codex 审查的输入包 |

### 9. 最常见的日常流程

```text
启动 GUI
  -> 导入项目
  -> Codex 写 PLAN
  -> 保存 PLAN
  -> 开始协同
  -> Claude 写代码并跑测试
  -> Codex 审查 REVIEW_INPUT.md
  -> 保存 CODEX_REVIEW.json
  -> 如果 NEEDS_FIX，再次开始协同
  -> 如果 PASS，流程完成
```

一个自动化的 AI 编程协作编排器：**主 AI 制定计划 → Claude Code 实施 → 自动测试验证 → 失败时自动修复循环**。

## 当前版本 vs 目标版本

| 能力 | 当前版本 (v1.0) | 目标版本 (v2.0) |
|------|:---:|:---:|
| 读取 PLAN.md 并调用 Claude 实施 | ✅ | ✅ |
| 自动运行测试并验证退出码 | ✅ (直接调用，避免 shell 注入) | ✅ |
| 测试失败时自动进入修复循环 | ✅ (最多 N 轮, Round 2 已修复) | ✅ |
| 自动收集 git status/diff 到文件 | ✅ (含本轮隔离) | ✅ |
| 结构化 JSON 审查 schema + 校验 | ✅ (schema 已定义, 已接入主流程) | ✅ |
| 密文检测与日志脱敏 | ✅ | ✅ |
| 工具层安全限制 (.claude/settings.json) | ⚠️ deny 规则已配置, bypassPermissions 行为待验证 | ✅ |
| Codex 自动审查 | ✅ (通过 `-ReviewCommand` 接入外部 reviewer) | ✅ |
| Codex findings → Claude 修复轮 | ✅ (读取 NEEDS_FIX 并自动注入下一轮) | ✅ |
| MAX_ROUNDS 硬上限 (1-10) | ✅ | ✅ |
| 脏工作区基线检测 + 本轮变更隔离 | ✅ (CHANGES_THIS_ROUND.txt) | ✅ |
| Ctrl+C 优雅退出 (CancelKeyPress 处理) | ✅ (Round 2 已增强) | ✅ |
| 无测试时误判 PASS | ✅ 已修复: 默认 NEEDS_MANUAL_VERIFY (exit 2) | ✅ |

## 工作流程

```
你写下需求到 PLAN.md → 运行 run-claude.ps1
                           ↓
                      [Round 1] Claude Code 实施
                           ↓
                      自动运行测试 (pytest / npm test / ...)
                           ↓
                     ┌─ 全部通过 → 校验 CODEX_REVIEW.json
                     │
                     └─ 有失败 → [Round 2] Claude 修复
                                       ↓
                                  重新运行测试
                                       ↓
                                 ┌─ 通过 → 校验 CODEX_REVIEW.json
                                 └─ 失败 → ... (最多 MAX_ROUNDS 轮)
                                                ↓
                                          ❌ FAIL_MAX_ROUNDS
                           ↓
        PASS → ✅ PASS / NEEDS_FIX → Claude 修复轮 / 缺失 → exit 8
```

## 项目结构

```
├── .claude/
│   └── settings.json                ← 项目级安全配置（工具层 deny 规则）
├── .gitignore                       ← Git 忽略规则
├── scripts/
│   ├── run-claude.ps1              ← 主编排器：检查、调用 Claude、测试、修复循环
│   └── test-orchestrator.ps1       ← 编排器级单元测试
├── docs/
│   ├── PLAN.template.md            ← 开发计划模板（复制为 PLAN.md 使用）
│   ├── PLAN.md                     ← 你的开发计划（用户手动创建）
│   ├── IMPLEMENTATION_REPORT.template.md ← 实施报告模板
│   ├── IMPLEMENTATION_REPORT.md    ← 实施报告（Claude Code 自动生成）
│   ├── CODEX_REVIEW.template.md    ← 代码审查模板（Markdown 格式）
│   ├── CODEX_REVIEW.md            ← 审查报告（Codex 生成）
│   ├── CODEX_REVIEW.schema.json   ← Codex 审查 JSON Schema（结构化验证）
│   ├── CHANGES_STATUS.txt         ← 本轮 git status（自动生成）
│   ├── CHANGES_DIFF.txt           ← 本轮 git diff（自动生成）
│   ├── BASELINE_STATUS.txt        ← 运行前基线（自动生成）
│   ├── claude-run.log             ← Claude 执行日志（自动生成，密文已脱敏）
│   ├── TEST_REPORT.md             ← 测试报告
│   └── AUDIT.md                   ← 代码审计报告
├── gui/                            ← 本地图形界面和 HTTP API
│   ├── server.py                   ← Web/API 服务入口
│   ├── static/                     ← 浏览器界面资源
│   └── orchestrator/               ← 任务模型、状态机、Git 和测试编排逻辑
├── vscode-extension/               ← VS Code 侧边栏插件
│   ├── src/                        ← 插件 TypeScript 源码
│   ├── out/                        ← 编译后的插件产物（VS Code 实际加载）
│   ├── resources/                  ← Activity Bar 图标等资源
│   └── package.json                ← 插件命令、菜单和配置声明
├── tests/                          ← Python 自动化测试
├── demo-project/                   ← 简单测试项目（用于验证工具）
│   ├── package.json
│   ├── index.js
│   ├── test.js
│   ├── calculator.py
│   ├── test_calculator.py
│   └── README.md
└── README.md
```

## 快速开始

### 0. 安全配置（首次）

项目附带了 `.claude/settings.json`，配置了工具层的 deny 规则：
- 禁止 `git commit` / `git push` / `git reset --hard` / `git clean`
- 禁止读取或写入 `.env` 文件

脚本使用 `--permission-mode bypassPermissions` 允许 Claude 自由编辑文件和运行测试。安全约束来自两个层面：
1. **工具层**：`.claude/settings.json` 的 `permissions.deny` 规则强制拦截危险操作
2. **Prompt 层**：自然语言安全规则（Claude 遵守指令，不执行 git 提交/推送等）

> ⚠️ **安全提示**：请在可信仓库中使用此工具。如需更严格的限制，可编辑 `.claude/settings.json` 添加更多 deny 规则。

### 1. 准备任务

```powershell
# 复制模板
copy docs\PLAN.template.md docs\PLAN.md

# 编辑 PLAN.md，写入你的开发任务
notepad docs\PLAN.md
```

PLAN.md 中可以包含测试命令（脚本会自动发现）：
```markdown
## 验收标准
运行：
pytest demo-project -q
所有测试通过。
```

### 2. 执行

```powershell
# 默认：最多 3 轮修复
powershell -ExecutionPolicy Bypass -File scripts\run-claude.ps1

# 自定义轮次
powershell -ExecutionPolicy Bypass -File scripts\run-claude.ps1 -MaxRounds 5

# 跳过自动测试（手动验证）
powershell -ExecutionPolicy Bypass -File scripts\run-claude.ps1 -SkipTests

# 跳过 Codex 审查要求（测试通过即 PASS）
powershell -ExecutionPolicy Bypass -File scripts\run-claude.ps1 -SkipCodexReview

# 自动调用外部 reviewer，生成 docs\CODEX_REVIEW.json
powershell -ExecutionPolicy Bypass -File scripts\run-claude.ps1 `
  -ReviewCommand "powershell -NoProfile -ExecutionPolicy Bypass -File scripts\your-reviewer.ps1"
```

默认流程要求 `docs\CODEX_REVIEW.json` 存在且有效：
- `status=PASS`：测试也通过时返回 0。
- `status=NEEDS_FIX`：脚本把 findings 注入下一轮 Claude prompt，修复后要求新的 Codex review。
- 文件缺失：返回 exit 8；确认只想跑 Claude+测试时使用 `-SkipCodexReview`。

使用 `-ReviewCommand` 时，脚本会在每轮测试通过后自动生成：
- `docs\REVIEW_INPUT.md`：给 reviewer 的审查输入，包含 git status、git diff、测试结果和 JSON 输出要求。
- `docs\TEST_RESULTS.txt`：本轮测试输出。

然后脚本会执行你传入的 reviewer 命令。该命令必须读取 `docs\REVIEW_INPUT.md`，并写出符合 schema 的 `docs\CODEX_REVIEW.json`。脚本会先把旧 `CODEX_REVIEW.json` 移到 `docs\CODEX_REVIEW.json.before-auto`，防止复用旧审查结果。

### 3. 查看结果

脚本退出码含义：

| 退出码 | 含义 |
|:---:|------|
| 0 | PASS / SKIPPED — 所有测试通过 或 用户跳过测试 |
| 1 | 环境问题（非 Git 仓库 / PLAN.md 不存在 / Claude 未安装） |
| 2 | FAIL — MAX_ROUNDS 耗尽 或 需要人工验证（无测试命令） |
| 3 | FAIL — Claude 异常退出或未生成报告 |
| 4 | INTERRUPTED — 脚本被 Ctrl+C 中断 |
| 5 | UNKNOWN — 未知状态 |
| 6 | FAIL — CODEX_REVIEW.json 无效或被 Codex 审查拒绝 |
| 7 | NEEDS_FIX — Codex 审查要求额外修复 |
| 8 | NEEDS_CODEX_REVIEW — CODEX_REVIEW.json 缺失（使用 -SkipCodexReview 可跳过） |

生成的文件：

| 文件 | 内容 |
|------|------|
| `docs/IMPLEMENTATION_REPORT.md` | Claude 实施/修复报告 |
| `docs/claude-run.log` | 完整执行日志（密文已脱敏） |
| `docs/CHANGES_STATUS.txt` | 本轮 `git status --short --untracked-files=all` |
| `docs/CHANGES_DIFF.txt` | 本轮 `git diff` |
| `docs/BASELINE_STATUS.txt` | 运行前工作区基线（用于区分新增 vs 历史改动） |
| `docs/REVIEW_INPUT.md` | 自动审查输入包（使用 `-ReviewCommand` 时生成） |
| `docs/TEST_RESULTS.txt` | 自动审查使用的测试结果摘要 |
| `docs/CODEX_REVIEW.json` | 外部 Codex 结构化审查输入/结果（PASS/NEEDS_FIX/FAIL） |

查看代码改动：
```powershell
git status --short --untracked-files=all   # 查看所有变更（含新增文件）
git diff                                   # 查看已跟踪文件的修改细节
```
> **注意**：普通 `git diff` 不显示未跟踪的新增文件。如需完整审查 Claude 所有改动，请同时使用以上两条命令。对未跟踪的新增文件（`git status` 中显示为 `??` 的文件），直接读取文件内容进行审查，或使用 `git add -N <path>` 后再通过 `git diff` 查看。

## 前置要求

| 依赖 | 说明 |
|------|------|
| Git | 当前目录必须是 Git 仓库 |
| Claude Code CLI | `claude --version` 必须可用 |
| Node.js | demo-project 测试需要（你自己的项目不一定需要） |
| Python 3 + pytest | Python 项目测试需要（你自己的项目不一定需要） |

> **注意**：建议将项目放在纯英文路径中（不含中文、空格），以避免 PowerShell 5.x 编码问题。脚本已使用 UTF-8 without BOM 编码策略以最大限度兼容。

## 安全限制

脚本通过两层机制进行安全约束：

### 工具层（强制 — `.claude/settings.json`）
- ❌ `git commit` / `git push`
- ❌ `git reset --hard` / `git clean`
- ❌ 危险删除操作（`rm -rf`、`del /f`）
- ❌ 读取或写入 `.env` 文件

### Prompt 层（AI 遵守）
- ❌ 删除现有文件（修改可，删除不可）
- ❌ 修改 `.git` 目录
- ❌ 开发 MCP / 网页 / 数据库 / 后台任务
- ❌ 多 Agent 并行

### 日志安全
- 所有写入 `claude-run.log` 的内容在落盘前自动扫描密文模式
- 检测到疑似 API Key / Token / JWT / Private Key 时自动脱敏为 `[REDACTED]`

> 💡 **设计权衡**：脚本使用 `--permission-mode bypassPermissions` 以支持无人值守运行（Claude 可以在非交互模式下自由编辑文件和运行测试）。工具层限制通过 `.claude/settings.json` 的 `permissions.deny` 规则实现，并非完全依赖 prompt。

## 许可证

MIT
