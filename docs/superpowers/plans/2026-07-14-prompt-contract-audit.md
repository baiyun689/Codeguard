# Prompt Contract Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让当前生产与 eval Prompt 准确描述 task-scoped 调用、条件性工具、领域边界和真实结构化输出字段，并用契约测试防止再次漂移。

**Architecture:** 以实际调用点、`ReviewResult`/Council/eval Pydantic 模型及 `RISK_TAG_REVIEWERS` 为唯一事实源。Prompt 仍保持独立文本文件或既有 eval 常量，不引入运行时生成层；测试只断言稳定契约事实，不快照整段文案。

**Tech Stack:** Python 3.12、Pydantic v2、LangChain structured output、pytest、Ruff、mypy。

## Global Constraints

- 不修改或删除 `src/codeguard_agent/legacy/` 下的任何文件，也不改写只供 legacy 使用的 `fp_verify.txt`。
- 不改变 `Issue`、`ReviewResult`、Council 模型、RiskTag 路由、工具 allowlist 或 Java Gateway。
- 每条发现者 Issue 必须属于当前 task 文件；diff 外代码只能作为证据。
- DirectEngine 下不得暗示工具可用；只有运行时暴露的工具才能调用。
- 保留工作树已有的 `recipes.py`、`test_evidence_rules.py`、`GetCodeMetricsTool.java` 改动，不纳入本计划提交。

---

### Task 1: 建立 Prompt 可达性与字段契约测试

**Files:**
- Create: `services/agent/tests/test_prompt_contracts.py`

**Interfaces:**
- Consumes: `pipeline.stages.reviewer_stage.DEFAULT_REVIEWERS`、`pipeline.risk_rules.catalog.RISK_TAG_REVIEWERS`、`models.tasks.RiskTag` 和 Prompt 文件树。
- Produces: 面向后续 Prompt 改动的稳定失败断言；不新增运行时代码接口。

- [ ] **Step 1: 写发现者与知识覆盖的失败测试**

```python
from pathlib import Path

from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.risk_rules.catalog import RISK_TAG_REVIEWERS

PROMPT_DIR = Path(__file__).parents[1] / "src" / "codeguard_agent" / "prompts"
REVIEWER_DIRS = {
    "ThreatModelAgent": "threat_model",
    "BehaviorAgent": "behavior",
    "MaintainabilityAgent": "maintainability",
}


def _prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def test_reviewer_prompts_describe_task_scoped_conditional_tool_contract():
    for name in ("threat-model-base.txt", "behavior-base.txt", "maintainability-base.txt"):
        text = _prompt(name)
        assert "当前 task patch" in text
        assert "完整 diff" not in text
        assert "运行时提供" in text
        assert "当前任务文件" in text
        assert "diff 外部问题" not in text
        assert "低置信候选" not in text


def test_every_routed_risk_tag_has_reviewer_knowledge():
    for tag, reviewers in RISK_TAG_REVIEWERS.items():
        if tag is RiskTag.GENERAL_REVIEW:
            continue
        for reviewer in reviewers:
            path = PROMPT_DIR / "knowledge" / REVIEWER_DIRS[reviewer] / f"{tag.value}.txt"
            assert path.is_file(), f"missing knowledge: {reviewer}/{tag.value}"
```

- [ ] **Step 2: 写结构化模型字段的失败测试**

```python
def test_reviewer_output_contract_names_every_review_result_field():
    fields = {"summary", "issues", "severity", "file", "line", "type", "message", "suggestion", "confidence"}
    for name in ("threat-model-base.txt", "behavior-base.txt", "maintainability-base.txt"):
        text = _prompt(name)
        assert all(f"`{field}`" in text for field in fields)
        assert all(value in text for value in ("CRITICAL", "WARNING", "INFO"))


def test_evidence_and_judge_prompts_describe_wrapper_contracts():
    analysis = _prompt("evidence-analysis.txt")
    assert all(f"`{field}`" in analysis for field in ("relation", "strength", "observation", "limitation"))
    judge = _prompt("council-judge.txt")
    assert "`decisions`" in judge
    assert "`candidate_id`" in judge
    assert "不要选择 `merge`" in judge
    assert "仅在输入明确允许补证" in judge
```

- [ ] **Step 3: 运行测试并确认因旧 Prompt 失败**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_prompt_contracts.py -q`

Expected: FAIL，至少包含“完整 diff”仍存在或缺少反引号字段契约。

- [ ] **Step 4: 提交测试基线**

```powershell
git add -- services/agent/tests/test_prompt_contracts.py
git commit -m "test(prompts): 添加 Prompt 契约回归测试"
```

### Task 2: 修正三个 task-scoped 发现者 Prompt

**Files:**
- Modify: `services/agent/src/codeguard_agent/prompts/threat-model-base.txt`
- Modify: `services/agent/src/codeguard_agent/prompts/behavior-base.txt`
- Modify: `services/agent/src/codeguard_agent/prompts/maintainability-base.txt`

**Interfaces:**
- Consumes: `ReviewTask.patch`、运行时注入的风险画像/ContextBundle/知识片段、可选工具和 `ReviewResult` schema。
- Produces: `ReviewResult(summary: str, issues: list[Issue])`，每个 Issue 严格定位当前 task 文件。

- [ ] **Step 1: 将输入和工具契约改成真实调用语义**

三个 Prompt 都必须明确写入以下规则：

```text
你每次只收到一个已路由的当前 task patch，而不是整个 PR 的完整 diff。
只有运行时提供并暴露给你的工具才可调用；未提供工具时，只依据 task patch、风险画像和任务上下文判断，不得假装取得额外事实。
工具读取到的其他文件只作为当前候选的证据，不得把 diff 外已有代码作为新的 Issue 位置。
```

- [ ] **Step 2: 消除阈值、范围和领域边界冲突**

删除“可输出低置信候选”和“可报告 diff 外问题”。Behavior 不再排除已路由的性能、资源生命周期和 API 契约，而是限定为可证明的运行影响；Maintainability 将这三类限定为结构、所有权和可演进性影响。ThreatModel 只报告攻击者/信任边界成立的安全影响。

- [ ] **Step 3: 为三个 Prompt 写完整 ReviewResult/Issue 字段语义**

```text
- `summary`：本 Agent 对当前 task 的简短结论；没有问题时也不得虚构 Issue。
- `issues`：当前 task 的问题列表。
- `severity`：只能为 `CRITICAL`、`WARNING`、`INFO`。
- `file`：当前任务文件的仓库相对路径。
- `line`：问题对应的 new-side 行号；确实无法定位时填 `0`，不得猜测。
- `type`：稳定、简洁的问题类型。
- `message`：根因、触发条件和实际影响。
- `suggestion`：针对根因的可执行修复建议。
- `confidence`：0 到 1 的证据确信度；低于 0.7 的候选不要放入 `issues`。
```

- [ ] **Step 4: 运行 Task 1 契约测试**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_prompt_contracts.py tests/test_reviewer_stage.py tests/test_graph_orchestration.py -q`

Expected: PASS。

- [ ] **Step 5: 提交发现者 Prompt 修正**

```powershell
git add -- services/agent/src/codeguard_agent/prompts/threat-model-base.txt services/agent/src/codeguard_agent/prompts/behavior-base.txt services/agent/src/codeguard_agent/prompts/maintainability-base.txt
git commit -m "fix(prompts): 对齐 task scoped 发现者契约"
```

### Task 3: 对齐证据、裁决、摘要、聚合和 eval Prompt

**Files:**
- Modify: `services/agent/src/codeguard_agent/prompts/summary-system.txt`
- Modify: `services/agent/src/codeguard_agent/prompts/summary-user.txt`
- Modify: `services/agent/src/codeguard_agent/prompts/evidence-tag-classifier-system.txt`
- Modify: `services/agent/src/codeguard_agent/prompts/evidence-tag-classifier-user.txt`
- Modify: `services/agent/src/codeguard_agent/prompts/evidence-analysis.txt`
- Modify: `services/agent/src/codeguard_agent/prompts/council-judge.txt`
- Modify: `services/agent/src/codeguard_agent/prompts/aggregation-system.txt`
- Modify: `services/agent/evals/matcher.py`
- Test: `services/agent/tests/test_prompt_contracts.py`

**Interfaces:**
- Consumes: `_DiffSummary`、`_LlmTagResolution`、`_EvidenceAnalysis`、`JudgeDecisions`、聚合 wrapper 与 `CaseJudgement`。
- Produces: 与上述 structured output 模型一致的字段语义；不改变任何模型定义。

- [ ] **Step 1: 扩充测试以覆盖全部当前结构化 Prompt**

测试分别断言 summary 的 `summary`；分类器的 `tag/confidence/reason`；Evidence 的四字段和枚举；Judge 的 `decisions` 及 JudgeDecision 字段、禁止主动 `merge`、补证条件；Aggregation 的 `groups/members`；eval judge 的 `matches/expected_id/reported_id/reason`。

- [ ] **Step 2: 运行扩充测试并确认失败**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_prompt_contracts.py -q`

Expected: FAIL，指出旧 Prompt 未完整描述 wrapper 或字段条件。

- [ ] **Step 3: 按真实模型修正 Prompt**

Judge 必须明确：

```text
返回 `JudgeDecisions`，外层 `decisions` 数组必须恰好包含当前候选的一项决定。
`candidate_id` 原样返回短别名；`action` 只按允许动作选择。
不要选择 `merge`；跨候选语义合并由后续聚合阶段完成。
`needs_more_evidence` 仅在输入明确允许补证且仍有下一轮时使用，并填写 `requested_purpose`。
`downgrade` 必须填写 `adjusted_severity`。
```

其他 Prompt 同样显式描述 wrapper 字段和空结果，不要求手写 JSON。

- [ ] **Step 4: 运行相关测试**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_prompt_contracts.py tests/test_council_judge.py tests/test_evidence_agent.py tests/test_evidence_rules.py tests/test_summary_stage.py tests/test_aggregation_stage.py -q`

Expected: PASS；如仓库中的测试文件名不同，使用 `rg --files tests | rg "(judge|evidence|summary|aggregation)"` 选取实际对应文件后运行同等集合。

- [ ] **Step 5: 提交结构化 Prompt 修正**

```powershell
git add -- services/agent/src/codeguard_agent/prompts services/agent/evals/matcher.py services/agent/tests/test_prompt_contracts.py
git commit -m "fix(prompts): 对齐证据裁决与评测字段契约"
```

### Task 4: 审查知识片段边界并完成全量验证

**Files:**
- Modify only when a conflict is proven: `services/agent/src/codeguard_agent/prompts/knowledge/**/*.txt`
- Modify: `services/agent/tests/test_prompt_contracts.py`

**Interfaces:**
- Consumes: `RISK_TAG_REVIEWERS` 的 24 标签路由和三个 Agent 基础职责。
- Produces: 每个已路由 reviewer/tag 组合都有不冲突的知识说明；不改变路由本身。

- [ ] **Step 1: 逐个核对 33 个知识片段**

对每个片段检查标题、典型模式、判定要点、严重度、误报判例和排除项。重点核对共享标签 `AUTHORIZATION`、`AUTHENTICATION_SESSION`、`INPUT_VALIDATION`、`INJECTION`、`FILE_PATH_IO`、`SSRF_OUTBOUND`、`DATA_EXPOSURE`、`RESOURCE_LIFECYCLE`、`API_CONTRACT`、`PERFORMANCE` 的 Agent 视角是否互补且不互相拒绝。

- [ ] **Step 2: 仅修正有证据的冲突**

Behavior 的共享片段必须落到运行时触发路径与错误结果；Maintainability 的共享片段必须落到结构、所有权、容量和可演进性；ThreatModel 的共享片段必须要求攻击者可控输入、信任边界或安全影响链。保留合理的标签专属例子，不做纯文风重写。

- [ ] **Step 3: 运行全量验证**

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/ -q
conda run -n codeguard --no-capture-output ruff check src/ tests/test_prompt_contracts.py
conda run -n codeguard --no-capture-output mypy src/
```

Expected: pytest 全部 PASS；Ruff 无错误；mypy 无错误。

- [ ] **Step 4: 运行无工具最小管线验证**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_graph_orchestration.py tests/test_orchestrator.py -q`

Expected: PASS，证明 DirectEngine/无工具图未因 Prompt 契约变化失效。

- [ ] **Step 5: 检查变更边界并提交**

```powershell
git diff --check
git status --short
git add -- services/agent/src/codeguard_agent/prompts/knowledge services/agent/tests/test_prompt_contracts.py
git commit -m "fix(prompts): 校准风险知识领域边界"
```

确认提交不包含 `legacy/`、`recipes.py`、原有 `test_evidence_rules.py` 或 Java Gateway 工作树改动。
