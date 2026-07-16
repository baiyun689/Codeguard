# 普通审查全覆盖与 ReAct Task 预算设计

**日期**：2026-07-16  
**状态**：已实施

## 目标

将“是否审查 task”和“是否使用昂贵 ReAct”拆成两个独立决策：普通 diff 默认审查全部 task，
只限制 ReAct 数量；超大 diff 才裁剪 task 范围。

## 模式判定

- 大 diff 的唯一判定条件为原始 diff 行数超过 5000。
- task 数量不再参与大 diff 判定。
- 大 diff 继续按风险最多选择 20 个 task、每文件最多 3 个、每 task 上下文最多 2000 字符。
- 普通 diff 默认选择全部 task，不应用原来的总任务 100、每文件 10 的裁剪。
- 用户显式配置的任务总量/单文件预算只作为大 diff 降级的更严格上限，不裁剪普通 diff。

## ReAct 预算

新增 `CODEGUARD_MAX_REACT_TASKS`，默认 20，只允许正整数。

风险画像先按现有规则判断 ReAct 资格：任一具体标签得分达到 2 的 task 有资格；
`GENERAL_REVIEW`、纯弱信号和缺失画像的 task 只能走 Direct。所有已选 task 沿用 TaskRank 的
稳定风险顺序，前 20 个有资格的 task 获得 ReAct，其余 task 降级为 Direct，但不被跳过。

ReAct 预算按 task 计数，不按 reviewer 实际调用次数计数。同一个 task 路由到的所有 reviewer
使用相同 tier，避免一个 task 内出现部分 ReAct、部分 Direct。

没有配置 Java 工具服务时，所有 task 都使用 Direct；不得把 Direct 执行标记为 ReAct，也不得因
“ReAct 空结果兜底”造成重复 Direct 调用。

## 模块与数据流

`large_diff_policy` 继续作为模式和 task 选择预算的唯一模块。普通模式返回无限 task 选择预算，
但保留普通上下文预算；大 diff 返回现有 20/3/2000 上限。

`risk_routing` 增加一个确定性批量 tier 规划接口，输入稳定顺序的 selected task IDs、风险画像、
ReAct task 上限和工具可用性，输出 task ID 到 `react/direct` 的映射。现有单 task 风险判断保留为
内部资格规则。Reviewer 节点重复派生同一映射，不新增顶层 ReviewState 字段。

```text
diff
  → build task / risk triage / stable rank
  → 大 diff：选前20（每文件3）
    普通 diff：选择全部
  → 在选中范围内分配最多20个ReAct task
  → 其余task全部Direct
  → Summary / Context / Discover / Evidence / Judge
```

EvidencePlanner 的证据 gate 仍按候选风险和证据策略决定，不受发现者 ReAct 配额直接限制；这样
Direct 发现的高价值候选仍可进入受约束取证。

## 兼容与可观测性

- `CODEGUARD_MAX_REVIEW_TASKS`、`CODEGUARD_MAX_TASKS_PER_FILE` 保留，但文档明确为大 diff 更严格上限。
- trace 在 TaskRank 记录普通全选或大 diff 裁剪，在 reviewer trace 中记录最终 tier；不新增高基数指标。
- `ReviewResult`、`Issue`、Java 工具协议和数据库结构保持不变。

## 测试

必须覆盖：

1. 5001 行进入大 diff，任意 task 数量但不超过 5000 行均为普通模式。
2. 普通模式单文件超过 10 个 hunk 时仍全部选中。
3. 大 diff 继续执行 20/3/2000 降级，且用户更严格上限仍生效。
4. 25 个 ReAct 资格 task 中只有风险排序前 20 个为 ReAct，其余为 Direct。
5. 低风险和 `GENERAL_REVIEW` 不消耗 ReAct 名额。
6. 同一 task 的所有 reviewer 获得相同 tier。
7. 无工具模式全部 Direct，空结果不产生第二次 Direct 调用。
8. 配置默认值、合法覆盖和非法值校验。

全量交付继续运行 Python pytest、Ruff、mypy 和 Java `mvn verify`。
