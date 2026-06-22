# Codeguard 审查质量评测报告

- 生成时间:2026-06-22 19:55:33
- Provider / Model:`openai` / `deepseek-v4-pro`
- 数据集:23 条(漏洞 15 / 干净 8)
- 重复跑测:1 次

> 审查质量以固定的数据集 + 指标为统一标准;被测目标(mode / 工具 / 模型)由 profile 描述。
> 跨 profile、跨版本的纵向趋势与横向对照见下方「历史趋势 / profile 对照 / 能力切片」。

## 核心指标

| 指标 | 数值 | 含义 |
|---|---|---|
| **Precision** | 0.615 (±0.000) | 报出的问题里真问题占比(越高=噪音越少) |
| **Recall** | 0.800 (±0.000) | 该审出的问题被审出占比(越高=漏报越少) |
| **F1** | 0.696 | Precision 与 Recall 的调和平均 |
| 误报率(每条干净 diff) | 0.375 | 干净代码上平均误报几个(越低越好) |
| 定位准确率 | 0.792 | 命中项里行号也对上的比例 |
| 级别准确率 | 0.708 | 命中项里 severity 也对上的比例 |
| 诱饵命中率 | 0.167 | 过度上报里「被诱饵骗」的比例(越低=越克制) |
| vuln 噪音/条 | 0.800 | 脏代码上平均每条 diff 误报几个(区别于 clean 误报率) |
| 报告膨胀比 | 1.222 | vuln 用例上 报告数/标答数 的均值(>1 偏过度上报) |
| 级别准确率·复杂用例 | 0.688 | 多问题场景下的级别判准率 |

## 逐用例明细(最后一次跑测)

| 用例 | 类别 | 标答 | 报告 | TP | FP | FN |
|---|---|---|---|---|---|---|
| clean_bounded_loop_001 | clean | 0 | 0 | 0 | 0 | 0 |
| clean_getter_001 | clean | 0 | 0 | 0 | 0 | 0 |
| clean_logged_exception_001 | clean | 0 | 1 | 0 | 1 | 0 |
| clean_logging_001 | clean | 0 | 0 | 0 | 0 | 0 |
| clean_prepared_stmt_001 | clean | 0 | 2 | 0 | 2 | 0 |
| clean_rename_001 | clean | 0 | 0 | 0 | 0 | 0 |
| clean_try_with_resources_001 | clean | 0 | 0 | 0 | 0 | 0 |
| clean_unit_test_001 | clean | 0 | 0 | 0 | 0 | 0 |
| complex_cache_004 | vuln | 3 | 4 | 3 | 1 | 0 |
| complex_config_005 | vuln | 3 | 5 | 2 | 3 | 1 |
| complex_discount_003 | vuln | 4 | 1 | 1 | 0 | 3 |
| complex_file_download_001 | vuln | 3 | 4 | 3 | 1 | 0 |
| complex_import_002 | vuln | 4 | 5 | 4 | 1 | 0 |
| complex_report_006 | vuln | 4 | 6 | 3 | 3 | 1 |
| file_missing_authz_001 | vuln | 1 | 3 | 0 | 3 | 1 |
| file_npe_contract_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| file_path_traversal_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| repomap_npe_abstract_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| repomap_npe_caller_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| repomap_npe_crossfile_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| repomap_npe_delegate_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| repomap_npe_iface_impl_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| repomap_npe_isolated_001 | vuln | 1 | 1 | 1 | 0 | 0 |

## 工具使用(最后一次跑测)

审查员实际发起的工具调用画像(去重后取得有效上下文的调用)。若 repo_map / 读到 callers 段 全为 — 但该用例仍判 TP,即工具没用上、纯靠 diff 推理蒙对(见 ADR-022)。

| 用例 | 工具调用 | 用到的工具 | repo_map | 读到 callers 段 | 读取文件 |
|---|---|---|---|---|---|
| file_missing_authz_001 | 2 | get_file_content, get_repo_map | ✓ | — | src/main/java/com/demo/AccountController.java |
| file_npe_contract_001 | 2 | get_file_content, get_repo_map | ✓ | — | src/main/java/com/demo/UserLookup.java |
| file_path_traversal_001 | 2 | get_file_content, get_repo_map | ✓ | — | src/main/java/com/demo/FileController.java |
| repomap_npe_abstract_001 | 4 | get_file_content, get_repo_map | ✓ | ✓ | src/main/java/com/demo/InvoiceController.java, src/main/java/com/demo/RenderEngine.java, src/main/java/com/demo/engine/CachedSnippetEngine.java |
| repomap_npe_caller_001 | 4 | get_file_content, get_repo_map | ✓ | ✓ | src/main/java/com/demo/directory/MemberDirectory.java, src/main/java/com/demo/greeting/GreetingService.java, src/main/java/com/demo/report/MemberReport.java |
| repomap_npe_crossfile_001 | 3 | get_file_content, get_repo_map | ✓ | ✓ | src/main/java/com/demo/OrderController.java, src/main/java/com/demo/OrderRepository.java |
| repomap_npe_delegate_001 | 5 | get_file_content, get_repo_map | ✓ | ✓ | src/main/java/com/demo/AccountController.java, src/main/java/com/demo/AccountService.java, src/main/java/com/demo/NameDirectory.java, src/main/java/com/demo/directory/StaticRoster.java |
| repomap_npe_iface_impl_001 | 4 | get_file_content, get_repo_map | ✓ | ✓ | src/main/java/com/demo/OrderController.java, src/main/java/com/demo/OrderStore.java, src/main/java/com/demo/store/LedgerBackedStore.java |
| repomap_npe_isolated_001 | 7 | get_file_content, get_repo_map | ✓ | ✓ | src/main/java/com/demo/config/AppConfig.java, src/main/java/com/demo/pricing/PriceCatalog.java, src/main/java/com/demo/pricing/impl/RegionalCatalog.java, src/main/java/com/demo/pricing/impl/StandardCatalog.java, src/main/java/com/demo/pricing/legacy/TariffLookupTable.java, src/main/java/com/demo/web/QuoteController.java |

## 规则尺 vs 裁判尺(最后一次跑测)

**裁判↔规则一致率:80.0%**(全部跑测累计)。这是评测尺自身的健康度——一致率低说明规则尺关键词匹配偏差大、需靠裁判纠偏,此时复杂用例指标只有开 `--judge` 才可信。

主判为 LLM 裁判(语义配对),规则尺并行作确定性交叉校验。下表只列两尺判定不一致的用例;共 3 条分歧(本次跑测)。分歧为 0 则两尺一致,可放心用规则尺做廉价回归。

| 用例 | 裁判 TP/FP/FN | 规则 TP/FP/FN |
|---|---|---|
| complex_config_005 | 2/3/1 | 3/2/0 |
| complex_discount_003 | 1/0/3 | 0/1/4 |
| repomap_npe_caller_001 | 1/0/0 | 0/1/1 |

## 级别诊断(最后一次跑测)

只统计标了期望级别的命中项(漏报项不计)。共 24 项,其中 7 项级别判错(✗)。

| 用例 | 类型 | 期望级别 | 报告级别 | 判定 |
|---|---|---|---|---|
| complex_cache_004 | 魔法数字 | INFO | WARNING | ✗ |
| complex_cache_004 | ConcurrentModificationException | CRITICAL | WARNING | ✗ |
| complex_cache_004 | 健壮性 | WARNING | INFO | ✗ |
| complex_config_005 | 硬编码凭据 | CRITICAL | CRITICAL | ✓ |
| complex_config_005 | 空指针 | WARNING | CRITICAL | ✗ |
| complex_discount_003 | 魔法数字 | WARNING | WARNING | ✓ |
| complex_file_download_001 | 路径遍历 | CRITICAL | CRITICAL | ✓ |
| complex_file_download_001 | 资源泄漏 | WARNING | WARNING | ✓ |
| complex_file_download_001 | 敏感信息泄漏 | WARNING | WARNING | ✓ |
| complex_import_002 | SSRF | CRITICAL | CRITICAL | ✓ |
| complex_import_002 | 资源泄漏 | WARNING | WARNING | ✓ |
| complex_import_002 | 命令注入 | CRITICAL | CRITICAL | ✓ |
| complex_import_002 | 空catch块吞异常 | WARNING | WARNING | ✓ |
| complex_report_006 | 资源泄漏 | WARNING | WARNING | ✓ |
| complex_report_006 | 魔法数字 | INFO | WARNING | ✗ |
| complex_report_006 | 空catch块吞异常 | WARNING | WARNING | ✓ |
| file_npe_contract_001 | 空指针 | WARNING | CRITICAL | ✗ |
| file_path_traversal_001 | 路径穿越 | CRITICAL | CRITICAL | ✓ |
| repomap_npe_abstract_001 | 空指针风险（可维护性） | WARNING | WARNING | ✓ |
| repomap_npe_caller_001 | 契约变更/隐含null语义 | WARNING | WARNING | ✓ |
| repomap_npe_crossfile_001 | 空指针 | WARNING | WARNING | ✓ |
| repomap_npe_delegate_001 | 空指针 | WARNING | WARNING | ✓ |
| repomap_npe_iface_impl_001 | 空指针 | WARNING | WARNING | ✓ |
| repomap_npe_isolated_001 | 空指针 | WARNING | CRITICAL | ✗ |

## 过度上报诊断(最后一次跑测)

对埋了诱饵的用例,把误报拆成「中诱饵(被似是而非的点骗了)」与「凭空乱报(既非真问题也非诱饵)」。中诱饵高=克制力差、易被表象误导;凭空乱报高=无中生有。

| 用例 | 诱饵数 | 中诱饵 | 凭空乱报 | FP 合计 |
|---|---|---|---|---|
| complex_cache_004 | 1 | 0 | 1 | 1 |
| complex_config_005 | 1 | 1 | 2 | 3 |
| complex_discount_003 | 1 | 0 | 0 | 0 |
| complex_file_download_001 | 1 | 0 | 1 | 1 |
| complex_import_002 | 1 | 0 | 1 | 1 |
| complex_report_006 | 1 | 0 | 3 | 3 |

## 主/次项 recall 对照

按严重级别分层的检出率:主项=CRITICAL(必须修),次项=WARNING/INFO(建议/可选)。主低次高=漏掉要紧问题(危险);主高次低=只盯大的、忽略次要(可接受)。

| 档位 | Recall |
|---|---|
| 主项(CRITICAL) | 0.600 |
| 次项(WARNING/INFO) | 0.900 |

## 怎么读这份报告

- **Recall 低**:漏报多,prompt 没覆盖到的漏洞类型,或模型没看懂上下文 —— 这正是阶段 3 工具调用要补的(让 Agent 自己去读相关文件)。
- **误报率高 / Precision 低**:噪音大,代码审查工具最致命的体验问题,对应阶段 2 的「误报过滤」。
- **定位准确率低**:`Issue.line` 不准,影响开发者定位,可考虑结合 diff 行号映射。
- **方差(±)大**:输出不稳定,温度过高或 prompt 不够约束。

_本报告由 `python -m evals.runner` 自动生成。_

## 历史趋势(最近 8 次)

| 时间 | git | profile | 工具 | P | R | F1 | 误报率 |
|---|---|---|---|---|---|---|---|
| 2026-06-21T15-12-14 | 9c7e2da | pipeline-file | 开 | 0.529 | 0.828 | 0.646 | 0.708 |
| 2026-06-21T15-27-37 | 9c7e2da | pipeline-repomap | 开 | 0.583 | 0.885 | 0.703 | 0.667 |
| 2026-06-21T16-46-52 | 07a016c | pipeline-repomap | 开 | 0.472 | 0.756 | 0.581 | 0.583 |
| 2026-06-21T17-00-49 | 07a016c | pipeline-repomap | 开 | 0.508 | 0.744 | 0.604 | 0.500 |
| 2026-06-21T17-20-57 | 07a016c | pipeline-repomap | 开 | 0.506 | 0.867 | 0.639 | 0.667 |
| 2026-06-21T17-41-52 | 07a016c | pipeline-repomap | 开 | 0.545 | 0.878 | 0.672 | 0.625 |
| 2026-06-21T17-59-20 | 07a016c | pipeline-repomap | 开 | 0.543 | 0.911 | 0.680 | 0.708 |
| 2026-06-22T19-55-33 | ff1a728 | pipeline-repomap-fpverify | 开 | 0.615 | 0.800 | 0.696 | 0.375 |

## profile 横向对照(各 profile 最近一次)

| profile | 工具 | P | R | F1 | 误报率 |
|---|---|---|---|---|---|
| pipeline-file | 开 | 0.529 | 0.828 | 0.646 | 0.708 |
| pipeline-fpverify | 关 | 0.720 | 0.798 | 0.757 | 0.375 |
| pipeline-notools | 关 | 0.511 | 0.833 | 0.633 | 0.792 |
| pipeline-repomap | 开 | 0.543 | 0.911 | 0.680 | 0.708 |
| pipeline-repomap-fpverify | 开 | 0.615 | 0.800 | 0.696 | 0.375 |

## 按能力切片(各 profile 最近一次的 Recall)

在'需要该能力'的用例子集上各 profile 的 Recall;同一能力行内比较即该能力的工具/编排增益。

| 能力 \ profile | pipeline-file | pipeline-fpverify | pipeline-notools | pipeline-repomap | pipeline-repomap-fpverify |
|---|---|---|---|---|---|
| diff-only | 0.857 | 0.794 | 0.857 | 0.873 | 0.762 |
| file | 0.750 | 0.810 | 0.762 | 1.000 | 0.889 |
| repo-map | 0.733 | 0.917 | 0.667 | 1.000 | 1.000 |
