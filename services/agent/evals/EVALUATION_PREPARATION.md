# 评测准备结果（2026-09-10）

准备阶段没有调用付费模型；随后按授权完成首轮及两个指定 case 的追加一轮，结果见文末。保留旧 selected-20-v2 的 diff、expected 和历史成绩，另建两个本地版本化集合。

| 集合 | 样本 | 缺陷数 | 评测用途 |
|---|---:|---:|---|
| verified-pilot-v1 / development | 1 | 4 | 已调试项目的修订多缺陷回归，不作为未见测试 |
| verified-pilot-v1 / prospective | 4（含 2 clean） | 2 | 已知项目上的新变体/等价重构，分别统计 |
| verified-codec-holdout-v1 | 2 | 2 | 新仓库留出：Commons Codec 固定版本，建集时尚未评测 |

共 7 个 case、8 条通过行为对照的缺陷。两例 clean 都来自 CBOR 的正常代码重构：null 归一化改三元表达式、Map.put 返回值提取局部变量。它们不是空 diff，也不是只改注释；但不能代表所有正常 PR 的误报分布。

## 四项缺口的处理与边界

1. **标答**：实际 Java 基线/变异编译和行为验证，测试源不进入被审工作区。CBOR 原完整 patch 在未初始化 final 字段处编译失败，旧 E2 不再被视为可信运行时 NPE；修订多缺陷样本也隔离了未证明可达的 E5。旧 76 条的分级表在 pilot/legacy-label-audit.csv，60 条仍未逐项验证。绝不把登记数量说成已核验数量。
2. **语义评分**：新增 `evals.adjudication`。保留 matcher 原分数，另生成全 pending 的逐条复核表；要求报告内容、归档哈希、分母一致，正确/重复/错误/额外有效/越界分开。存在 pending 或争议时不生成最终分数。工具不能替代人或审计者的语义判断，也不宣称已完成独立人工复核。
3. **留出集**：Commons Codec 不在其它本地 case 的来源中。固定 commit 与两个新变体，现已完成首次模型评测；公开代码是否被预训练见过未知。CBOR 新变体只能算同项目 prospective，两者不能混称跨项目留出集。留出样本用于调参后应转开发集。
4. **正常变更**：两例 clean 编译并通过与基线一致的 7 项行为检查，另有求值顺序/调用次数等价性论证。若后续发现实际引入的问题，要新建数据版本纠正标签，不可静默改分。

## 已完成验证

- CBOR 基线 MAIN 全部编译、7 项 oracle 全 PASS；5 个变体全部编译，实际失败项与预设缺陷完全一致，两个 clean 全 PASS。
- Commons Codec 基线 MAIN 全部编译、6 项 oracle 全 PASS；2 个变体全部编译，失败项分别对应 null 消费契约与偏移范围。
- 两个集合的 diff、标答与 Git 基线树已冻结，通过 `evals.frozen_dataset` 完整性检查。
- Python 全量 495 项通过；离线语义评分新增 10 项测试，覆盖 pending、重复、输入篡改和 clean 标签矛盾；相关 Ruff 通过。

## 开始评测前

在 services/agent 中运行零成本完整性检查：

```powershell
conda run -n codeguard --no-capture-output python -m evals.frozen_dataset evals/dataset/verified-pilot-v1
conda run -n codeguard --no-capture-output python -m evals.frozen_dataset evals/dataset/verified-codec-holdout-v1
```

随后按明确授权的 case/轮次数运行模型。每种 profile 分开存档，固定模型与实际预算；先对照无工具与完整流程，不边测边改。两例留出缺陷和两例 clean，各 profile 一轮为 8 次审查，是小规模先导验证，不是正式大样本稳定性评测。

复核某个单轮归档：

```powershell
conda run -n codeguard --no-capture-output python -m evals.adjudication prepare <archive.json> <review.json>
# 逐条填写裁决、触发条件、证据与原因；没有自动替你填写“正确”。
conda run -n codeguard --no-capture-output python -m evals.adjudication score <archive.json> <review.json>
```

延迟、Token 与失败单列；规则分数与语义复核分数单列；开发/同项目新变体/新仓库留出单列。当前可写进简历的是“建立版本化样本、编译及行为验证、语义复核流程”，已有小样本规则分数，但不能声称稳定 Precision/Recall 或改善幅度。

数据在本地忽略目录；README、冻结记录和 oracle 验证记录位于各集合内。可复用评分代码、数据完整性检查及两个 Java oracle 位于 evals/，可正常版本控制。

## 2026-09-10：首轮与指定 case 复跑

固定 deepseek-v4-flash、单组工具预算 10/探索决策 6、task 工具预算 32，7 个 case 各跑一次，不增加外部评测 Judge。首轮共 8 个行为验证标答，最终报告检出 6 个；包括一个执行失败的端到端口径为 6/8，两条 clean 均无报告。总 Token 398,862（含缓存输入），Trace 合计 110.28 秒。开发回归 4/4、同仓库新缺陷变体 0/2、新仓库留出 2/2，不能合并宣传为未见集泛化成绩。

删除索引维护首轮先出现 4 次非法 JSON，最终候选引用 4 条观察超过上限 3，收口失败；第二轮检出 1/1，7.38 秒、20,459 Token。列表提前返回两轮 ReAct 均正确发现，但两轮 Judge 各两次响应均因 JSON 尾部多余闭合字符解析失败，最终均为 0/1；第二轮 7.87 秒、20,513 Token。复跑仅针对这两个 case，不能取最佳结果覆盖首轮或宣称总体第二轮成绩。

已知限制：Judge 格式失败仍会丢弃正确候选；一般范围说明也可能触发 inconclusive（首轮 10 组为 9 个 inconclusive、1 个 failed）；部分命中报告附加未证实的影响。没有无工具/源码/图谱对照，不证明图谱带来的增益，也不把跨轮波动当作成本优化。数据集、原始 Trace 与报告保留本地，未随仓库发布；公开这些数字仅作开发验证记录，尚非可独立复现的公开基准。
