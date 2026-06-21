# Codeguard 审查质量评测报告

- 生成时间:2026-06-21 15:27:37
- Provider / Model:`openai` / `deepseek-v4-pro`
- 数据集:22 条(漏洞 14 / 干净 8)
- 重复跑测:3 次

> 审查质量以固定的数据集 + 指标为统一标准;被测目标(mode / 工具 / 模型)由 profile 描述。
> 跨 profile、跨版本的纵向趋势与横向对照见下方「历史趋势 / profile 对照 / 能力切片」。

## 核心指标

| 指标 | 数值 | 含义 |
|---|---|---|
| **Precision** | 0.583 (±0.008) | 报出的问题里真问题占比(越高=噪音越少) |
| **Recall** | 0.885 (±0.043) | 该审出的问题被审出占比(越高=漏报越少) |
| **F1** | 0.703 | Precision 与 Recall 的调和平均 |
| 误报率(每条干净 diff) | 0.667 | 干净代码上平均误报几个(越低越好) |
| 定位准确率 | 0.922 | 命中项里行号也对上的比例 |
| 级别准确率 | 0.714 | 命中项里 severity 也对上的比例 |
| 诱饵命中率 | 0.111 | 过度上报里「被诱饵骗」的比例(越低=越克制) |
| vuln 噪音/条 | 0.929 | 脏代码上平均每条 diff 误报几个(区别于 clean 误报率) |
| 报告膨胀比 | 1.437 | vuln 用例上 报告数/标答数 的均值(>1 偏过度上报) |
| 级别准确率·复杂用例 | 0.630 | 多问题场景下的级别判准率 |

## 逐用例明细(最后一次跑测)

| 用例 | 类别 | 标答 | 报告 | TP | FP | FN |
|---|---|---|---|---|---|---|
| clean_bounded_loop_001 | clean | 0 | 1 | 0 | 1 | 0 |
| clean_getter_001 | clean | 0 | 0 | 0 | 0 | 0 |
| clean_logged_exception_001 | clean | 0 | 1 | 0 | 1 | 0 |
| clean_logging_001 | clean | 0 | 0 | 0 | 0 | 0 |
| clean_prepared_stmt_001 | clean | 0 | 2 | 0 | 2 | 0 |
| clean_rename_001 | clean | 0 | 0 | 0 | 0 | 0 |
| clean_try_with_resources_001 | clean | 0 | 1 | 0 | 1 | 0 |
| clean_unit_test_001 | clean | 0 | 0 | 0 | 0 | 0 |
| complex_cache_004 | vuln | 3 | 4 | 3 | 1 | 0 |
| complex_config_005 | vuln | 3 | 4 | 2 | 2 | 1 |
| complex_discount_003 | vuln | 4 | 6 | 4 | 2 | 0 |
| complex_file_download_001 | vuln | 3 | 5 | 3 | 2 | 0 |
| complex_import_002 | vuln | 4 | 5 | 4 | 1 | 0 |
| complex_report_006 | vuln | 4 | 6 | 4 | 2 | 0 |
| file_missing_authz_001 | vuln | 1 | 3 | 1 | 2 | 0 |
| file_npe_contract_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| file_path_traversal_001 | vuln | 1 | 3 | 1 | 2 | 0 |
| repomap_npe_abstract_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| repomap_npe_crossfile_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| repomap_npe_delegate_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| repomap_npe_iface_impl_001 | vuln | 1 | 1 | 1 | 0 | 0 |
| repomap_npe_isolated_001 | vuln | 1 | 0 | 0 | 0 | 1 |

## 规则尺 vs 裁判尺(最后一次跑测)

**裁判↔规则一致率:75.6%**(全部跑测累计)。这是评测尺自身的健康度——一致率低说明规则尺关键词匹配偏差大、需靠裁判纠偏,此时复杂用例指标只有开 `--judge` 才可信。

主判为 LLM 裁判(语义配对),规则尺并行作确定性交叉校验。下表只列两尺判定不一致的用例;共 4 条分歧(本次跑测)。分歧为 0 则两尺一致,可放心用规则尺做廉价回归。

| 用例 | 裁判 TP/FP/FN | 规则 TP/FP/FN |
|---|---|---|
| complex_cache_004 | 3/1/0 | 2/2/1 |
| complex_config_005 | 2/2/1 | 3/1/0 |
| complex_file_download_001 | 3/2/0 | 2/3/1 |
| complex_import_002 | 4/1/0 | 2/3/2 |

## 级别诊断(最后一次跑测)

只统计标了期望级别的命中项(漏报项不计)。共 27 项,其中 6 项级别判错(✗)。

| 用例 | 类型 | 期望级别 | 报告级别 | 判定 |
|---|---|---|---|---|
| complex_cache_004 | 魔法数字 | INFO | WARNING | ✗ |
| complex_cache_004 | ConcurrentModificationException | CRITICAL | CRITICAL | ✓ |
| complex_cache_004 | 空指针 | WARNING | WARNING | ✓ |
| complex_config_005 | 硬编码凭据 | CRITICAL | CRITICAL | ✓ |
| complex_config_005 | 吞异常 | WARNING | WARNING | ✓ |
| complex_discount_003 | 空指针解引用 | CRITICAL | WARNING | ✗ |
| complex_discount_003 | 整数除法截断 | WARNING | CRITICAL | ✗ |
| complex_discount_003 | 魔法数字 | WARNING | WARNING | ✓ |
| complex_discount_003 | 数组越界 | CRITICAL | CRITICAL | ✓ |
| complex_file_download_001 | 路径穿越 | CRITICAL | CRITICAL | ✓ |
| complex_file_download_001 | 资源泄漏 | WARNING | CRITICAL | ✗ |
| complex_file_download_001 | 敏感信息泄漏 | WARNING | WARNING | ✓ |
| complex_import_002 | SSRF（服务端请求伪造） | CRITICAL | CRITICAL | ✓ |
| complex_import_002 | 资源泄漏 | WARNING | WARNING | ✓ |
| complex_import_002 | 命令注入 | CRITICAL | CRITICAL | ✓ |
| complex_import_002 | 异常处理缺失 | WARNING | CRITICAL | ✗ |
| complex_report_006 | 路径穿越 | CRITICAL | CRITICAL | ✓ |
| complex_report_006 | 资源泄漏 | WARNING | WARNING | ✓ |
| complex_report_006 | 魔法数字 | INFO | WARNING | ✗ |
| complex_report_006 | 吞异常 | WARNING | WARNING | ✓ |
| file_missing_authz_001 | 越权/鉴权缺失 | CRITICAL | CRITICAL | ✓ |
| file_npe_contract_001 | 错误处理缺陷 | WARNING | WARNING | ✓ |
| file_path_traversal_001 | 路径遍历 | CRITICAL | CRITICAL | ✓ |
| repomap_npe_abstract_001 | 空指针 | WARNING | WARNING | ✓ |
| repomap_npe_crossfile_001 | 空指针 | WARNING | WARNING | ✓ |
| repomap_npe_delegate_001 | 空指针 | WARNING | WARNING | ✓ |
| repomap_npe_iface_impl_001 | 空指针 | WARNING | WARNING | ✓ |

## 过度上报诊断(最后一次跑测)

对埋了诱饵的用例,把误报拆成「中诱饵(被似是而非的点骗了)」与「凭空乱报(既非真问题也非诱饵)」。中诱饵高=克制力差、易被表象误导;凭空乱报高=无中生有。

| 用例 | 诱饵数 | 中诱饵 | 凭空乱报 | FP 合计 |
|---|---|---|---|---|
| complex_cache_004 | 1 | 0 | 1 | 1 |
| complex_config_005 | 1 | 1 | 1 | 2 |
| complex_discount_003 | 1 | 0 | 2 | 2 |
| complex_file_download_001 | 1 | 1 | 1 | 2 |
| complex_import_002 | 1 | 0 | 1 | 1 |
| complex_report_006 | 1 | 0 | 2 | 2 |

## 主/次项 recall 对照

按严重级别分层的检出率:主项=CRITICAL(必须修),次项=WARNING/INFO(建议/可选)。主低次高=漏掉要紧问题(危险);主高次低=只盯大的、忽略次要(可接受)。

| 档位 | Recall |
|---|---|
| 主项(CRITICAL) | 0.900 |
| 次项(WARNING/INFO) | 0.877 |

## 怎么读这份报告

- **Recall 低**:漏报多,prompt 没覆盖到的漏洞类型,或模型没看懂上下文 —— 这正是阶段 3 工具调用要补的(让 Agent 自己去读相关文件)。
- **误报率高 / Precision 低**:噪音大,代码审查工具最致命的体验问题,对应阶段 2 的「误报过滤」。
- **定位准确率低**:`Issue.line` 不准,影响开发者定位,可考虑结合 diff 行号映射。
- **方差(±)大**:输出不稳定,温度过高或 prompt 不够约束。

_本报告由 `python -m evals.runner` 自动生成。_

## 历史趋势(最近 8 次)

| 时间 | git | profile | 工具 | P | R | F1 | 误报率 |
|---|---|---|---|---|---|---|---|
| 2026-06-20T10-54-39 | 37eaa91 | pipeline-repomap | 开 | 0.500 | 0.893 | 0.641 | 0.750 |
| 2026-06-20T11-34-47 | f64ae34 | pipeline-repomap | 开 | 0.531 | 0.929 | 0.675 | 0.833 |
| 2026-06-20T11-55-32 | f64ae34 | pipeline-repomap-fpverify | 开 | 0.640 | 0.869 | 0.737 | 0.500 |
| 2026-06-21T11-24-58 | de6e037 | pipeline-file | 开 | 0.547 | 0.893 | 0.679 | 0.708 |
| 2026-06-21T11-38-24 | de6e037 | pipeline-repomap | 开 | 0.531 | 0.905 | 0.670 | 0.792 |
| 2026-06-21T11-50-44 | de6e037 | pipeline-notools | 关 | 0.511 | 0.833 | 0.633 | 0.792 |
| 2026-06-21T15-12-14 | 9c7e2da | pipeline-file | 开 | 0.529 | 0.828 | 0.646 | 0.708 |
| 2026-06-21T15-27-37 | 9c7e2da | pipeline-repomap | 开 | 0.583 | 0.885 | 0.703 | 0.667 |

## profile 横向对照(各 profile 最近一次)

| profile | 工具 | P | R | F1 | 误报率 |
|---|---|---|---|---|---|
| pipeline-file | 开 | 0.529 | 0.828 | 0.646 | 0.708 |
| pipeline-fpverify | 关 | 0.720 | 0.798 | 0.757 | 0.375 |
| pipeline-notools | 关 | 0.511 | 0.833 | 0.633 | 0.792 |
| pipeline-repomap | 开 | 0.583 | 0.885 | 0.703 | 0.667 |
| pipeline-repomap-fpverify | 开 | 0.640 | 0.869 | 0.737 | 0.500 |

## 按能力切片(各 profile 最近一次的 Recall)

在'需要该能力'的用例子集上各 profile 的 Recall;同一能力行内比较即该能力的工具/编排增益。

| 能力 \ profile | pipeline-file | pipeline-fpverify | pipeline-notools | pipeline-repomap | pipeline-repomap-fpverify |
|---|---|---|---|---|---|
| diff-only | 0.857 | 0.794 | 0.857 | 0.857 | 0.841 |
| file | 0.750 | 0.810 | 0.762 | 0.958 | 0.952 |
| repo-map | 0.733 | 0.917 | 0.667 | 0.933 | 1.000 |
