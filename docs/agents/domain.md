# Domain Docs

本仓库采用 **single-context** 领域文档布局。Python Agent 与 Java
Gateway 属于同一个 Codeguard 产品上下文,共享术语和架构边界。

## 探索代码前

按以下顺序读取与当前任务相关的内容:

1. 根目录 `CONTEXT.md`——领域术语与统一语言;不存在时静默继续。
2. 根目录 `DECISIONS.md`——本项目现有 ADR 总账。
3. `CLAUDE.md`——项目心智模型、开发约束和运行方式。

不要仅因 `CONTEXT.md` 尚不存在就提前创建空文件。
`domain-modeling`、`grill-with-docs` 或
`improve-codebase-architecture` 在形成真实术语或决策时按需创建和更新。

## 布局

```text
Codeguard/
├── CONTEXT.md       # 按需创建:领域术语与 glossary
├── DECISIONS.md     # 现有 ADR 总账
├── CLAUDE.md        # 项目约束
└── docs/agents/     # Agent skills 的消费规则
```

本仓库不另建重复的 `docs/adr/`;架构决策继续追加到 `DECISIONS.md`。

## 使用统一术语

Issue 标题、规格、重构建议、假设和测试名称应优先使用
`CONTEXT.md` 定义的术语。不要漂移到 glossary 明确排除的同义词。

若需要的概念尚未定义,先判断它是项目之外的措辞,还是领域模型的真实缺口。
真实缺口交给 `domain-modeling` 处理。

## ADR 冲突

如果提议与 `DECISIONS.md` 中的 ADR 冲突,必须明确指出,不能静默覆盖:

> 与 ADR-032 的确定性 ReviewCouncil 外层拓扑冲突;若仍需修改,
> 应先说明重新打开该决策的原因。
