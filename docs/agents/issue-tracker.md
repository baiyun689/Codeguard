# Issue tracker: GitHub

本仓库的任务、需求规格和工作票据使用 GitHub Issues:
`https://github.com/baiyun689/Codeguard/issues`。

在仓库 clone 内使用 `gh` CLI;它会根据 `git remote` 自动识别仓库。

## 常用操作

- 创建:`gh issue create --title "..." --body "..."`
- 查看:`gh issue view <number> --comments`
- 列表:`gh issue list --state open --json number,title,body,labels,comments`
- 评论:`gh issue comment <number> --body "..."`
- 标签:`gh issue edit <number> --add-label "..."` 或 `--remove-label "..."`
- 关闭:`gh issue close <number> --comment "..."`

当 skill 要求“publish to the issue tracker”时,创建 GitHub Issue。
当 skill 要求“fetch the relevant ticket”时,运行
`gh issue view <number> --comments`。

## Pull Requests as a triage surface

**外部 Pull Request 不作为 triage 请求来源。**

`triage` 只处理 GitHub Issues,不把外部 PR 或协作者正在开发的 PR
放入需求分流队列。

## Wayfinder

`wayfinder` 使用一个带 `wayfinder:map` 标签的 Issue 作为总地图,
并用子 Issue 表示工作票据:

- 子票据标签使用 `wayfinder:<type>`,其中 type 为
  `research`、`prototype`、`grilling` 或 `task`。
- 优先使用 GitHub 原生 sub-issues 和 issue dependencies。
- 不支持原生关系时,在子 Issue 顶部写
  `Part of #<map>` 与 `Blocked by: #<n>`。
- 领取票据使用 `gh issue edit <n> --add-assignee @me`。
- 完成后先评论结论,再关闭 Issue,并把上下文链接补入地图的
  Decisions-so-far。
