# Codeguard 审查质量评测报告

- 生成时间:2026-07-04 21:35:06
- Provider / Model:`mock` / `deepseek-v4-pro`
- 数据集:23 条(漏洞 15 / 干净 8)
- 重复跑测:1 次

> 审查质量以固定的数据集 + 指标为统一标准;被测目标(mode / 工具 / 模型)由 profile 描述。
> 跨 profile、跨版本的纵向趋势与横向对照见下方「历史趋势 / profile 对照 / 能力切片」。

## 核心指标

| 指标 | 数值 | 含义 |
|---|---|---|
| **Precision** | 0.000 (±0.000) | 报出的问题里真问题占比(越高=噪音越少) |
| **Recall** | 0.000 (±0.000) | 该审出的问题被审出占比(越高=漏报越少) |
| **F1** | 0.000 | Precision 与 Recall 的调和平均 |
| 误报率(每条干净 diff) | 1.000 | 干净代码上平均误报几个(越低越好) |
| 定位准确率 | 0.000 | 命中项里行号也对上的比例 |
| 级别准确率 | 0.000 | 命中项里 severity 也对上的比例 |
| 诱饵命中率 | 0.000 | 过度上报里「被诱饵骗」的比例(越低=越克制) |
| vuln 噪音/条 | 1.000 | 脏代码上平均每条 diff 误报几个(区别于 clean 误报率) |
| 报告膨胀比 | 0.717 | vuln 用例上 报告数/标答数 的均值(>1 偏过度上报) |
| 级别准确率·复杂用例 | — | 多问题场景下的级别判准率 |

## 逐用例明细(最后一次跑测)

| 用例 | 类别 | 标答 | 报告 | TP | FP | FN |
|---|---|---|---|---|---|---|
| clean_bounded_loop_001 | clean | 0 | 1 | 0 | 1 | 0 |
| clean_getter_001 | clean | 0 | 1 | 0 | 1 | 0 |
| clean_logged_exception_001 | clean | 0 | 1 | 0 | 1 | 0 |
| clean_logging_001 | clean | 0 | 1 | 0 | 1 | 0 |
| clean_prepared_stmt_001 | clean | 0 | 1 | 0 | 1 | 0 |
| clean_rename_001 | clean | 0 | 1 | 0 | 1 | 0 |
| clean_try_with_resources_001 | clean | 0 | 1 | 0 | 1 | 0 |
| clean_unit_test_001 | clean | 0 | 1 | 0 | 1 | 0 |
| complex_cache_004 | vuln | 3 | 1 | 0 | 1 | 3 |
| complex_config_005 | vuln | 3 | 1 | 0 | 1 | 3 |
| complex_discount_003 | vuln | 4 | 1 | 0 | 1 | 4 |
| complex_file_download_001 | vuln | 3 | 1 | 0 | 1 | 3 |
| complex_import_002 | vuln | 4 | 1 | 0 | 1 | 4 |
| complex_report_006 | vuln | 4 | 1 | 0 | 1 | 4 |
| file_missing_authz_001 | vuln | 1 | 1 | 0 | 1 | 1 |
| file_npe_contract_001 | vuln | 1 | 1 | 0 | 1 | 1 |
| file_path_traversal_001 | vuln | 1 | 1 | 0 | 1 | 1 |
| repomap_npe_abstract_001 | vuln | 1 | 1 | 0 | 1 | 1 |
| repomap_npe_caller_001 | vuln | 1 | 1 | 0 | 1 | 1 |
| repomap_npe_crossfile_001 | vuln | 1 | 1 | 0 | 1 | 1 |
| repomap_npe_delegate_001 | vuln | 1 | 1 | 0 | 1 | 1 |
| repomap_npe_iface_impl_001 | vuln | 1 | 1 | 0 | 1 | 1 |
| repomap_npe_isolated_001 | vuln | 1 | 1 | 0 | 1 | 1 |

## ReviewCouncil 过程统计(最后一次跑测)

ADR-032 中间态只用于 trace/eval,不进入最终 ReviewResult。

| 用例 | 候选 | 角色候选分布 | 证据请求 | 证据轮次 | Challenge | SelfChecker 移除 | 截断 | Trace 事件 |
|---|---|---|---|---|---|---|---|---|
| clean_bounded_loop_001 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| clean_getter_001 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| clean_logged_exception_001 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| clean_logging_001 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| clean_prepared_stmt_001 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| clean_rename_001 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| clean_try_with_resources_001 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| clean_unit_test_001 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| complex_cache_004 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| complex_config_005 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| complex_discount_003 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| complex_file_download_001 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| complex_import_002 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| complex_report_006 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| file_missing_authz_001 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| file_npe_contract_001 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| file_path_traversal_001 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| repomap_npe_abstract_001 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| repomap_npe_caller_001 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| repomap_npe_crossfile_001 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| repomap_npe_delegate_001 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| repomap_npe_iface_impl_001 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |
| repomap_npe_isolated_001 | 1 | threat_model=1, behavior=0, maintainability=0 | 1 | 1 | 1 | 0 (challenge=0, aggregation=0, fp_rules=0, fp_llm=0) | candidates=0, evidence_requests=0 | 13 |

## 过度上报诊断(最后一次跑测)

对埋了诱饵的用例,把误报拆成「中诱饵(被似是而非的点骗了)」与「凭空乱报(既非真问题也非诱饵)」。中诱饵高=克制力差、易被表象误导;凭空乱报高=无中生有。

| 用例 | 诱饵数 | 中诱饵 | 凭空乱报 | FP 合计 |
|---|---|---|---|---|
| complex_cache_004 | 1 | 0 | 1 | 1 |
| complex_config_005 | 1 | 0 | 1 | 1 |
| complex_discount_003 | 1 | 0 | 1 | 1 |
| complex_file_download_001 | 1 | 0 | 1 | 1 |
| complex_import_002 | 1 | 0 | 1 | 1 |
| complex_report_006 | 1 | 0 | 1 | 1 |

## 主/次项 recall 对照

按严重级别分层的检出率:主项=CRITICAL(必须修),次项=WARNING/INFO(建议/可选)。主低次高=漏掉要紧问题(危险);主高次低=只盯大的、忽略次要(可接受)。

| 档位 | Recall |
|---|---|
| 主项(CRITICAL) | 0.000 |
| 次项(WARNING/INFO) | 0.000 |

## 怎么读这份报告

- **Recall 低**:漏报多,prompt 没覆盖到的漏洞类型,或模型没看懂上下文 —— 这正是阶段 3 工具调用要补的(让 Agent 自己去读相关文件)。
- **误报率高 / Precision 低**:噪音大,代码审查工具最致命的体验问题,对应阶段 2 的「误报过滤」。
- **定位准确率低**:`Issue.line` 不准,影响开发者定位,可考虑结合 diff 行号映射。
- **方差(±)大**:输出不稳定,温度过高或 prompt 不够约束。

_本报告由 `python -m evals.runner` 自动生成。_

## 历史趋势(最近 8 次)

| 时间 | git | profile | 工具 | P | R | F1 | 误报率 |
|---|---|---|---|---|---|---|---|
| 2026-06-21T15-27-37 | 9c7e2da | pipeline-repomap | 开 | 0.583 | 0.885 | 0.703 | 0.667 |
| 2026-06-21T16-46-52 | 07a016c | pipeline-repomap | 开 | 0.472 | 0.756 | 0.581 | 0.583 |
| 2026-06-21T17-00-49 | 07a016c | pipeline-repomap | 开 | 0.508 | 0.744 | 0.604 | 0.500 |
| 2026-06-21T17-20-57 | 07a016c | pipeline-repomap | 开 | 0.506 | 0.867 | 0.639 | 0.667 |
| 2026-06-21T17-41-52 | 07a016c | pipeline-repomap | 开 | 0.545 | 0.878 | 0.672 | 0.625 |
| 2026-06-21T17-59-20 | 07a016c | pipeline-repomap | 开 | 0.543 | 0.911 | 0.680 | 0.708 |
| 2026-06-22T19-55-33 | ff1a728 | pipeline-repomap-fpverify | 开 | 0.615 | 0.800 | 0.696 | 0.375 |
| 2026-07-04T21-35-05 | 26e3075 | adr-032-smoke | 关 | 0.000 | 0.000 | 0.000 | 1.000 |

## profile 横向对照(各 profile 最近一次)

| profile | 工具 | P | R | F1 | 误报率 |
|---|---|---|---|---|---|
| adr-032-smoke | 关 | 0.000 | 0.000 | 0.000 | 1.000 |
| pipeline-file | 开 | 0.529 | 0.828 | 0.646 | 0.708 |
| pipeline-fpverify | 关 | 0.720 | 0.798 | 0.757 | 0.375 |
| pipeline-notools | 关 | 0.511 | 0.833 | 0.633 | 0.792 |
| pipeline-repomap | 开 | 0.543 | 0.911 | 0.680 | 0.708 |
| pipeline-repomap-fpverify | 开 | 0.615 | 0.800 | 0.696 | 0.375 |

## 按能力切片(各 profile 最近一次的 Recall)

在'需要该能力'的用例子集上各 profile 的 Recall;同一能力行内比较即该能力的工具/编排增益。

| 能力 \ profile | adr-032-smoke | pipeline-file | pipeline-fpverify | pipeline-notools | pipeline-repomap | pipeline-repomap-fpverify |
|---|---|---|---|---|---|---|
| diff-only | 0.000 | 0.857 | 0.794 | 0.857 | 0.873 | 0.762 |
| file | 0.000 | 0.750 | 0.810 | 0.762 | 1.000 | 0.889 |
| repo-map | 0.000 | 0.733 | 0.917 | 0.667 | 1.000 | 1.000 |
