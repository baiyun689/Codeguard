# Graph Necessity v1

这是一个受控的图谱必要性评测集，不计入默认 `evals.dataset.load_cases()`。
调用 `load_cases(Path("evals/dataset/graph-necessity-v1"))` 显式加载。

当前从真实 Spring Retry 工程快照派生一个 case，包含两个只靠 diff 无法充分确认的 Behavior 缺陷：

- `RetryTemplate.doExecute` 在 open listener 之后才注册同步上下文；
- stateful retry cache 命中时丢弃缓存上下文并重新创建上下文。

`case.yaml` 中的 `required_graph_facts` 是评测 oracle，要求候选证据引用调用关系或状态生命周期事实。
`repo/` 是安全基线提交后植入缺陷的工程快照，`changes.diff` 是唯一审查输入。
