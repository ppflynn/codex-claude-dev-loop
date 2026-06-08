# 代码审计报告

> **状态**：⚠️ 历史审计记录 — 本文档保留原始审计发现。**所有 P1 问题已于 2026-06-09 修复**，修复验证详见 [TEST_REPORT.md](TEST_REPORT.md#审计问题修复验证2026-06-09)。
>
> 审计日期：2026-06-09
> 审计范围：scripts/run-claude.ps1 + 全部配套文件
> 审计方法：逐行只读审查
> 原始结论：**无 P0 问题。3 个 P1 问题需修复。5 个 P2 问题建议修复。**（已全部修复 ✅）

---

## 审计结果总览

| 等级 | 数量 | 说明 |
|------|------|------|
| P0 | 0 | 无破坏文件、泄露密钥或危险操作 |
| P1 | 3 | 核心功能有不可用的风险 |
| P2 | 5 | 边界情况或文档不一致 |
| P3 | 3 | 可选改进 |

---

## P1 — 核心功能可能无法运行

### P1-1：Claude Code 无权限修改文件，核心流程断裂

**位置**：[scripts/run-claude.ps1:126](scripts/run-claude.ps1#L126)

**问题**：脚本以 `claude -p` 非交互模式运行，但未配置项目级权限（无 `.claude/settings.json`，无 `--permission-mode` 标志）。Claude Code 在 `-p` 模式下每次文件编辑仍需批准，无人值守运行时编辑会被跳过。

**证据**：`docs/claude-run.log` 第 39 行：
```
The edits are ready for your approval — I need write permission to modify
`demo-project/index.js` and `demo-project/test.js`.
```
Claude 识别了任务但未能实际修改任何文件。`demo-project/test.js` 执行结果仍是 4 个测试（未经改动）。

**影响**：用户运行脚本后看到 "finished successfully"，但代码未改动。核心承诺"Claude Code 修改代码"不可用。

**修复建议**：在 README 的"快速开始"第 1 步之前增加"第 0 步：配置权限"，引导用户创建 `.claude/settings.json`。或提供一个 `scripts/setup.ps1` 自动创建该文件。

---

### P1-2：`$ErrorActionPreference = "Stop"` 导致 claude 未安装时的友好提示不可达

**位置**：[scripts/run-claude.ps1:14](scripts/run-claude.ps1#L14) 和 [scripts/run-claude.ps1:60-65](scripts/run-claude.ps1#L60-L65)

**问题**：
```powershell
$ErrorActionPreference = "Stop"    # 第 14 行：所有错误变为终止错误
...
$claudeVersion = claude --version 2>&1   # 第 60 行
if ($LASTEXITCODE -ne 0) {               # 第 61 行：永远不会执行
```

当 `claude` 命令不存在时，PowerShell 在第 60 行抛出 `CommandNotFoundException`。由于第 14 行的 `Stop` 设置，该异常是**终止错误**，脚本立即退出。第 61-65 行的友好提示**永远不会被执行**。

用户实际看到的是：
```
claude : The term 'claude' is not recognized as the name of a cmdlet...
```
而不是脚本精心编写的：
```
ERROR: 'claude' command not available.
Please install Claude Code CLI first.
```

**影响**：用户体验差。尤其是初学者可能不理解 PowerShell 错误信息。

**修复建议**：在第 60 行前添加 `Get-Command claude -ErrorAction SilentlyContinue` 检查，或使用 `try/catch` 包裹 claude 调用：
```powershell
$claudeVersion = try { claude --version 2>&1 } catch { $null }
if ($LASTEXITCODE -ne 0 -or -not $claudeVersion) { ... }
```

---

### P1-3：`$LASTEXITCODE` 通过管道赋值后可能总是为 0，执行失败被掩盖

**位置**：[scripts/run-claude.ps1:126-130](scripts/run-claude.ps1#L126-L130)

**问题**：
```powershell
$claudeOutput = & claude -p $prompt 2>&1 | ForEach-Object {
    Write-Host $_
    $_
}
$exitCode = $LASTEXITCODE
```

在 PowerShell 中，当原生命令的输出通过管道赋值给变量时（`$var = & native_cmd | ...`），`$LASTEXITCODE` 的行为在不同版本间不一致。尤其是在 Windows PowerShell 5.x 中，管道末端的 `ForEach-Object`（PowerShell cmdlet）可能重置 `$LASTEXITCODE` 为 0，导致**Claude 失败时脚本仍返回退出码 0**。

**证据**：`docs/claude-run.log` 显示 Claude 未能实际修改代码（任务失败），但脚本报告 "finished successfully" 且退出码为 0。这说明退出码传递已经不可靠。

**影响**：如果 Claude 崩溃、超时或部分失败，调用方（如 CI、外部调度器）会收到退出码 0，认为一切正常。

**修复建议**：将退出码捕获与管道分离：
```powershell
$claudeOutput = & claude -p $prompt 2>&1
$exitCode = $LASTEXITCODE
# 然后再显示和记录
$claudeOutput | ForEach-Object { Write-Host $_ }
```

或使用 `$PSNativeCommandUseErrorActionPreference` 或检查 `$?` 自动变量作为后备。

---

## P2 — 边界问题

### P2-1：`Resolve-Path` 返回 PathInfo 对象而非字符串，存在类型风险

**位置**：[scripts/run-claude.ps1:18](scripts/run-claude.ps1#L18)

**问题**：
```powershell
$ProjectDir = Resolve-Path (Join-Path $ScriptDir "..")
```

`Resolve-Path` 返回 `System.Management.Automation.PathInfo` 对象，而非纯字符串。后续使用中：
- `Set-Location $ProjectDir`（第 19 行）：PathInfo 通过位置参数绑定到 `-Path`，尚可工作
- `"  $ProjectDir"`（第 33、45 行）：字符串插值隐式调用 `.ToString()`，正常
- `"Project: $ProjectDir"` 写入日志（第 112 行）：同上

当前能工作，但依赖隐式类型转换。如果未来 PowerShell 版本改变参数绑定行为，或某处需要纯字符串路径（如调用 .NET API），会出错。

**修复建议**：显式转换为字符串：
```powershell
$ProjectDir = (Resolve-Path (Join-Path $ScriptDir "..")).Path
```

---

### P2-2：README 说"读取计划"但脚本实际不读 PLAN.md

**位置**：[README.md:17](README.md#L17) vs [scripts/run-claude.ps1:76-102](scripts/run-claude.ps1#L76-L102)

**问题**：README 描述脚本功能为"读取计划，调用 Claude Code"。但脚本**不读取** PLAN.md 内容。脚本只检查文件是否存在（第 43 行）和获取文件大小（第 53 行）。PLAN.md 的内容由 Claude 自己去读。

这是一种设计选择，有其优点（避免命令行注入、避免命令行长度限制），但与文档描述不一致。另外：用户最初的设计意图是"脚本读取 PLAN.md 并传递给 Claude"，而当前实现不同。

**影响**：用户可能期望 PLAN.md 内容被嵌入 prompt，但实际上 prompt 是固定文本。不影响功能，但文档应准确。

**修复建议**：更新 README 描述为"检查 PLAN.md 是否存在，然后指示 Claude Code 读取并实施计划"。

---

### P2-3：Claude 退出码 0 不代表任务成功

**位置**：[scripts/run-claude.ps1:153-157](scripts/run-claude.ps1#L153-L157)

**问题**：
```powershell
if ($exitCode -eq 0) {
    Write-Host " Claude Code finished successfully." -ForegroundColor Green
}
```

`claude -p` 退出码 0 只表示 Claude 进程正常退出（成功输出了一段文本），**不表示计划被成功实施**。如 `claude-run.log` 所示：Claude 退出码为 0，但它只是请求了编辑权限而没有实际修改文件。

**影响**：用户看到 "finished successfully" 就以为代码改好了，但实际上可能什么都没变。

**修复建议**：在 "finished successfully" 后面添加一行提示：
```powershell
Write-Host " NOTE: Exit code 0 means Claude ran without crashing." -ForegroundColor Yellow
Write-Host " Please verify changes with: git diff" -ForegroundColor Yellow
```

---

### P2-4：缺少 .gitignore

**位置**：项目根目录

**问题**：项目没有 `.gitignore` 文件。以下文件应在 `.gitignore` 中：
- `docs/claude-run.log` — 每次运行产生的日志
- `docs/IMPLEMENTATION_REPORT.md` — Claude 自动生成
- `docs/CODEX_REVIEW.md` — Codex 自动生成
- `.claude/` — 用户本地权限配置

如果不忽略，这些自动生成的文件可能被误提交。

**修复建议**：创建 `.gitignore` 包含上述路径。

---

### P2-5：含中文或空格的路径未经过验证

**位置**：[scripts/run-claude.ps1:17-19](scripts/run-claude.ps1#L17-L19)

**问题**：脚本通过 `$MyInvocation.MyCommand.Path` 获取脚本路径、`Split-Path` 取父目录、`Join-Path` 拼接 ".." 来定位项目根目录。这套逻辑**理论正确**，能处理空格和中文。但：

1. 从未在包含中文或空格的路径（如 `e:\我的项目\codex tool\`）中实际测试过
2. PowerShell 内部对 Unicode 路径的处理在不同版本间有细微差别
3. `Set-Location` 后的相对路径引用（如 `"docs/PLAN.md"`）在中文路径下可能触发编码问题

**影响**：用户如果把项目放在含中文的路径下（这是中国用户的常见场景），可能遇到意外的路径错误。

**修复建议**：在 TEST_REPORT.md 或 README 中注明"建议将项目放在纯英文路径中"，或在中文路径下实际测试一次。

---

## P3 — 可选优化

### P3-1：空 PLAN.md 能通过检查

**位置**：[scripts/run-claude.ps1:53](scripts/run-claude.ps1#L53)

**问题**：脚本只检查文件存在和文件大小，不验证内容。一个全是空白或只有标题的 PLAN.md 也能通过检查，Claude 会被要求去读一个空计划。

**修复建议**：添加最小内容检查（如至少 50 字节或包含特定关键词）。

---

### P3-2：PLAN.md 内容发送到 Anthropic API 未在文档中披露

**位置**：[scripts/run-claude.ps1:81](scripts/run-claude.ps1#L81)（prompt 要求 Claude 读取 PLAN.md）

**问题**：Claude 读取 PLAN.md 时，文件内容会发送到 Anthropic 的服务器。README 未提及这一点。对于处理私密代码计划的用户，这是需要知情的信息。

**修复建议**：在 README 的"安全限制"部分添加说明："PLAN.md 内容由 Claude Code 读取后发送至 Anthropic API。请勿在 PLAN.md 中包含密钥或敏感信息。"

---

### P3-3：安全规则仅靠 prompt 约束，无技术强制执行

**位置**：[scripts/run-claude.ps1:93-101](scripts/run-claude.ps1#L93-L101)

**问题**：对 Claude 的全部安全限制都是自然语言 prompt 指令。Claude 会遵守这些指令，但如果 prompt 失效或被忽略，没有第二层技术防护。例如：
- `git commit` 没有通过 `.git/hooks` 阻止
- 文件删除没有通过文件系统权限阻止
- `.env` 读取没有通过文件权限阻止

**当前风险评估**：对于个人使用的本地工具，风险可接受。Claude 模型可靠地遵循 prompt 指令。

**修复建议**：未来可考虑添加 Git hooks（`pre-commit` 钩子检查是否由 AI 触发）作为纵深防御。第一版不需要。

---

## 逐项清单

按用户提出的 12 个问题逐一回答：

| # | 检查项 | 结论 | 对应问题 |
|---|--------|------|----------|
| 1 | run-claude.ps1 是否真的能够调用 Claude Code | ⚠️ 能调用，但不能修改文件 | P1-1 |
| 2 | 路径中有中文或空格时是否能够工作 | ⚠️ 理论可行，未经实测 | P2-5 |
| 3 | docs/PLAN.md 不存在时是否正确退出 | ✅ 正确退出，提示清晰 | — |
| 4 | claude 命令未安装时是否有明确提示 | ❌ 友好提示被跳过 | P1-2 |
| 5 | Claude执行失败时是否返回非零退出码 | ❌ 可能被掩盖为 0 | P1-3 |
| 6 | 是否可能读取或泄露 .env | ✅ 脚本不触碰 .env | — |
| 7 | 是否可能自动提交或推送代码 | ✅ 无 commit/push | — |
| 8 | 是否包含 git reset --hard 等危险命令 | ✅ 无危险命令 | — |
| 9 | 是否存在 PowerShell 命令注入 | ✅ 无用户输入拼接 | — |
| 10 | 是否将计划文本安全传递给 Claude | ✅ Claude 自行读取 | — |
| 11 | README 中的命令是否与真实实现一致 | ⚠️ 描述不够精确 | P2-2 |
| 12 | 测试是否真正执行 | ⚠️ 部分执行，报告诚实 | P1-3 |

---

## 总体评价

**代码质量**：脚本结构清晰，注释完整，错误处理思路正确。安全问题考虑得比较周全：无命令注入、无自动 git 操作、无 .env 泄露。代码只有 163 行，简洁可控。

**核心缺口**：3 个 P1 问题共同导致一个结果——用户运行脚本后，Claude 可以对话但不能改代码，退出码也不反映真实状态。这 3 个问题**必须修复**才能达到"最小可运行"的标准。修复量很小，约 10 行改动。

**安全性**：在当前代码层面（PowerShell 脚本本身）安全性良好。对 Claude 的约束依赖 prompt，对个人本地使用场景来说是合适的。

**建议优先修复**：P1-1（权限配置）→ P1-3（退出码）→ P1-2（claude 检测）→ P2-2（README 准确性）
