# Codeguard 审查质量评测报告

- 生成时间:2026-06-11 19:48:47
- Provider / Model:`openai` / `deepseek-v4-pro`
- 数据集:27 条(漏洞 19 / 干净 8)
- 重复跑测:1 次

> 这份报告是**阶段 1「无 Agent 基准版」的 baseline**。阶段 3 引入工具调用 Agent 后,
> 用同一数据集、同一脚本再跑一份,两份对比即 Agent 的价值证明(见 DECISIONS.md ADR-002)。

## 核心指标

| 指标 | 数值 | 含义 |
|---|---|---|
| **Precision** | 0.297 (±0.000) | 报出的问题里真问题占比(越高=噪音越少) |
| **Recall** | 1.000 (±0.000) | 该审出的问题被审出占比(越高=漏报越少) |
| **F1** | 0.458 | Precision 与 Recall 的调和平均 |
| 误报率(每条干净 diff) | 1.125 | 干净代码上平均误报几个(越低越好) |
| 定位准确率 | 1.000 | 命中项里行号也对上的比例 |
| 级别准确率 | 0.684 | 命中项里 severity 也对上的比例 |

## 逐用例明细(最后一次跑测)

| 用例 | 类别 | 标答 | 报告 | TP | FP | FN |
|---|---|---|---|---|---|---|
| clean_getter_001 | clean | 0 | 0 | 0 | 0 | 0 |
| clean_prepared_stmt_001 | clean | 0 | 4 | 0 | 4 | 0 |
| clean_logging_001 | clean | 0 | 1 | 0 | 1 | 0 |
| clean_rename_001 | clean | 0 | 0 | 0 | 0 | 0 |
| clean_unit_test_001 | clean | 0 | 0 | 0 | 0 | 0 |
| clean_try_with_resources_001 | clean | 0 | 2 | 0 | 2 | 0 |
| clean_logged_exception_001 | clean | 0 | 1 | 0 | 1 | 0 |
| clean_bounded_loop_001 | clean | 0 | 1 | 0 | 1 | 0 |
| sql_injection_001 | vuln | 1 | 5 | 1 | 4 | 0 |
| command_injection_001 | vuln | 1 | 5 | 1 | 4 | 0 |
| path_traversal_001 | vuln | 1 | 3 | 1 | 2 | 0 |
| hardcoded_secret_001 | vuln | 1 | 3 | 1 | 2 | 0 |
| weak_crypto_001 | vuln | 1 | 2 | 1 | 1 | 0 |
| sensitive_log_001 | vuln | 1 | 2 | 1 | 1 | 0 |
| insecure_deser_001 | vuln | 1 | 2 | 1 | 1 | 0 |
| ssrf_001 | vuln | 1 | 2 | 1 | 1 | 0 |
| xss_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| missing_authz_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| insecure_random_001 | vuln | 1 | 2 | 1 | 1 | 0 |
| xxe_001 | vuln | 1 | 2 | 1 | 1 | 0 |
| npe_map_get_001 | vuln | 1 | 4 | 1 | 3 | 0 |
| resource_leak_001 | vuln | 1 | 4 | 1 | 3 | 0 |
| off_by_one_001 | vuln | 1 | 3 | 1 | 2 | 0 |
| concurrent_modification_001 | vuln | 1 | 3 | 1 | 2 | 0 |
| swallowed_exception_001 | vuln | 1 | 4 | 1 | 3 | 0 |
| hardcoded_config_001 | vuln | 1 | 3 | 1 | 2 | 0 |
| magic_number_001 | vuln | 1 | 4 | 1 | 3 | 0 |

## 级别诊断(最后一次跑测)

只统计标了期望级别的命中项(漏报项不计)。共 19 项,其中 6 项级别判错(✗)。

| 用例 | 类型 | 期望级别 | 报告级别 | 判定 |
|---|---|---|---|---|
| sql_injection_001 | SQL注入 | CRITICAL | CRITICAL | ✓ |
| command_injection_001 | 命令注入 | CRITICAL | CRITICAL | ✓ |
| path_traversal_001 | 路径遍历 | CRITICAL | WARNING | ✗ |
| hardcoded_secret_001 | 硬编码凭证 | CRITICAL | CRITICAL | ✓ |
| weak_crypto_001 | 硬编码/魔法字符串 | WARNING | WARNING | ✓ |
| sensitive_log_001 | 敏感信息泄漏 | WARNING | CRITICAL | ✗ |
| insecure_deser_001 | 资源泄漏 | CRITICAL | CRITICAL | ✓ |
| ssrf_001 | 异常处理不完整 | WARNING | WARNING | ✓ |
| xss_001 | 反射型 XSS（跨站脚本攻击） | WARNING | CRITICAL | ✗ |
| missing_authz_001 | 鉴权缺失 | CRITICAL | CRITICAL | ✓ |
| insecure_random_001 | 魔法数字/硬编码 | WARNING | WARNING | ✓ |
| xxe_001 | 硬编码/缺少安全配置 | WARNING | WARNING | ✓ |
| npe_map_get_001 | 空指针 | WARNING | WARNING | ✓ |
| resource_leak_001 | 资源泄漏 | WARNING | WARNING | ✓ |
| off_by_one_001 | 数组越界 | CRITICAL | CRITICAL | ✓ |
| concurrent_modification_001 | 并发修改异常 | CRITICAL | CRITICAL | ✓ |
| swallowed_exception_001 | 异常处理 | WARNING | CRITICAL | ✗ |
| hardcoded_config_001 | 硬编码 | INFO | CRITICAL | ✗ |
| magic_number_001 | 魔法数字 | INFO | WARNING | ✗ |

## 怎么读这份报告

- **Recall 低**:漏报多,prompt 没覆盖到的漏洞类型,或模型没看懂上下文 —— 这正是阶段 3 工具调用要补的(让 Agent 自己去读相关文件)。
- **误报率高 / Precision 低**:噪音大,代码审查工具最致命的体验问题,对应阶段 2 的「误报过滤」。
- **定位准确率低**:`Issue.line` 不准,影响开发者定位,可考虑结合 diff 行号映射。
- **方差(±)大**:输出不稳定,温度过高或 prompt 不够约束。

_本报告由 `python -m evals.runner` 自动生成。_
