# Codeguard 审查质量评测报告

- 生成时间:2026-06-11 15:37:05
- Provider / Model:`openai` / `deepseek-v4-pro`
- 数据集:17 条(漏洞 12 / 干净 5)
- 重复跑测:3 次

> 这份报告是**阶段 1「无 Agent 基准版」的 baseline**。阶段 3 引入工具调用 Agent 后,
> 用同一数据集、同一脚本再跑一份,两份对比即 Agent 的价值证明(见 DECISIONS.md ADR-002)。

## 核心指标

| 指标 | 数值 | 含义 |
|---|---|---|
| **Precision** | 0.756 (±0.096) | 报出的问题里真问题占比(越高=噪音越少) |
| **Recall** | 0.944 (±0.079) | 该审出的问题被审出占比(越高=漏报越少) |
| **F1** | 0.840 | Precision 与 Recall 的调和平均 |
| 误报率(每条干净 diff) | 0.267 | 干净代码上平均误报几个(越低越好) |
| 定位准确率 | 1.000 | 命中项里行号也对上的比例 |
| 级别准确率 | 0.500 | 命中项里 severity 也对上的比例 |
| LLM-judge 描述质量 | 5.00 / 5 |
| LLM-judge 建议质量 | 4.97 / 5 |

## 逐用例明细(最后一次跑测)

| 用例 | 类别 | 标答 | 报告 | TP | FP | FN |
|---|---|---|---|---|---|---|
| clean_getter_001 | clean | 0 | 0 | 0 | 0 | 0 |
| clean_prepared_stmt_001 | clean | 0 | 0 | 0 | 0 | 0 |
| clean_logging_001 | clean | 0 | 1 | 0 | 1 | 0 |
| clean_rename_001 | clean | 0 | 0 | 0 | 0 | 0 |
| clean_unit_test_001 | clean | 0 | 0 | 0 | 0 | 0 |
| sql_injection_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| command_injection_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| path_traversal_001 | vuln | 1 | 0 | 0 | 0 | 1 |
| hardcoded_secret_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| weak_crypto_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| sensitive_log_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| insecure_deser_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| ssrf_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| xss_001 | vuln | 1 | 0 | 0 | 0 | 1 |
| missing_authz_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| insecure_random_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| xxe_001 | vuln | 1 | 1 | 1 | 0 | 0 |

## 怎么读这份报告

- **Recall 低**:漏报多,prompt 没覆盖到的漏洞类型,或模型没看懂上下文 —— 这正是阶段 3 工具调用要补的(让 Agent 自己去读相关文件)。
- **误报率高 / Precision 低**:噪音大,代码审查工具最致命的体验问题,对应阶段 2 的「误报过滤」。
- **定位准确率低**:`Issue.line` 不准,影响开发者定位,可考虑结合 diff 行号映射。
- **方差(±)大**:输出不稳定,温度过高或 prompt 不够约束。

_本报告由 `python -m evals.runner` 自动生成。_
