# Codex Review Round 2

审查日期：2026-06-09
结论：NEEDS_FIX

## 审查范围

- 当前 `git status --short --untracked-files=all`
- `git diff`
- `README.md`
- `scripts/run-claude.ps1`
- `scripts/test-orchestrator.ps1`
- `.claude/settings.json`
- `docs/CODEX_REVIEW.schema.json`
- `docs/TEST_REPORT.md`
- `docs/CODEX_REVIEW_ROUND_1.md`

## 实际验证

已运行：

```powershell
git status --short --untracked-files=all
git diff --stat
git diff -- .
claude --version
node demo-project\test.js
py -B -m pytest demo-project -q -p no:cacheprovider
[System.Management.Automation.Language.Parser]::ParseFile(...) # run-claude.ps1
[System.Management.Automation.Language.Parser]::ParseFile(...) # test-orchestrator.ps1
powershell -ExecutionPolicy Bypass -File scripts\test-orchestrator.ps1
```

结果：

- Claude CLI 可用：`2.1.132 (Claude Code)`
- Node demo 测试通过：4 passed, 0 failed
- Python pytest 通过：17 passed in 0.02s
- 两个 PowerShell 脚本语法检查通过
- `scripts/test-orchestrator.ps1` 报告通过，但其关键测试多为正则/字符串存在性检查，未覆盖真实成功路径

未直接在当前仓库运行完整 `scripts/run-claude.ps1`，因为它会调用真实 Claude 并写入/删除运行产物。为避免破坏当前工作区，改用静态审查和局部 PowerShell 行为验证。关键阻塞问题可被独立复现：

```powershell
$MaxRounds=3
$iterations=0
for ($round = 1; $round -le $MAX_ROUNDS; $round++) { $iterations++ }
"MaxRounds=$MaxRounds MAX_ROUNDS='$MAX_ROUNDS' Iterations=$iterations"
```

输出：

```text
MaxRounds=3 MAX_ROUNDS='' Iterations=0
```

这证明当前主循环变量会导致 0 次迭代。

## P0

### P0-1：主 CLI 编排循环实际不会执行

状态：未修复，并引入阻塞回归。

证据：

- `scripts/run-claude.ps1` 参数定义为 `$MaxRounds`
- 主流程显示、日志、循环和失败判断使用 `$MAX_ROUNDS`
- PowerShell 变量名不把下划线视为同一个变量，`$MAX_ROUNDS` 未赋值
- `for ($round = 1; $round -le $MAX_ROUNDS; $round++)` 在 `$MAX_ROUNDS` 为空时执行 0 次
- 随后 `$finalResult` 从 `UNKNOWN` 变为 `FAIL_UNKNOWN`，最终 exit 5

影响：

完整 CLI 成功路径不可用。Claude 不会被调用，测试不会运行，git status/diff 不会在 Claude 后收集，修复循环也不会发生。

修复方法：

统一变量名。最小修复：

```powershell
$MAX_ROUNDS = $MaxRounds
```

放在 `param(...)` 之后，或把所有 `$MAX_ROUNDS` 改为 `$MaxRounds`。同时新增一个真实执行测试，断言 `-MaxRounds 1` 时至少调用一次 fake `claude`。

### P0-2：`CODEX_REVIEW.json` 校验没有接入主流程

状态：部分修复但仍未满足 Round 1 要求。

证据：

- 新增了 `docs/CODEX_REVIEW.schema.json`
- 新增了 `Test-CodexReviewJson` 函数
- 但主流程没有调用 `Test-CodexReviewJson`
- 没有生成、读取或消费 `docs/CODEX_REVIEW.json`
- `README.md` 也承认 Codex 自动审查和 Codex -> Claude 闭环仍需手动

影响：

Round 1 的“`CODEX_REVIEW.json` 无效时不得错误 PASS”仍没有真实业务路径。因为 JSON 审查结果完全不参与 PASS/FAIL 判定，所以无效 JSON 不会阻止 PASS。

修复方法：

在测试通过和最终 PASS 之前加入审查阶段：

1. 生成或要求存在 `docs/CODEX_REVIEW.json`
2. 调用 `Test-CodexReviewJson`
3. JSON 缺失、无效、字段非法、状态不是 `PASS` 时不得返回 PASS
4. `NEEDS_FIX` 时进入 Claude 修复轮；`FAIL` 时退出非 0

### P0-3：新增测试没有覆盖真实成功路径

状态：未修复。

证据：

- `scripts/test-orchestrator.ps1` 对 MAX_ROUNDS 的测试只检查文本中同时出现 `MaxRounds` 和 `MAX_ROUNDS`
- 该测试没有执行主脚本成功路径，因此没有发现 `$MAX_ROUNDS` 空变量
- 多个测试只检查字符串存在，例如 `CHANGES_STATUS`、`CHANGES_DIFF`、`try/finally`、`FAILING TESTS`
- 测试汇总显示 `Total: 16`，`Passed: 17`，说明测试计数本身不可靠

影响：

测试报告给出 PASS，但不能证明编排器真实可用，会掩盖主流程阻塞错误。

修复方法：

使用临时 git repo + fake `claude` 命令做端到端测试：

- fake `claude --version` 返回 0
- fake `claude -p ...` 写入 `docs/IMPLEMENTATION_REPORT.md`
- 运行 `scripts/run-claude.ps1 -MaxRounds 1`
- 断言 exit 0、fake claude 被调用一次、测试被执行、`CHANGES_STATUS.txt` 和 `CHANGES_DIFF.txt` 被生成

## P1

### P1-1：测试命令执行引入 shell 注入风险

状态：新引入问题。

证据：

```powershell
$testOutput = & cmd /c "$testCmd 2>&1" 2>&1
```

`$testCmd` 来自 `docs/PLAN.md` 和自动发现逻辑，其中 PLAN.md 是用户输入。通过 `cmd /c` 拼接字符串执行，等价于 shell 字符串执行，可能被 `&`、`|`、`&&`、重定向等扩展为额外命令。

影响：

违反 Round 1 对命令注入的安全要求。即使这是本地工具，也会让 PLAN.md 中的测试命令具备任意 shell 执行能力。

修复方法：

不要用 `cmd /c` 执行未解析字符串。建议：

- 对支持的测试命令做白名单枚举，如 pytest、node、npm test
- 使用参数数组调用，例如 `& py -B -m pytest demo-project -q -p no:cacheprovider`
- 如果必须允许自定义命令，明确标注为不安全模式并默认关闭

### P1-2：工具层安全限制声明强于实际保证

状态：部分修复但不足。

证据：

- 新增 `.claude/settings.json`
- 但脚本仍使用 `claude -p --permission-mode bypassPermissions`
- 代码注释写“Use --disallowedTools”，实际命令没有传 `--disallowedTools`
- `.claude/settings.json` 的 allow 列表包含 `Task`、`WebSearch`、`WebFetch`，与 prompt 中“不要多 Agent / Web”方向冲突

影响：

README 说“工具层强制拦截危险操作”，但当前实现没有被本轮实际验证。`bypassPermissions` 是否仍尊重项目 settings 需要端到端验证；allow Web/Task 也扩大了工具面。

修复方法：

- 明确验证 Claude Code 在 `bypassPermissions` 下仍应用 `.claude/settings.json` deny
- 或在 CLI 命令中显式传入 `--disallowedTools`
- 移除不需要的 `Task`、`WebSearch`、`WebFetch` allow
- 新增测试：让 fake/受控 Claude 尝试危险命令，确认被拒绝

### P1-3：无测试时仍可返回 PASS

状态：新引入问题。

证据：

```powershell
if ($TestCommands.Count -eq 0) {
    Write-Log "No test commands configured. Marking as PASS (no tests to run)." "Yellow"
    $finalResult = "PASS_NO_TESTS"
    break
}
```

影响：

如果自动发现失败，CLI 会返回 exit 0。这会把“没有验证”误判成 PASS，与“测试失败不能误判 PASS”的修复目标相冲突。

修复方法：

默认应为 `NEEDS_MANUAL_VERIFY` 或非 0，除非用户显式传 `-AllowNoTests`。`-SkipTests` 也不应显示 `PASS`，应显示 `SKIPPED` 并返回专用退出码或要求人工确认。

## P2

### P2-1：脏工作区基线只记录，不隔离

状态：部分修复。

证据：

脚本会写 `docs/BASELINE_STATUS.txt`，但后续 `CHANGES_DIFF.txt` 仍直接保存完整 `git diff`。当前工作区已经有大量历史/未跟踪变更，脚本只提示“may include them”，没有隔离本轮变化。

影响：

Codex 审查仍可能混入本轮之前的改动。对实际使用不是绝对阻塞，但会降低审查可靠性。

修复方法：

运行前保存 baseline 文件列表和 blob hash；运行后生成“新增/修改于本轮”的差异清单。或者要求 clean worktree 后才运行，提供 `-AllowDirty` 覆盖。

### P2-2：Ctrl+C 处理仍不完整

状态：部分修复。

证据：

已有 `try/finally`，但没有 `trap`、`Console.CancelKeyPress` 或显式子进程句柄管理。`claude` 被同步调用，Ctrl+C 时是否终止子进程树未验证。

影响：

比 Round 1 有改善，但不能证明 Ctrl+C 后无残留进程。

修复方法：

使用 `Start-Process -PassThru` 或 PowerShell job 管理子进程，捕获 Ctrl+C 时终止 Claude 进程树并写入中断状态。

### P2-3：README 宣称能力过满

状态：部分修复但仍有误导。

证据：

README 当前版本表写“自动运行测试”“测试失败自动修复循环”“结构化 JSON 审查 schema”“密文检测”“工具层安全限制”为当前 v1.0 已具备。但本轮确认主循环不会执行，JSON 审查未接入，测试命令有注入风险，安全限制未验证。

影响：

用户会误以为 CLI 已可稳定使用。

修复方法：

在修复主流程前，把这些能力标为“开发中”或“部分实现，未验收”。

## Round 1 修复状态汇总

| Round 1 问题 | 本轮结论 |
|---|---|
| P0-1 目标闭环流程没有实现 | 仍未正确修复；新增循环因 `$MAX_ROUNDS` bug 不执行 |
| P0-2 测试失败可能误判成功 | 部分修复；但无测试时仍 PASS，且主循环不执行 |
| P0-3 无效 `CODEX_REVIEW.json` 可能误判 PASS | 未修复；schema/函数存在但未接入 |
| P1-1 不会自动收集 git status/diff | 代码中有实现，但因主循环不执行而实际不可用 |
| P1-2 脏工作区检查不可靠 | 部分修复；只记录 baseline，未隔离 |
| P1-3 安全限制靠 prompt | 部分修复；settings 存在但未验证，allow 面过宽 |
| P1-4 日志可能包含密钥 | 部分修复；有脱敏函数，但需真实日志测试 |
| P2-1 README 不一致 | 部分修复；但现在又过度声明未验收能力 |
| P2-2 Ctrl+C 半成品 | 部分修复；try/finally 有了，进程清理未验证 |
| P2-3 测试未覆盖编排器 | 未修复；新增测试多数不是行为测试 |
| P2-4 Windows/中文/空格路径风险 | 部分修复；编码策略有改善，路径测试未真实覆盖 |
| P2-5 测试报告陈旧 | 有更新，但报告结论被不充分测试支撑 |

## 最终结论

NEEDS_FIX

原因：

- 仍存在 P0：主 CLI 循环因 `$MAX_ROUNDS` 未赋值而不会执行
- 仍存在 P0：`CODEX_REVIEW.json` schema 校验未接入 PASS/FAIL 状态机
- 仍存在 P1：测试命令通过 `cmd /c` 执行，存在命令注入风险
- 新增测试通过但覆盖不足，未发现上述阻塞问题

在这些问题修复前，不能给 PASS。
