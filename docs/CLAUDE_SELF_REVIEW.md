# CLI 工具只读审计报告

> **审计日期**：2026-06-09
> **审计范围**：`scripts/run-claude.ps1`（191 行）+ 全部配套文件
> **审计方法**：逐行只读审查 + 对照 10 项检查点 + 独立运行测试验证（grep / 语法解析 / 测试执行）
> **审计分支**：`feature/automatic-cli-orchestrator`
> **审计原则**：独立重审——不依赖已有审计结论，所有结论均基于本轮验证证据

---

## 审计总览

| # | 检查项 | 结论 | 严重度 | 关键证据 |
|---|--------|------|--------|----------|
| 1 | 是否能正确生成 PLAN.md | ❌ 不生成 | P2 | grep：脚本无任何 Copy/New/Set PLAN.md 操作 |
| 2 | 是否能正确引导 Claude 开发 | ✅ 能 | PASS | prompt 含 5 步指令链 + 7 条安全规则 |
| 3 | 是否正确收集 git status 和 git diff | ❌ 不收集 | P1 | grep：仅 Write-Host 建议，不执行 git 命令 |
| 4 | 是否能校验 CODEX_REVIEW.json | ❌ 无此能力 | P0 | 项目无 .json 审查文件，无 schema，无校验代码 |
| 5 | 循环次数是否有限制 | ❌ 无循环 | P1 | grep：无 while/for/do/until，仅 ForEach-Object |
| 6 | Ctrl+C 是否能正常退出 | ⚠️ 默认行为 | P3 | grep：无 trap/finally/CancelKeyPress |
| 7 | 是否存在危险 Git 命令 | ✅ 无 | PASS | 仅 `git rev-parse`（只读），prompt 禁止危险操作 |
| 8 | 是否可能泄露 .env | ✅ 无 | PASS | 四层防护：脚本/提示词/.gitignore/文件系统 |
| 9 | 路径包含中文或空格时是否正常 | ⚠️ 有风险 | P2 | 核心逻辑正确，编码链路有已知脆弱性 |
| 10 | 测试是否真实运行 | ✅ 真实 | PASS | Node.js 4/4 + pytest 17/17 + 语法 PASS（独立重跑） |

**总评**：当前脚本是一个**诚实的最小可用单次 CLI 工具**。191 行代码在错误处理、安全防护、测试真实性和文档完整性上表现出色。但在"自动编排"方向上——自动 git 收集、JSON 结构化审查、审查→修复循环、轮次上限——当前为**零实现**。这些属于**范围差距**而非质量缺陷。

---

## 1. PLAN.md 生成

### 结论：❌ 不生成（P2 — 设计如此，非 bug）

### 逐行分析

[scripts/run-claude.ps1:42-54](scripts/run-claude.ps1#L42-L54) 的完整逻辑：

```powershell
# Step 2: Check docs/PLAN.md exists
if (-not (Test-Path "docs/PLAN.md")) {
    Write-Host "ERROR: docs/PLAN.md not found." -ForegroundColor Red
    Write-Host "  Expected: $ProjectDir\docs\PLAN.md" -ForegroundColor Red
    Write-Host "To fix this:"
    Write-Host "  1. Copy docs/PLAN.template.md to docs/PLAN.md"
    Write-Host "  2. Fill in your development task"
    Write-Host "  3. Run this script again"
    exit 1
}
$planSize = (Get-Item "docs/PLAN.md").Length
Write-Host "  OK: docs/PLAN.md found ($planSize bytes)."
```

代码审查证据：
- `grep -nE 'Copy-Item|copy\s+|New-Item.*PLAN|Set-Content.*PLAN'` → **无匹配**
- 脚本**仅检查 PLAN.md 是否存在**，获取文件大小用于显示
- 不读取 PLAN.md 内容（由 Claude 自行读取——[prompt 第 1 步](scripts/run-claude.ps1#L81)）
- 不自动生成、不自动填充模板

### 判定

- 按字面语义（"生成 PLAN.md"）：**不能**，脚本不产出任何 PLAN.md 内容
- 按设计意图（README 流程："你写下需求到 PLAN.md → 运行脚本"）：**符合预期**，用户手动编写
- 缺失提示清晰完整（3 步操作指引 → exit 1），无用户体验问题

**P2 级差异**——功能不存在，但与设计文档一致，不影响核心流程。

---

## 2. Claude 开发引导

### 结论：✅ 能正确引导（PASS）

### 指令链分析

[scripts/run-claude.ps1:76-102](scripts/run-claude.ps1#L76-L102) 的 prompt 结构：

| 步骤 | 指令 | 目标 |
|:---:|------|------|
| 1 | `Read docs/PLAN.md` to understand what needs to be done | 理解任务 |
| 2 | `Implement all changes` described in the plan by modifying code files | 写代码 |
| 3 | `Run any available tests` to verify your changes | 验证 |
| 4 | `Read docs/IMPLEMENTATION_REPORT.template.md` for the expected report format | 了解输出格式 |
| 5 | `Write a detailed implementation report` to docs/IMPLEMENTATION_REPORT.md | 写报告 |

报告中必须包含（L88-91）：
- 改动内容和原因
- 完整文件变更清单（路径、操作、描述）
- 执行的精确测试命令及完整输出
- 实施中遇到的问题
- 未能完成的计划项及原因

安全规则（L93-101）覆盖 7 个维度：

| 禁止项 | 行号 |
|--------|:---:|
| git commit, push, reset --hard, clean | L95 |
| 删除现有文件（修改可，删除不可） | L96 |
| 读取或输出任何 .env 文件内容 | L97 |
| 修改 .git 目录 | L98 |
| 开发 MCP / Web / 数据库 / 后台任务系统 | L99 |
| 多 Agent 并行 | L100 |
| 仅创建/修改实施计划直接需要的文件 | L101 |

### 已知不足

- prompt **不包含**对 `CODEX_REVIEW.md` 的读取指令——审查→修复闭环需用户手动介入
- 安全规则依赖 prompt 自然语言遵守（`--permission-mode bypassPermissions` 绕过工具层检查），README 已如实披露

### 判定

**PASS**——在"单次 Claude 实施"场景下，指令链完整、安全约束到位、输出要求明确。

---

## 3. Git Status 和 Git Diff 收集

### 结论：❌ 不自动收集（P1 — 阻碍自动审查闭环）

### 逐行分析

[scripts/run-claude.ps1:180-182](scripts/run-claude.ps1#L180-L182)：

```powershell
Write-Host " Please verify changes with:" -ForegroundColor Yellow
Write-Host "   git status --short --untracked-files=all" -ForegroundColor Yellow
Write-Host "   git diff" -ForegroundColor Yellow
```

grep 验证：
- `grep -nE 'git\s+(status|diff|log|show)' scripts/run-claude.ps1` → **仅在 Write-Host 字符串中出现**，不作为命令执行
- 脚本不调用 `git status`、不调用 `git diff`
- 不将 git 输出写入任何文件
- 不嵌入 LOG 或 REPORT

### 影响

Codex 审查时需**自行**运行 `git status` + `git diff`（或等用户手动提供），工具未准备任何变更摘要文件。这使审查步骤依赖外部 Git 环境，而非工具提供的结构化输入。

`CODEX_REVIEW.template.md` 注明审查应基于 `git status --short --untracked-files=all + git diff` 输出，但工具本身不产出这些数据。

### 改进方向

在 Claude 执行完毕后自动运行：
```powershell
git status --short --untracked-files=all > docs/CHANGES_STATUS.txt
git diff > docs/CHANGES_DIFF.txt
```

### 判定

**P1 级功能缺失**——不影响单次实施，但阻碍 Codex 审查的自动化（Codex 需从零运行 git 命令来获取变更上下文）。

---

## 4. CODEX_REVIEW.json 校验

### 结论：❌ 无此能力（P0 — 结构化审查链路完全缺失）

### 逐维度核查

| 维度 | 现状 | 证据 |
|------|------|------|
| `CODEX_REVIEW.json` 文件 | **不存在** | `find . -name "*.json"` → 仅 `demo-project/package.json` |
| JSON Schema 定义 | **不存在** | 无 `.schema.json`、无 schema 变量、无校验代码 |
| 校验/验证逻辑 | **不存在** | 脚本中无 `ConvertFrom-Json`、无 `Test-Json`、无 schema 比对 |
| 审查模板 | Markdown 格式 | `CODEX_REVIEW.template.md` 是自由文本模板 |
| 审查实例 | Markdown 格式 | `CODEX_REVIEW.md` 是人工编写的自由文本 |

### 为什么这是 P0

如果目标是"自动编排器"（本分支名称的暗示），则：
1. Codex 产出的审查报告**必须结构化**（JSON），脚本才能解析出"需修复的问题列表"
2. 必须定义 **JSON Schema**，确保 Codex 产出格式一致（严重程度、文件、行号等字段不缺失）
3. 必须有**校验逻辑**，拒绝格式不合规的审查报告，防止下游流程因解析失败而静默跳过

当前 Markdown 审查报告完全依赖人工阅读和人工决策，无法被脚本自动消费。

### 判定

**P0 级能力缺失**——结构化审查链路（JSON schema + 校验 + 自动解析）为零实现。

---

## 5. 循环次数限制

### 结论：❌ 无循环（P1 — 自动迭代能力缺失）

### 逐行分析

[scripts/run-claude.ps1](scripts/run-claude.ps1) 的完整控制流：

```
[1/4] Git 仓库检查 → [2/4] PLAN.md 检查 → [3/4] Claude CLI 检查 → [4/4] 运行 Claude → 结果摘要 → exit
```

grep 验证：
- `grep -nE '\b(while|for\s*\(|do\s*\{|until|loop)\b' -i` → 仅命中 `ForEach-Object`（L139：逐行输出 Claude 结果，非业务循环）
- `grep -nE '\$i\s*=|counter|round|iteration|max.*loop|max.*round'` → **无匹配**（上轮已验证）
- 无计数变量、无最大轮次配置、无终止条件判断
- 执行一次后直接 `exit $exitCode`（L190）

### 影响

缺少循环意味着：
- 无"Codex 审查 → Claude 修复 → 再审查"的自动闭环
- 每次迭代需用户手动触发
- 如果未来加入循环，**必须有一个 MAX_ROUNDS 硬上限**防止无限循环消耗 API

### 改进方向（参考设计）

```powershell
$MAX_ROUNDS = 5
for ($round = 1; $round -le $MAX_ROUNDS; $round++) {
    Write-Host "=== Round $round / $MAX_ROUNDS ==="
    # 1. 运行 Claude 实施/修复
    # 2. 自动收集 git status + git diff
    # 3. Codex 审查 → 生成结构化 CODEX_REVIEW.json
    # 4. 校验审查报告
    # 5. 如果无 P0/P1 → break
    # 6. 提取需修复项 → 构造修复 prompt → 回到步骤 1
}
```

### 判定

**P1 级能力缺失**——单次执行模型无法支持迭代式开发。分支名为 `feature/automatic-cli-orchestrator`，暗示循环是本分支目标功能，当前尚未实现。

---

## 6. Ctrl+C 退出行为

### 结论：⚠️ 依赖 PowerShell 默认行为（P3 — 当前可接受）

### 逐行分析

grep 验证：
- `grep -nE '\b(trap|finally|CancelKeyPress|TreatControlC|SIGINT|signal)\b'` → **无匹配**
- 脚本中**不存在**任何信号处理、清理块或自定义中断逻辑

### PowerShell 默认 Ctrl+C 行为分析

| 场景 | 预期行为 |
|------|----------|
| Ctrl+C 在 `claude -p` 执行期间 | PowerShell 向 claude 进程发 SIGINT；claude 终止 → `$LASTEXITCODE` 非零 → L183-184 打印错误 → exit |
| Ctrl+C 在 `Out-File` 期间 | PowerShell 终止当前管道；L14 `$ErrorActionPreference = "Stop"` 可能导致脚本直接退出 |
| Ctrl+C 在 `Write-Host` 期间 | 通常安全——不产生残留状态 |

**不需要清理的原因**：脚本不创建临时文件（`docs/claude-run.log` 每次覆盖写入，`docs/IMPLEMENTATION_REPORT.md` 运行前删除），不修改系统状态，无后台进程。

### 风险场景（未来）

如果加入循环和临时文件管理（如 `CHANGES_STATUS.txt`、`CODEX_REVIEW.json`），Ctrl+C 可能留下半写入文件。此时需要 `try/finally` 清理块。

### 判定

**P3 级**——对当前单次执行场景无影响。未来引入循环后建议补齐 `finally` 清理逻辑。

---

## 7. 危险 Git 命令

### 结论：✅ 不存在（PASS）

### 逐项核查

`grep -nE '\bgit\s+(commit|push|reset|clean|rm|checkout|rebase|merge|tag|stash|bisect|revert)\b'` 结果：

| 命令 | 脚本 | Prompt |
|------|:---:|:---:|
| `git commit` | 无 | 明确禁止（L95） |
| `git push` | 无 | 明确禁止（L95） |
| `git reset --hard` | 无 | 明确禁止（L95） |
| `git clean` | 无 | 明确禁止（L95） |
| `git rebase` | 无 | — |
| `git merge` | 无 | — |
| `git checkout` | 无 | — |
| `git rm` | 无 | — |
| `git stash` | 无 | — |
| `git bisect` | 无 | — |
| `git revert` | 无 | — |

脚本自身仅运行 **一条** git 命令：

```powershell
$gitCheck = git rev-parse --is-inside-work-tree 2>&1   # L30 — 纯只读
```

- 只读、零副作用、仅检查当前目录是否在 Git 仓库内
- 退出时不做任何 git 操作

### Claude 侧风险

Claude 可通过 `--permission-mode bypassPermissions` 自由执行 shell 命令，包括 git。防护完全依赖 prompt 指令（L95）。README 已如实说明此设计取舍。

### 判定

**PASS**——脚本层面零危险命令。Prompt 层有明确禁令（对个人本地使用场景风险可接受）。

---

## 8. .env 泄露风险

### 结论：✅ 不泄露（PASS）

### 防护层次验证

| 层次 | 机制 | 验证结果 |
|------|------|----------|
| **文件系统** | `.env` 文件是否存在于项目目录？ | `find . -name ".env*"` → **无匹配** |
| **.gitignore** | 是否忽略 `.env`？ | `.gitignore` L9-10：`.env` + `.env.*` 均已忽略 ✅ |
| **脚本层** | 脚本是否触碰 `.env`？ | `grep -nE '\.env' scripts/run-claude.ps1` → 仅 L97（prompt 中的禁令）✅ |
| **Prompt 层** | Claude 是否被禁止读取？ | L97：`DO NOT read or output the contents of any .env file` ✅ |

### 判定

**PASS**——四层防护完整。即使用户误将 `.env` 放入项目目录：
- `.gitignore` 阻止提交
- prompt 阻止 Claude 读取
- 脚本自身不触碰

---

## 9. 中文/空格路径兼容性

### 结论：⚠️ 理论正确、已验证、但编码链路脆弱（P2）

### 路径解析链路分析

[scripts/run-claude.ps1:16-19](scripts/run-claude.ps1#L16-L19)：

```powershell
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path   # L17
$ProjectDir = (Resolve-Path (Join-Path $ScriptDir "..")).Path  # L18
Set-Location $ProjectDir                                        # L19
```

| 步骤 | 操作 | Unicode 安全性 |
|------|------|:---:|
| `$MyInvocation.MyCommand.Path` | PowerShell 自动变量，获取脚本完整路径 | ✅ 原生支持 |
| `Split-Path -Parent` | PowerShell 内置 cmdlet | ✅ 原生支持 |
| `Join-Path ... ".."` | PowerShell 内置 cmdlet | ✅ 原生支持 |
| `Resolve-Path ...` | PowerShell 内置 cmdlet | ✅ 原生支持 |
| `.Path` 属性 | 显式转为 String（P2-1 修复） | ✅ 避免 PathInfo 隐式转换 |
| `Set-Location` | 切换当前目录 | ✅ 原生支持 |
| 后续相对路径（`"docs/PLAN.md"`） | 依赖当前目录 | ✅ PowerShell 内部处理正确 |

### 编码风险证据

`docs/claude-run.log` 历史记录中存在 em dash（`—`）被渲染为乱码 `鈥?` 的案例。根因是：
- `Out-File -Encoding UTF8` 在 Windows PowerShell 5.x 中生成 **带 BOM 的 UTF-8**
- 终端编码（通常 GBK/CP936）与 UTF-8 不匹配 → 显示乱码

这不影响路径解析的正确性，但暴露了整体编码链路的脆弱性——在中文 Windows + PowerShell 5.x 环境下，包含特殊字符的输出可能乱码。

### 验证状态

- `CODEX_REVIEW.md` 明确确认："中文路径和空格路径已用临时仓库验证可运行" ✅
- `README.md` 已添加建议："建议将项目放在纯英文路径中（不含中文、空格），以避免 PowerShell 编码问题" ✅

### 判定

**P2 级**——核心路径逻辑经过验证可正常工作。编码问题存在于输出/显示层面（非功能层面），当前文档警告充分。

---

## 10. 测试是否真实运行

### 结论：✅ 全部真实运行（PASS）

### 本轮审计独立重跑结果

| 测试项 | 命令 | 结果 | 退出码 |
|--------|------|------|:---:|
| Node.js demo | `node demo-project/test.js` | 4 passed, 0 failed | 0 |
| Python pytest | `py -B -m pytest demo-project -q -p no:cacheprovider` | 17 passed in 0.02s | 0 |
| PowerShell 语法 | `[System.Management.Automation.Language.Parser]::ParseFile(...)` | PASS: No parse errors | — |

所有测试**独立于任何预运行状态**可复现：
- `demo-project/` 是自包含测试项目，无需安装依赖（Node.js 使用零依赖手动测试框架，Python 仅依赖标准库 + pytest）
- 测试输出与 [TEST_REPORT.md](TEST_REPORT.md) 记录一致
- 测试结果不依赖环境变量、网络或外部服务

### 测试覆盖矩阵

| 被测目标 | 测试框架 | 用例数 | 覆盖内容 |
|----------|---------|:---:|------|
| `calculator.py` | pytest | 17 | add/subtract/multiply/divide + 正数/负数/零/浮点/除零异常 |
| `index.js` | 手动 assertEqual | 4 | add(正/负), subtract(正/负) |
| `run-claude.ps1` | PowerShell Parser | 语法层 | 全 191 行 AST 有效 |

### 判定

**PASS**——测试真实、可复现、结果一致。TEST_REPORT.md 中记录的 4 轮回归测试结论与独立重跑结果吻合。

---

## 补充发现（10 项检查之外）

### S1：`bypassPermissions` 的安全模型（P1 — 未来升级建议）

[scripts/run-claude.ps1:135](scripts/run-claude.ps1#L135) 使用 `--permission-mode bypassPermissions`，所有安全约束依赖 prompt 自然语言指令。README 已如实披露。

- **当前场景**（单次本地执行）：风险可接受
- **未来场景**（while 循环自动迭代）：建议升级为 `--allowedTools` 白名单或 `.claude/settings.json` 细粒度权限，防止 Claude 在修复循环中越权操作

### S2：空 PLAN.md 可通过检查（P3）

[scripts/run-claude.ps1:53](scripts/run-claude.ps1#L53) 仅检查文件大小（`.Length`），不验证内容。一份全空白或仅含模板占位符的 PLAN.md 也会通过检查。可考虑最小内容阈值（如 >50 字节且包含非模板关键词）。

### S3：Claude exit 0 ≠ 任务成功（设计上已知并已缓解）

[scripts/run-claude.ps1:162-178](scripts/run-claude.ps1#L162-L178) 在 exit 0 后追加验证 IMPLEMENTATION_REPORT.md 的存在性和非空性。但如果 Claude 生成一份"诚实但内容为空的报告"（如仅写"无法完成任务"），脚本仍会报告成功。当前无内容质量语义检查——不过这属于 AI 输出质量的开放问题，无法通过脚本层面完全解决。

### S4：`$ErrorActionPreference = "Stop"` 的边界影响（P3）

[scripts/run-claude.ps1:14](scripts/run-claude.ps1#L14) 设置全局 Stop 模式。如果 `Set-Location $ProjectDir`（L19）因意外原因失败，脚本立即终止且不输出友好错误。不过 `$ProjectDir` 由 `Resolve-Path` 计算，出现无效路径的概率极低。与 P1-2（claude 检测 try/catch）不同，此项实际风险可忽略。

---

## 与分支目标的差距矩阵

分支名 `feature/automatic-cli-orchestrator` 暗示的目标能力 vs 当前实现：

| 能力 | 当前状态 | 目标状态 | 差距 |
|------|:---:|:---:|------|
| 单次 Claude 实施 | ✅ 完成 | — | — |
| 自动收集 git status/diff | ❌ 仅打印建议 | 自动执行并写入文件 | 需新增 |
| CODEX_REVIEW JSON schema | ❌ 不存在 | 定义 + 校验 | 需新建 |
| Codex 审查自动触发 | ❌ 不存在 | Claude 实施后自动运行 | 需新建 |
| 审查 → 修复 while 循环 | ❌ 不存在 | 自动迭代直至无问题或达上限 | 需新建 |
| MAX_ROUNDS 硬限制 | ❌ 无循环故无限制 | 可配置轮次上限 | 需新建 |
| Ctrl+C 优雅退出 + 清理 | ⚠️ 默认行为 | finally 清理块 | 建议新增 |
| 工具层安全限制 | ❌ prompt 约束 | `--allowedTools` / settings.json | 建议升级 |

---

## 最终结论

**当前脚本是一个诚实的、最小可用的单次 CLI 工具。** 191 行 PowerShell 代码在以下方面表现出色：

- ✅ **错误处理**：3 层前置检查（Git/PLAN/Claude）+ 退出码正确捕获（P1-3 修复）+ 报告存在性/非空验证 → 退出码语义正确（0=成功, 1=环境问题, 3=Claude 未完成任务）
- ✅ **安全防护**：零危险 Git 命令 + .env 四层防护 + prompt 7 条安全规则
- ✅ **测试真实**：Node.js / Python / PowerShell 三层测试全部独立可复现
- ✅ **文档完整**：README + 模板 + 审计 trail + 测试报告

**但"自动编排"能力为零实现**——这正是本分支 `feature/automatic-cli-orchestrator` 的目标。差距集中在 4 项核心能力：

| 优先级 | 待实现 |
|:---:|------|
| **P0** | CODEX_REVIEW.json schema 定义 + 校验逻辑 |
| **P1** | 审查→修复 while 循环 + MAX_ROUNDS 上限 |
| **P1** | 自动收集 git status/diff 到文件 |
| **P2** | 安全模型升级为工具层强制限制 |
| **P3** | Ctrl+C 自定义处理 + finally 清理 |
