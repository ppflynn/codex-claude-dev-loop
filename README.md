# AI Coding Collaboration Tool

一个最小化的 AI 编程协作工具：**主 AI 制定计划 → Claude Code 实施 → Codex 审查**。

## 工作流程

```
你写下需求到 PLAN.md  →  运行 run-claude.ps1  →  Claude Code 改代码
                                                    ↓
                                              生成实施报告
```

## 项目结构

```
├── .claude/
│   └── settings.json                ← 可选：细粒度权限配置
├── .gitignore                       ← Git 忽略规则
├── scripts/
│   └── run-claude.ps1              ← 唯一入口：检查 PLAN.md，调用 Claude Code 实施
├── docs/
│   ├── PLAN.template.md            ← 开发计划模板（复制为 PLAN.md 使用）
│   ├── PLAN.md                     ← 你的开发计划（用户自己创建）
│   ├── IMPLEMENTATION_REPORT.template.md ← 实施报告模板
│   ├── IMPLEMENTATION_REPORT.md    ← 实施报告（Claude Code 自动生成）
│   ├── CODEX_REVIEW.template.md    ← 代码审查模板
│   ├── CODEX_REVIEW.md            ← 审查报告（Codex 生成）
│   └── claude-run.log             ← Claude 执行日志（脚本自动生成）
├── demo-project/                   ← 简单测试项目（用于验证工具）
│   ├── package.json
│   ├── index.js
│   └── test.js
└── README.md
```

## 快速开始

### 0. 配置权限（仅首次）

脚本使用 `--permission-mode bypassPermissions` 在非交互模式下自动允许文件编辑和测试执行。**注意：此模式会绕过 Claude Code 的工具层权限检查。** 安全约束由 prompt 中的安全规则描述（禁止 git commit/push、删除文件、读取 .env 等），但不由工具层强制阻断。

> ⚠️ **安全提示**：请在可信仓库中使用此工具。如需工具层强制限制，可创建 `.claude/settings.json` 配置 `permissions.allow` 列表，或使用 `--allowedTools` / `--disallowedTools` 参数。

### 1. 准备任务

```powershell
# 复制模板
copy docs\PLAN.template.md docs\PLAN.md

# 编辑 PLAN.md，写入你的开发任务
notepad docs\PLAN.md
```

### 2. 执行

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run-claude.ps1
```

### 3. 查看结果

- 代码改动：
  ```powershell
  git status --short --untracked-files=all   # 查看所有变更（含新增文件）
  git diff                                   # 查看已跟踪文件的修改细节
  ```
  > **注意**：普通 `git diff` 不显示未跟踪的新增文件。如需完整审查 Claude 所有改动，请同时使用以上两条命令。对未跟踪的新增文件（`git status` 中显示为 `??` 的文件），直接读取文件内容进行审查，或使用 `git add -N <path>` 后再通过 `git diff` 查看。
- 实施报告：`docs\IMPLEMENTATION_REPORT.md`
- 执行日志：`docs\claude-run.log`

## 前置要求

| 依赖 | 说明 |
|------|------|
| Git | 当前目录必须是 Git 仓库 |
| Claude Code CLI | `claude --version` 必须可用 |
| Node.js | demo-project 测试需要（你自己的项目不需要） |

> **注意**：建议将项目放在纯英文路径中（不含中文、空格），以避免 PowerShell 编码问题。

## 安全限制

脚本通过 prompt 要求 Claude 遵守以下限制（非工具层强制阻断，依赖 Claude 遵守指令）：

- ❌ git commit / git push
- ❌ git reset --hard / git clean
- ❌ 删除现有文件
- ❌ 读取或输出 .env 内容
- ❌ 修改 .git 目录
- ❌ 开发 MCP / 网页 / 数据库 / 后台任务

> 如需工具层强制限制，请配置 `.claude/settings.json` 的 `permissions.allow` / `permissions.deny`，或使用 `claude` CLI 的 `--allowedTools` / `--disallowedTools` 参数。

## 许可证

MIT
