# Codex Review Round 1

审查日期：2026-06-09
审查范围：`git status`、`git diff`、README、核心脚本、demo 源码、全部测试、测试报告。
工作区状态：`M docs/CODEX_REVIEW.template.md`，`?? docs/CLAUDE_SELF_REVIEW.md`，`?? docs/FIX_REPORT_ROUND_1.md`。
验证结果：`node demo-project\test.js` 通过 4/4；`py -B -m pytest demo-project -q -p no:cacheprovider` 通过 17/17；PowerShell 语法检查通过。

## P0

### P0-1：目标闭环流程没有实现

证据：`scripts/run-claude.ps1` 只有 4 步：检查 Git、检查 PLAN、检查 Claude、运行 Claude。没有生成 PLAN、没有收集 git diff/test 作为结构化输入、没有调用 Codex、没有 Claude 修复轮次、没有 PASS/max rounds。

修复：实现显式状态机：`PLAN_GENERATED -> CLAUDE_IMPLEMENTED -> TESTED -> DIFF_COLLECTED -> CODEX_REVIEWED -> CLAUDE_FIXED -> PASS/FAIL/MAX_ROUNDS`。

### P0-2：测试失败可能被错误判定成功

证据：`scripts/run-claude.ps1` 只要求 Claude exit code 为 0 且 `IMPLEMENTATION_REPORT.md` 非空。若 Claude 在报告里写“测试失败”，脚本仍可能 exit 0。

修复：由编排器实际运行测试命令，记录退出码；任何测试非 0 必须进入修复轮或最终 FAIL，不能依赖 Claude 自述。

### P0-3：`CODEX_REVIEW.json` 无效时没有失败路径

证据：仓库没有 `CODEX_REVIEW.json`、schema、`ConvertFrom-Json`、`Test-Json` 或解析逻辑。当前只有 Markdown 审查。

修复：定义 JSON schema，例如 `{status, findings[], severity, file, line, fix}`；Codex 输出必须校验，JSON 缺失/无效/不可解析一律 FAIL 或重试，不允许 PASS。

## P1

### P1-1：不会自动收集 git status/diff

证据：`scripts/run-claude.ps1` 只是打印建议命令，不执行 `git status` / `git diff`。

修复：Claude 后自动写入 `docs/CHANGES_STATUS.txt` 和 `docs/CHANGES_DIFF.txt`，未跟踪文件也要单独读取或 `git add -N` 后 diff。

### P1-2：脏工作区检查不可靠

证据：当前工作区已经有修改和未跟踪文件，但脚本只检查是否在 Git 仓库内。后续 diff 会混入历史/用户改动。

修复：运行前记录 baseline，或要求 clean worktree；至少把 pre/post status 分开保存，并只审查本轮新增变化。

### P1-3：安全限制主要靠 prompt，`bypassPermissions` 放大风险

证据：`scripts/run-claude.ps1` 使用 `--permission-mode bypassPermissions`。脚本自身没有 commit/push/reset/clean，但 Claude 侧只靠 prompt 禁止。

修复：使用 `--allowedTools` / `--disallowedTools` 或 `.claude/settings.json` 工具层限制；显式禁止危险 git、`.env`、`.git`、删除操作。

### P1-4：日志可能包含密钥

证据：`scripts/run-claude.ps1` 将 Claude 全量输出写入 `docs/claude-run.log`。若 Claude 误读/输出 secret，会落盘。

修复：日志写入前做 secret redaction；禁止记录 `.env` 内容；对 token/key 模式做扫描并中止。

## P2

### P2-1：README 与实际/目标流程不一致

证据：`README.md` 写“主 AI 制定计划 -> Claude Code 实施 -> Codex 审查”，但工作流图只到生成实施报告；脚本也不调用 Codex。

修复：README 分清“当前最小版”和“目标自动闭环版”，避免让用户误以为已自动审查。

### P2-2：Ctrl+C 后可能留下半成品状态

证据：无 `trap` / `finally` / 进程句柄清理；并且运行前会删除旧报告。

修复：用 `try/finally` 写中断状态；保留旧报告备份；对子进程使用可控 process handle，必要时终止子进程树。

### P2-3：测试覆盖没有覆盖编排器

证据：现有测试只覆盖 demo calculator；没有 Pester/伪 Claude 测试覆盖失败退出码、测试失败、无效 JSON、最大轮次、脏工作区。

修复：添加编排器级测试，用 fake `claude`、fake `codex` 和临时 git repo 覆盖关键状态。

### P2-4：Windows/中文/空格路径仍有编码风险

证据：路径处理本身较稳，但 README 明确建议纯英文路径；历史文档曾出现乱码。`Out-File -Encoding UTF8` 在 Windows PowerShell 5.1 会产生 BOM。

修复：在含中文和空格路径的临时仓库中加入自动测试；统一使用显式 UTF-8 编码策略。

### P2-5：测试报告局部陈旧

证据：`docs/TEST_REPORT.md` 的文件完整性列表仍是早期文件集，未包含当前新增的 calculator、报告等。

修复：重新生成测试报告，不要混合历史结论和当前结论。

## P3

### P3-1：PLAN 只检查存在和大小

证据：`scripts/run-claude.ps1` 不校验是否仍是模板或空任务。

修复：检查非模板内容、最小长度和必填字段。

### P3-2：核心源码无 `shell=True` / 命令注入迹象

结论：未发现 Python `shell=True`、`subprocess` 或 PowerShell `Invoke-Expression`。当前 native command 调用未拼接用户输入，风险低。

### P3-3：未发现核心伪代码/TODO

结论：核心脚本没有 TODO/伪实现；模板中的 `[自动填入]`、`[例如]` 属于模板占位，合理。

## 总结

当前项目是一个“单次 Claude 执行器”，不是目标描述中的自动 AI 开发 CLI 编排器。最危险的问题不是现有 demo 测试是否通过，而是成功判定、Codex JSON 审查、测试失败处理、循环上限和安全边界都没有真正实现。
