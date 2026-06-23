目标是把当前项目从“扁平项目列表”升级成“Git 工作树控制台”：导入项目后自动识别同一个 Git 仓库下的主工作树和分支 worktree，左侧按主干展开分支；开发任务可以绑定到某个 worktree；Codex 审查 PASS 后，再由用户点击按钮触发受控的 Git 提交和合并。
实现顺序建议：
先增强后端 Git worktree 识别
当前项目已经有 worktreeType / branch / gitCommonDir / mainWorktreePath，下一步要补 repoId / mainProjectId / headSha / worktree列表，让同一个仓库的多个路径能分组。

再改项目导入逻辑
导入任意一个 Git 路径时，调用 git worktree list --porcelain，自动把同仓库的主工作树和 worktree 都登记到 .gui/projects.json。

再改前端项目列表
左侧不再平铺，而是按仓库显示树：主干节点可展开，下面是各分支/worktree 节点。任务仍然绑定到具体 worktree 项目 ID。

再加“新建工作树”能力
在主干节点或项目顶部加按钮，填写分支名和目录，后端执行受控 git worktree add -b ...，成功后自动刷新树。

再加 PASS 后提交
任务状态 PASS 后显示“一键提交”按钮。用户只填“Git 节点名/提交名”，后端检查 .env、dirty 状态、任务状态，然后执行 git add -A + git commit -m ...。

最后加一键合并主干
提交成功后显示“一键合并主干”。后端先检查主干工作树干净、目标分支存在、无冲突；无冲突才 merge，有冲突就拒绝并写入任务历史。

安全边界
Claude/Codex 仍然禁止 git commit / merge / push / reset / clean。提交和合并只能由 GUI 明确按钮触发；不自动 push；不自动删除 worktree；冲突不自动解决。