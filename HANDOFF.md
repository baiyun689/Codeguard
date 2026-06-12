# 交接清单（2026-06-11）

> 本次对话的进度快照。下次接手从「下次从哪开始」一节读起即可。

## Phase 2（管线化）各 Step 目标与进度

> Phase 2 总目标（见 `docs/ROADMAP.md`）：**把单次 LLM 调用拆成多阶段工作流，加并行审查员**。
> 目标四阶段流水线：**摘要 → 并行审查 → 聚合去重 → 误报过滤**。仍是「工作流」，不是 Agent。
> 方法论：跑通 → 看效果 → 小步迭代 → 记录决策。

| Step | 目标 | 状态 |
|---|---|---|
| **Step 0** | 扩充数据集：security 加量 + 新增 logic/quality 两个维度，给各领域审查员可量化的标准答案 | ✅ 已提交（`08f1c23`） |
| **Step 1** | 管线骨架 + `--mode pipeline`：搭 `PipelineStage`/`PipelineContext`/`Orchestrator`，默认管线只放单审查 stage，**与 baseline 等价**（先证明骨架不掉精度） | ✅ 已提交（`9674279`） |
| **Step 2** | 并行三领域审查员（安全/逻辑/质量），线程池并行，**故意不去重** —— 先让「同一问题被多个审查员重复报」的噪音暴露出来 | ✅ 已提交（`ae30544`） |
| **Step 3** | 聚合去重：跨审查员**纯规则去重**（确定性、可测），保留最高 severity。刻意不删误报，让「去重」与「误报过滤」效果能分别量化 | 🟡 代码完成，**未提交** |
| **Step 4** | 两段式**误报过滤**：先正则（零成本）再 LLM 验证。Phase 2 真正让 precision 回升的一步 | ⬜ 未开始 |
| （后续） | **摘要阶段**：目标架构里排第一（摘要→审查→聚合→过滤），但尚未实现，留待 Step 4 后补 | ⬜ 未开始 |

> **本次插曲（不在原 Step 序列里）**：发现评测「尺子」不准（纯规则关键词匹配偏乐观），先做了一次**评测判分重构**（见下）。这是给 Step 3/Step 4 的效果度量打地基 —— 尺子不准，后面每步「看效果」都不可信。

## ✅ 已完成

### 1. 评测判分重构（本次主线，代码+测试已完成，**未提交**）

把评测的「尺子」从**纯规则、偏乐观**改成 **LLM 裁判主判 + 规则尺交叉校验**，并给 LLM 套了三道约束：确定性算分 / 规则交叉校验 / 独立裁判 + `temperature=0`。

| 文件 | 改动 | git 状态 |
|---|---|---|
| `services/agent/evals/schema.py` | 加 `CaseJudgement`/`JudgeMatch`；`MatchOutcome` 加 `primary_judge` + `rule_*` 交叉校验字段 | M |
| `services/agent/evals/matcher.py` | 重写：`_rule_pairing`（规则尺）+ `judge_case`/`_llm_pairing`（裁判双向语义配对，带脏数据防御）+ `_build_outcome`（据配对**确定性**算 TP/FP/FN/定位/级别） | M |
| `services/agent/src/codeguard_agent/config.py` | 加 `Settings.judge_from_env()`，按**「同端点」而非「同 provider」**决定是否沿用主配置 | M |
| `services/agent/src/codeguard_agent/llm/client.py` | `build_llm` 加 `temperature` 参数（裁判用 0 锁确定性） | M |
| `services/agent/evals/runner.py` | `--judge` 改走独立裁判模型；同源时打 ⚠️ 自评偏差告警 | M |
| `services/agent/evals/report.py` | 加「规则尺 vs 裁判尺」分歧表 | M |
| `services/agent/tests/test_matcher.py` | 12 个新测试（配对/算分/脏数据防御/失败回退） | 新增（??） |
| `DECISIONS.md` | ADR-005 记录决策与权衡 | M |

**关键设计**：裁判只干「配对」这件难事，级别是否相等之类 trivial 判断留给代码；clean 样本**不调裁判**（报出来按定义全是误报），避开最易被自评偏差污染的误报率指标。

**验证**：pytest **25 passed**；mock 模式整条评测链路跑通；「DeepSeek 审查 + 千问裁判」配置解析已验证正确（DeepSeek 的 `disable_thinking` 不会被错塞给千问）。**但真实质量影响尚未量化**（见待办 B/C）。

### 2. 千问（通义千问）裁判配置方案（已给出方案，**待填 key**）

- 千问走 **DashScope 的 OpenAI 兼容端点**，在现有架构里即 `provider=openai` + 自定义 `base_url`。
- 顺带修了 `judge_from_env` 的「同 provider 误继承」坑（DeepSeek 与千问都借 `openai`，但不是同一家）。

### 3.（更早）Step 3 聚合去重（代码完成，**未提交**）

- `services/agent/src/codeguard_agent/pipeline/stages/aggregation.py`（新增）
- `services/agent/tests/test_aggregation.py`（新增）
- `services/agent/src/codeguard_agent/pipeline/orchestrator.py`（改：默认管线接入 `AggregationStage`）

---

## ⏳ 未完成 / 待办

| # | 任务 | 备注 |
|---|---|---|
| A | **填 DashScope key 到 `.env`** | 见下方「千问配置块」 |
| B | **跑一轮真实 `--judge`** | `python -m evals.runner --mode pipeline --judge --runs 3` |
| C | **核对裁判效果** | 看报告里「规则尺 vs 裁判尺」分歧表是否合理；留意有无「案例级裁判调用失败,回退规则尺」日志 |
| D | **两笔 commit 收尾** | 工作区混了两件不相干的事，分开提交（见下） |
| E | 旧逐对 `JudgeScore` + 质量打分(1~5) | 暂搁置，ADR-005 已记，需要时再折进案例级裁判一并产出 |
| F | **Step 4：误报过滤（FP filter）** | Phase 2 下一步，真正让 precision 回升的地方 |

### 待提交的两笔（顺序无所谓）

1. `feat(pipeline): 新增聚合去重阶段(AggregationStage)`
   → `aggregation.py` + `test_aggregation.py` + `orchestrator.py`（+ `evals/reports/pipeline.md` 报告产物）
2. `feat(evals): 判分改为 LLM 裁判主判 + 规则尺交叉校验`
   → `matcher.py` / `schema.py` / `report.py` / `runner.py` / `config.py` / `llm/client.py` + `test_matcher.py` + `DECISIONS.md`

> ⚠️ `evals/reports/pipeline.md` 是评测产物（非手写代码），提交时归第 1 笔或单独处理，别和重构混。

### 千问裁判配置块（追加到 `services/agent/.env` 或仓库根 `.env`，不动原 DeepSeek 配置）

```ini
# 评测裁判 = 通义千问（走 DashScope OpenAI 兼容端点）
CODEGUARD_JUDGE_PROVIDER=openai
CODEGUARD_JUDGE_MODEL=qwen-max
CODEGUARD_JUDGE_API_KEY=sk-你的DashScope密钥
CODEGUARD_JUDGE_API_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

- key 获取：阿里云百炼 https://bailian.console.aliyun.com → 右上角 API-KEY。
- 模型选型：`qwen-max`（最强，当裁判推荐）/ `qwen-plus`（省一点）/ `qwen-turbo`（不要拿来当裁判）。

---

## 👉 下次从哪开始

1. **A → B → C**：填 key、跑 `--judge`、看分歧表。这是检验本次重构「尺子到底准不准」的关键 —— ADR-005 特意没宣称「更准了」，就等这组真实数据。
2. **D**：按两笔 commit 收尾。
3. **F**：进 Step 4 误报过滤。

---

## 跑测速查

```powershell
# 单测（工程正确性，应 25 passed）
conda run -n codeguard --no-capture-output python -m pytest tests/ -q

# 评测（质量量化）；--judge 开 LLM 裁判主判，规则尺并行交叉校验
conda run -n codeguard --no-capture-output python -m evals.runner --mode pipeline --judge --runs 3
```

> 当前 git 基线：`e971df5 docs: 增加提交信息规范(Conventional Commits)`
