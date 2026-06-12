# Codeguard 审查质量评测报告

- 生成时间:2026-06-12 16:49:24
- Provider / Model:`openai` / `deepseek-v4-pro`
- 数据集:27 条(漏洞 19 / 干净 8)
- 重复跑测:3 次

> 这份报告是**阶段 1「无 Agent 基准版」的 baseline**。阶段 3 引入工具调用 Agent 后,
> 用同一数据集、同一脚本再跑一份,两份对比即 Agent 的价值证明(见 DECISIONS.md ADR-002)。

## 核心指标

| 指标 | 数值 | 含义 |
|---|---|---|
| **Precision** | 0.401 (±0.032) | 报出的问题里真问题占比(越高=噪音越少) |
| **Recall** | 0.971 (±0.020) | 该审出的问题被审出占比(越高=漏报越少) |
| **F1** | 0.568 | Precision 与 Recall 的调和平均 |
| 误报率(每条干净 diff) | 0.667 | 干净代码上平均误报几个(越低越好) |
| 定位准确率 | 1.000 | 命中项里行号也对上的比例 |
| 级别准确率 | 0.642 | 命中项里 severity 也对上的比例 |

## 逐用例明细(最后一次跑测)

| 用例 | 类别 | 标答 | 报告 | TP | FP | FN |
|---|---|---|---|---|---|---|
| clean_getter_001 | clean | 0 | 0 | 0 | 0 | 0 |
| clean_prepared_stmt_001 | clean | 0 | 3 | 0 | 3 | 0 |
| clean_logging_001 | clean | 0 | 0 | 0 | 0 | 0 |
| clean_rename_001 | clean | 0 | 0 | 0 | 0 | 0 |
| clean_unit_test_001 | clean | 0 | 0 | 0 | 0 | 0 |
| clean_try_with_resources_001 | clean | 0 | 1 | 0 | 1 | 0 |
| clean_logged_exception_001 | clean | 0 | 1 | 0 | 1 | 0 |
| clean_bounded_loop_001 | clean | 0 | 0 | 0 | 0 | 0 |
| sql_injection_001 | vuln | 3 | 6 | 3 | 3 | 0 |
| command_injection_001 | vuln | 1 | 5 | 1 | 4 | 0 |
| path_traversal_001 | vuln | 1 | 2 | 1 | 1 | 0 |
| hardcoded_secret_001 | vuln | 1 | 2 | 1 | 1 | 0 |
| weak_crypto_001 | vuln | 1 | 3 | 1 | 2 | 0 |
| sensitive_log_001 | vuln | 1 | 2 | 1 | 1 | 0 |
| insecure_deser_001 | vuln | 1 | 6 | 1 | 5 | 0 |
| ssrf_001 | vuln | 1 | 3 | 1 | 2 | 0 |
| xss_001 | vuln | 1 | 5 | 1 | 4 | 0 |
| missing_authz_001 | vuln | 1 | 3 | 1 | 2 | 0 |
| insecure_random_001 | vuln | 1 | 3 | 1 | 2 | 0 |
| xxe_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| npe_map_get_001 | vuln | 1 | 3 | 1 | 2 | 0 |
| resource_leak_001 | vuln | 1 | 2 | 1 | 1 | 0 |
| off_by_one_001 | vuln | 1 | 2 | 1 | 1 | 0 |
| concurrent_modification_001 | vuln | 1 | 2 | 1 | 1 | 0 |
| swallowed_exception_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| hardcoded_config_001 | vuln | 2 | 2 | 2 | 0 | 0 |
| magic_number_001 | vuln | 2 | 2 | 2 | 0 | 0 |

## 规则尺 vs 裁判尺(最后一次跑测)

主判为 LLM 裁判(语义配对),规则尺并行作确定性交叉校验。下表只列两尺判定不一致的用例;共 0 条分歧。分歧多说明规则尺的关键词匹配偏差大(裁判在纠偏);分歧为 0 则两尺一致,可放心用规则尺做廉价回归。

| 用例 | 裁判 TP/FP/FN | 规则 TP/FP/FN |
|---|---|---|

## 级别诊断(最后一次跑测)

只统计标了期望级别的命中项(漏报项不计)。共 23 项,其中 7 项级别判错(✗)。

| 用例 | 类型 | 期望级别 | 报告级别 | 判定 |
|---|---|---|---|---|
| sql_injection_001 | SQL注入 | CRITICAL | CRITICAL | ✓ |
| sql_injection_001 | 资源泄漏 | WARNING | CRITICAL | ✗ |
| sql_injection_001 | 空指针 | WARNING | CRITICAL | ✗ |
| command_injection_001 | 命令注入 | CRITICAL | CRITICAL | ✓ |
| path_traversal_001 | 路径穿越 | CRITICAL | CRITICAL | ✓ |
| hardcoded_secret_001 | 硬编码凭证 | CRITICAL | CRITICAL | ✓ |
| weak_crypto_001 | 弱加密算法 | WARNING | WARNING | ✓ |
| sensitive_log_001 | 敏感信息泄露 | WARNING | CRITICAL | ✗ |
| insecure_deser_001 | 不安全反序列化 | CRITICAL | CRITICAL | ✓ |
| ssrf_001 | SSRF（服务端请求伪造） | WARNING | WARNING | ✓ |
| xss_001 | 跨站脚本(XSS) | WARNING | CRITICAL | ✗ |
| missing_authz_001 | 鉴权缺失 | CRITICAL | CRITICAL | ✓ |
| insecure_random_001 | 不安全的随机数 | WARNING | WARNING | ✓ |
| xxe_001 | XXE 漏洞 | WARNING | WARNING | ✓ |
| npe_map_get_001 | 空指针风险 | WARNING | WARNING | ✓ |
| resource_leak_001 | 资源泄漏 | WARNING | CRITICAL | ✗ |
| off_by_one_001 | 数组越界 | CRITICAL | CRITICAL | ✓ |
| concurrent_modification_001 | 并发修改异常 | CRITICAL | CRITICAL | ✓ |
| swallowed_exception_001 | 异常吞没 | WARNING | WARNING | ✓ |
| hardcoded_config_001 | 硬编码 | INFO | CRITICAL | ✗ |
| hardcoded_config_001 | 硬编码数据库凭证 | CRITICAL | CRITICAL | ✓ |
| magic_number_001 | 魔法数字 | INFO | WARNING | ✗ |
| magic_number_001 | 空指针 | WARNING | WARNING | ✓ |

## 怎么读这份报告

- **Recall 低**:漏报多,prompt 没覆盖到的漏洞类型,或模型没看懂上下文 —— 这正是阶段 3 工具调用要补的(让 Agent 自己去读相关文件)。
- **误报率高 / Precision 低**:噪音大,代码审查工具最致命的体验问题,对应阶段 2 的「误报过滤」。
- **定位准确率低**:`Issue.line` 不准,影响开发者定位,可考虑结合 diff 行号映射。
- **方差(±)大**:输出不稳定,温度过高或 prompt 不够约束。

_本报告由 `python -m evals.runner` 自动生成。_
