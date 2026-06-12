# 交接清单（2026-06-12）

> 当前进度快照。下次接手从「下次从哪开始」一节读起即可。

## 阶段 2(管线化)—— 已完成 ✅

目标管线:**并行审查 → 聚合去重 → 误报过滤**(摘要阶段尚缺,留待后续)。仍是"工作流",不是 Agent。

| Step | 内容 | 状态 |
|---|---|---|
| 0 | 扩数据集(security 加量 + logic/quality 两维度) | ✅ 已提交 |
| 1 | 管线骨架 + `--mode pipeline`(与 baseline 等价) | ✅ 已提交 |
| 2 | 并行三领域审查员(security/logic/quality,线程池) | ✅ 已提交 |
| 3 | 聚合去重(跨审查员纯规则去重) | ✅ 已提交 |
| — | 评测判分重构(LLM 裁判主判 + 规则尺交叉校验,ADR-005) | ✅ 已提交 |
| — | 评测台校准:裁判验真 + 多标答 gold(ADR-006)+ prompt 补判例(ADR-007) | ✅ 已提交 |
| 4 | 两段式误报过滤(确定性规则 + 异源 LLM 验证,ADR-008) | ✅ 已提交 |

## 这轮关键结论(都已写进 ADR / 报告)

- **评测台先校准再看效果**:单标答会系统性低估多维度管线 Precision → 改多标答后 Precision 由 0.264 升到可信的 0.40 档,Recall 也从"恒 1.0 橡皮图章"变成真实的 0.97(ADR-006)。
- **裁判/验证模型必须异源**:同源模型核查自己的结论 = 自我确认偏差。裁判尺在本数据集与规则尺 0 分歧(ADR-005 补记);FP 验证同源仅剔除 1 条、异源(qwen 复核 DeepSeek)剔除约 10 条/跑(ADR-008)。
- **误报压制的两层杠杆**:① prompt 判例(零成本,clean 误报率 1.375→0.667,ADR-007);② 后置**异源** LLM 验证(opt-in,再把 Precision 0.40→0.46、clean 误报率 0.667→0.417,Recall 不降,ADR-008)。确定性规则档在当前数据集上无效(噪音是语义型),如实记录、保留架构待数据集长大。

## 当前指标(`evals/reports/pipeline.md`,默认管线 = 误报验证关)

Precision ≈ 0.40 / Recall ≈ 0.97 / clean 误报率 ≈ 0.67(均 3 跑)。开 `CODEGUARD_FP_LLM_VERIFY=true` + 异源裁判模型可达 Precision 0.459 / 误报率 0.417。

## 跑测速查

```powershell
# 单测(工程正确性,应 40 passed)
conda run -n codeguard --no-capture-output python -m pytest tests/ -q

# 评测(默认管线);--judge 开裁判;CODEGUARD_FP_LLM_VERIFY=true 开异源 FP 验证
conda run -n codeguard --no-capture-output python -m evals.runner --mode pipeline --judge --runs 3
```

> 裁判 / FP 验证用独立模型:`.env` 配 `CODEGUARD_JUDGE_*`(本机用通义千问 qwen3.7-plus,推理模型需 `CODEGUARD_JUDGE_DISABLE_THINKING=true`)。

## 👉 下次从哪开始

1. **阶段 3(重头戏):Agent 核心 · 工具调用**——起 Java Tool Server 第一个工具 `get_file_content`,把审查员从"单次调用"升级成 ReAct Agent,用同一数据集做"有工具 vs 无工具"对照(ROADMAP 阶段 3)。
2. 或先补阶段 2 收尾项:摘要阶段、数据集扩量(现仅 19 vuln,二级标答只补了 3 条)、评测报告渲染 `filter_stats`。

## 衍生待办(散落在各 ADR)

- 评测报告头部仍写死"阶段1 baseline"(实为阶段2 pipeline),待修(`evals/report.py`)。
- `.env.example` 未含 `CODEGUARD_JUDGE_*`(评测重构时遗漏),需要时补。
- 级别准确率长期 ~0.6,模型系统性高判 severity(ADR-004 老账,待数据集扩量后复查)。
