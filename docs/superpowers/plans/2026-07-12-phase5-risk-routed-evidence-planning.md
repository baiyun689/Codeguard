# Phase 5 风险标签驱动的证据规划与裁决链 实施计划

**状态：已完成（2026-07-13）**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 把 Review 之后的证据链从"泛化补查 + 任意工具输出即 supported"重写为"候选证据主题 → 静态策略 → 规划/执行分离 → 结构化 finding（带强度/局限）→ 证据驱动裁决"。

**Architecture:** 新增 `EvidencePlanner` 节点做规划、`EvidenceAgent` 只执行确定策略、`CouncilJudge` 按 finding 强度裁决。所有跨节点关联复用既有 `evidence_requests` / `evidence_notes` 队列，**不新增任何顶层 `ReviewState` 字段**；`candidate evidence tag` / `CandidateDossier` / `EvidenceStrategy` 全是节点内部临时对象。分两个子阶段：**5A 只做纯新增模块（套件全程绿）**，**5B 做原子破坏性替换并删净旧代码（结束时绿）**。

**Tech Stack:** Python 3 / pydantic v2 / dataclass(frozen) / LangGraph / pytest。开发命令统一 `conda run -n codeguard --no-capture-output <cmd>`，工作目录 `services/agent`。

**关联设计稿:** `docs/superpowers/specs/2026-07-12-risk-routed-evidence-planning-phase5-design.md`（本计划的接口基线，§6.4/§6.5 的策略表与术语锚点是权威数据源）。

**两条硬原则（贯穿全程）:**
1. **删净死代码**：不再使用的旧代码/状态字段直接删，不防御式保留。删除集中在 5B。
2. **字段必被消费**：每个新增/修改的模型字段必须有运行时消费者 + 一条验证其影响的测试。设计稿 §5.1 硬约束表逐行落到测试。

## 2026-07-13 实施前审计修订（已获用户确认）

本节是对下方逐步示例代码的约束性修订；若示例与本节、关联设计稿或 `AGENTS.md`
冲突，以本节和设计硬约束为准。任务编号、5A/5B 顺序、验收项和删除清单保持不变。

1. EvidenceAgent 必须先复用 `task.patch` 与 `TaskContextBundle` 中匹配
   `context_kinds` 的事实，只为缺失事实调用 Gateway；`context_kinds`、`target`、
   `question`、`preferred_tools` 都必须有运行时行为影响。请求执行前完整校验策略、目的、
   task 文件边界、有序工具集合和当前 eval profile 的 `enabled_tools`；缓存复用仍为每请求
   生成独立 note，实际新工具调用继续写入 `gathered_context`。
2. AUTHORIZATION / TRANSACTION 的 direct counter 只能来自当前 task 所属方法或类作用域；
   同文件其他方法的注解不得触发 direct drop。全局 sensitive API 输出必须按 task 文件与
   行范围切片。无法解析方法、截断、空结果、工具禁用/失败和 LLM None 一律 insufficient。
3. 候选分类 exact alias 使用规范化等值判断，LLM 输入包含 task patch；初轮与回环复用
   同一 effective judge/classifier LLM 和 structured method。所有新增/改写 prompt 均放在
   `prompts/*.txt`，不得硬编码到 Python。
4. Judge 只保留一套 purpose-aware 裁决入口；孤儿 finding 不得默认 counter。无效 task
   绑定在任何 LLM/去重前 drop；最后一轮禁止悬而未决的 needs_more；生成最终 Issue 时
   应用 severity override，不得原地修改上游 CandidateIssue。
5. 为 23 个具体 RiskTag 与 GENERAL_REVIEW 注册实际可执行的 severity 策略。severity 仅在
   Judge 明确请求的回环轮选择，问题聚焦影响范围/可达性/恢复成本，并优先复用已有 task
   facts 与 findings；没有新增事实时明确 exhausted，不用换 question 重复同一工具调用。
6. Task 9 必须实现可观测数据接线和 eval schema/report 指标，而非只增加 YAML：直接保护
   事实仍被保留的候选率、全 insufficient 候选的最终保留率、最终 Issue 的策略覆盖率、
   最终 Issue 的有效事实覆盖率、注册表 RiskTag 覆盖率、平均实际工具调用数。行为真值由
   graph/evidence/judge 集成测试断言；工具调用成本使用 repo-backed/tool profile 观测。
7. Task 2 与 Task 4 合并为测试全绿的提交，不提交已知红灯。策略 ID 唯一性从原始注册列表
   校验，重复 ID 必须在建索引时失败。删除旧请求路径时一并清理无任务分支写入和失去消费
   者的常量。

---

## 状态字段变更总览（先读，避免误加字段）

**顶层 `ReviewState`：零新增、零删除。** 复用现有 `candidate_issues` / `evidence_requests` / `evidence_notes` / `council_verdicts` / `evidence_round`。

模型内部字段变更：

| 模型 | 新增字段 | 删除字段 | 首个消费者（保证非死字段） |
|---|---|---|---|
| `EvidenceRequest` | `strategy_id` `purpose` | — | 5A：planner 设置 + 单测；5B：agent 读取 |
| `EvidenceFinding`（新模型） | `evidence_id` `source` `observation` `relation` `strength` `limitation` | — | 5B：agent 写、judge 读 |
| `EvidenceNote` | `findings` | `status` `supports` `contradicts` `unknowns` `evidence_ids` | 5B：agent 写、judge 读 |
| `Verdict` / `JudgeDecision` | `requested_purpose` | `suggested_tools` `suggested_target_id`(见 5B-2 核对) | 5B：judge 写、planner 读 |
| 模块级 | — | `build_evidence_requests` `MAX_TOTAL_EVIDENCE_REQUESTS` `EvidenceNoteStatus` `EvidenceJudgment` `_rule_strong_support` | 5B 删除 |

---

# Phase 5A：纯新增模块（套件全程绿）

5A 只添加新代码，不触碰 `graph.py` 的运行时行为。`EvidenceRequest` 的两个新字段是**加法**（带默认值），旧 `build_evidence_requests` 仍能构造合法对象；planner/单测立即消费新字段，故非死字段。

---

## Task 1: EvidenceRequest 增加 strategy_id / purpose（加法）

**Files:**
- Modify: `services/agent/src/codeguard_agent/models/council.py`（`EvidenceRequest` 类，当前 109-130 行）
- Test: `services/agent/tests/test_evidence_models.py`（新建）

- [x] **Step 1: 写失败测试**

新建 `services/agent/tests/test_evidence_models.py`：

```python
from codeguard_agent.models.council import EvidenceRequest


def test_stable_id_includes_strategy_and_purpose():
    """同 candidate/target/question 但 strategy_id 或 purpose 不同 → id 必须不同。"""
    base = dict(candidate_id="c1", target="a/B.java", question="q")
    counter = EvidenceRequest(**base, strategy_id="authorization.counter", purpose="counter")
    support = EvidenceRequest(**base, strategy_id="authorization.support", purpose="support")
    same = EvidenceRequest(**base, strategy_id="authorization.counter", purpose="counter")
    assert counter.id != support.id
    assert counter.id == same.id  # 相同五元组 → 稳定同 id


def test_purpose_defaults_to_counter():
    """未显式指定 purpose 时默认 counter（兼容旧构造路径，5B 前不破坏）。"""
    req = EvidenceRequest(candidate_id="c1")
    assert req.purpose == "counter"
    assert req.strategy_id == ""
```

- [x] **Step 2: 跑测试确认失败**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_evidence_models.py -v`（在 `services/agent` 下）
Expected: FAIL —`EvidenceRequest` 无 `strategy_id`/`purpose` 参数（TypeError / ValidationError）。

- [x] **Step 3: 修改 EvidenceRequest**

在 `models/council.py`，把 `EvidenceRequest`（109-130 行）替换为：

```python
EvidencePurpose = Literal["support", "counter", "severity"]


class EvidenceRequest(BaseModel):
    """候选 issue 对证据的结构化请求（Phase 5：策略驱动）。"""

    id: str = ""
    candidate_id: str
    strategy_id: str = ""
    purpose: EvidencePurpose = "counter"
    target: str = ""
    question: str = ""
    preferred_tools: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def assign_stable_id(self) -> "EvidenceRequest":
        if not self.id:
            payload = "\0".join(
                [
                    self.candidate_id,
                    self.strategy_id,
                    self.purpose,
                    self.target,
                    self.question,
                    *self.preferred_tools,
                ]
            )
            self.id = f"evidence-{sha256(payload.encode('utf-8')).hexdigest()[:16]}"
        return self
```

`EvidencePurpose` 定义放在 `EvidenceRequest` 之前（模块顶部 import 区之后即可）。

- [x] **Step 4: 跑测试确认通过**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_evidence_models.py -v`
Expected: PASS（2 passed）。

- [x] **Step 5: 跑全套确认未破坏**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/ -q && conda run -n codeguard ruff check src/ && conda run -n codeguard mypy src/`
Expected: 全绿。旧 `build_evidence_requests` 构造的 `EvidenceRequest` 现在带默认 `purpose="counter"`，reducer 去重键因 id 算法变化而重算，但行为等价（同一候选同一请求仍稳定）。

- [x] **Step 6: 提交**

```bash
git add services/agent/src/codeguard_agent/models/council.py services/agent/tests/test_evidence_models.py
git commit -m "feat(council): EvidenceRequest 增加 strategy_id/purpose 稳定 ID"
```

---

## Task 2: evidence_rules 包骨架 + EvidenceStrategy + 完整性测试（先红）

**Files:**
- Create: `services/agent/src/codeguard_agent/pipeline/evidence_rules/__init__.py`
- Create: `services/agent/src/codeguard_agent/pipeline/evidence_rules/types.py`
- Create: `services/agent/src/codeguard_agent/pipeline/evidence_rules/security.py`（本任务只放空 `STRATEGIES` 列表）
- Create: `services/agent/src/codeguard_agent/pipeline/evidence_rules/behavior.py`（同上）
- Create: `services/agent/src/codeguard_agent/pipeline/evidence_rules/maintainability.py`（同上）
- Test: `services/agent/tests/test_evidence_rules.py`（新建）

设计参考：`pipeline/risk_rules/`（catalog.py 聚合、三领域分文件）是本包的范式。

- [x] **Step 1: 定义 types.py（策略值对象，不进 State）**

`pipeline/evidence_rules/types.py`：

```python
"""EvidenceStrategy 及其值对象。全部为不可变 dataclass，不进图 State。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal

from codeguard_agent.models.council import EvidencePurpose
from codeguard_agent.models.tasks import RiskTag

if TYPE_CHECKING:
    from codeguard_agent.pipeline.evidence_planner import CandidateDossier

ToolName = Literal[
    "get_file_content", "find_sensitive_apis", "find_callers", "get_code_metrics"
]


@dataclass(frozen=True)
class ToolCallSpec:
    """一次工具调用配方。arguments 用有序元组以便 canonical 缓存键。"""

    tool_name: ToolName
    arguments: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class EvidenceStrategy:
    id: str
    tags: frozenset[RiskTag]
    purpose: EvidencePurpose
    priority: int
    question_template: str
    context_kinds: tuple[str, ...]
    allowed_tools: tuple[ToolName, ...]
    build_tool_calls: Callable[["CandidateDossier"], list[ToolCallSpec]]
```

- [x] **Step 2: 三领域空列表 + __init__ 聚合**

`security.py` / `behavior.py` / `maintainability.py` 各自：

```python
"""<领域> 域 EvidenceStrategy。Task 4 填充。"""

from __future__ import annotations

from codeguard_agent.pipeline.evidence_rules.types import EvidenceStrategy

STRATEGIES: list[EvidenceStrategy] = []
```

`__init__.py`：

```python
"""evidence_rules 深 module：对外只暴露 strategies_for / resolve_candidate_evidence_tag。

内部按 security / behavior / maintainability 三组组织，调用方不感知分文件。
"""

from __future__ import annotations

from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence_rules import behavior, maintainability, security
from codeguard_agent.pipeline.evidence_rules.types import EvidenceStrategy, ToolCallSpec

_ALL: list[EvidenceStrategy] = [
    *security.STRATEGIES,
    *behavior.STRATEGIES,
    *maintainability.STRATEGIES,
]


def _build_index() -> dict[RiskTag, list[EvidenceStrategy]]:
    index: dict[RiskTag, list[EvidenceStrategy]] = {}
    for strat in _ALL:
        for tag in strat.tags:
            index.setdefault(tag, []).append(strat)
    for tag, strats in index.items():
        strats.sort(key=lambda s: s.priority)
    return index


STRATEGIES_BY_TAG: dict[RiskTag, list[EvidenceStrategy]] = _build_index()
STRATEGIES_BY_ID: dict[str, EvidenceStrategy] = {s.id: s for s in _ALL}


def strategies_for(tag: RiskTag, purpose: str | None = None) -> list[EvidenceStrategy]:
    """返回某 candidate evidence tag 的策略，按 priority 升序；可按 purpose 过滤。"""
    strats = STRATEGIES_BY_TAG.get(tag, [])
    if purpose is not None:
        strats = [s for s in strats if s.purpose == purpose]
    return strats


__all__ = ["EvidenceStrategy", "ToolCallSpec", "STRATEGIES_BY_TAG", "STRATEGIES_BY_ID", "strategies_for"]
```

- [x] **Step 3: 写完整性测试（此时必然失败）**

`services/agent/tests/test_evidence_rules.py`：

```python
from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence_rules import STRATEGIES_BY_ID, STRATEGIES_BY_TAG, strategies_for

_CONCRETE = [t for t in RiskTag if t != RiskTag.GENERAL_REVIEW]


def test_every_tag_registered():
    assert set(STRATEGIES_BY_TAG) == set(RiskTag)  # 含 GENERAL_REVIEW


def test_every_concrete_tag_has_counter_and_support():
    for tag in _CONCRETE:
        purposes = {s.purpose for s in strategies_for(tag)}
        assert "counter" in purposes, f"{tag} 缺 counter 策略"
        assert "support" in purposes, f"{tag} 缺 support 策略"


def test_strategy_ids_unique():
    ids = [s.id for s in STRATEGIES_BY_ID.values()]
    assert len(ids) == len(set(ids))


def test_strategy_questions_and_tools_valid():
    allowed = {"get_file_content", "find_sensitive_apis", "find_callers", "get_code_metrics"}
    for strat in STRATEGIES_BY_ID.values():
        assert strat.question_template.strip(), f"{strat.id} 问题模板为空"
        assert set(strat.allowed_tools) <= allowed, f"{strat.id} 含非法工具"
```

- [x] **Step 4: 跑测试确认失败**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_evidence_rules.py -v`
Expected: FAIL —`test_every_tag_registered` 报 `STRATEGIES_BY_TAG` 为空。（这是预期红灯，Task 4 填充后转绿。）

- [x] **Step 5: 跑全套确认其他测试未破坏**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/ -q`
Expected: 除 `test_evidence_rules.py` 外全绿（新包未被任何运行路径 import）。

- [x] **Step 6: 提交**

```bash
git add services/agent/src/codeguard_agent/pipeline/evidence_rules/ services/agent/tests/test_evidence_rules.py
git commit -m "feat(evidence): evidence_rules 包骨架与完整性测试"
```

> 注：本 commit 留一个已知红灯测试文件。若执行框架要求每 commit 全绿，可将 Task 2 与 Task 4 合并为一个 commit（先骨架后填充），但保持步骤顺序不变。

---

## Task 3: 候选证据主题解析 resolve_candidate_evidence_tag

**Files:**
- Create: `services/agent/src/codeguard_agent/pipeline/evidence_rules/terms.py`（`CANDIDATE_TAG_TERMS`）
- Create: `services/agent/src/codeguard_agent/pipeline/evidence_rules/classify.py`（解析算法）
- Modify: `services/agent/src/codeguard_agent/pipeline/evidence_rules/__init__.py`（导出 `resolve_candidate_evidence_tag`）
- Test: `services/agent/tests/test_candidate_tag_resolution.py`（新建）

- [x] **Step 1: 定义术语表 terms.py**

按设计稿 §6.5 锚点表为**全部 23 个具体标签**登记术语。结构：

```python
"""候选证据主题术语表。候选分类专用，不复用 diff 风险命中词。"""

from __future__ import annotations

from dataclasses import dataclass, field

from codeguard_agent.models.tasks import RiskTag


@dataclass(frozen=True)
class TagTerms:
    exact_type_aliases: frozenset[str] = field(default_factory=frozenset)
    strong_phrases: frozenset[str] = field(default_factory=frozenset)
    weak_terms: frozenset[str] = field(default_factory=frozenset)


CANDIDATE_TAG_TERMS: dict[RiskTag, TagTerms] = {
    RiskTag.AUTHORIZATION: TagTerms(
        exact_type_aliases=frozenset({"越权", "authorization", "access-control"}),
        strong_phrases=frozenset({"鉴权", "授权", "越权", "access control", "ownership", "permission"}),
        weak_terms=frozenset({"权限", "role", "校验"}),
    ),
    RiskTag.NULL_STATE_SAFETY: TagTerms(
        exact_type_aliases=frozenset({"空指针", "npe", "null-pointer"}),
        strong_phrases=frozenset({"空指针", "null", "未初始化", "nullable", "optional"}),
        weak_terms=frozenset({"为空", "判空"}),
    ),
    RiskTag.INJECTION: TagTerms(
        exact_type_aliases=frozenset({"注入", "injection", "sql-injection"}),
        strong_phrases=frozenset({"注入", "sql injection", "命令注入", "拼接查询", "动态表达式"}),
        weak_terms=frozenset({"拼接", "转义"}),
    ),
    # … 其余 20 个具体标签：逐条按设计稿 §6.5 锚点表登记
    # exact_type_aliases 取该类问题最典型的 type 值；strong_phrases 取锚点表主词；
    # weak_terms 取容易与他类混淆的泛词。GENERAL_REVIEW 不登记（无术语，走兜底）。
}
```

**执行者注意：** 必须为设计稿 §6.5 表中全部 23 个具体标签补齐条目，缺任一标签 Step 3 完整性子测试会失败。GENERAL_REVIEW 不进此表。

- [x] **Step 2: 定义解析算法 classify.py**

```python
"""候选证据主题解析：规则计分 → 歧义则受限 LLM 分类 → 兜底 GENERAL_REVIEW。"""

from __future__ import annotations

from typing import Literal, TYPE_CHECKING

from pydantic import BaseModel

from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence_rules.terms import CANDIDATE_TAG_TERMS

if TYPE_CHECKING:
    from codeguard_agent.pipeline.evidence_planner import CandidateDossier


class CandidateTagResolution(BaseModel):
    tag: RiskTag
    confidence: float
    source: Literal["rule", "llm", "general"]
    reason: str


class _LLMTagChoice(BaseModel):
    """受限 LLM 分类的结构化输出。"""

    tag: str
    confidence: float


def _norm(text: str) -> str:
    return (text or "").lower()


def _score_tags(type_s: str, claim_s: str, suggestion_s: str) -> dict[RiskTag, int]:
    scores: dict[RiskTag, int] = {}
    for tag, terms in CANDIDATE_TAG_TERMS.items():
        score = 0
        if any(a in type_s for a in terms.exact_type_aliases):
            score += 8
        elif any(p in type_s for p in terms.strong_phrases):
            score += 6
        if any(p in claim_s for p in terms.strong_phrases):
            score += 4
        elif any(w in claim_s for w in terms.weak_terms):
            score += 1
        if any(p in suggestion_s for p in terms.strong_phrases):
            score += 1
        if score:
            scores[tag] = score
    return scores


def is_ambiguous(scores: dict[RiskTag, int]) -> bool:
    ordered = sorted(scores.values(), reverse=True)
    top = ordered[0] if ordered else 0
    second = ordered[1] if len(ordered) > 1 else 0
    winners = sum(score == top for score in ordered)
    return top < 4 or winners != 1 or top - second < 2


def resolve_candidate_evidence_tag(
    dossier: "CandidateDossier",
    classifier_llm,
    *,
    structured_method: str,
) -> CandidateTagResolution:
    c = dossier.candidate
    type_s, claim_s, sugg_s = _norm(c.type), _norm(c.claim), _norm(c.suggestion)
    scores = _score_tags(type_s, claim_s, sugg_s)

    if not is_ambiguous(scores):
        top_tag = max(scores, key=lambda t: scores[t])
        exact = scores[top_tag] >= 8
        return CandidateTagResolution(
            tag=top_tag,
            confidence=0.95 if exact else 0.85,
            source="rule",
            reason=f"规则唯一命中 {top_tag.value} 得分={scores[top_tag]}",
        )

    if classifier_llm is not None:
        choice = _classify_with_llm(dossier, classifier_llm, structured_method=structured_method)
        if choice is not None:
            return choice

    return CandidateTagResolution(
        tag=RiskTag.GENERAL_REVIEW,
        confidence=0.5,
        source="general",
        reason="候选语义歧义且无可用分类，降级 GENERAL_REVIEW",
    )


def _classify_with_llm(dossier, classifier_llm, *, structured_method: str):
    from codeguard_agent.llm.client import invoke_with_retry

    c = dossier.candidate
    valid = {t.value for t in RiskTag}
    task_tags = ""
    if dossier.risk_profile is not None:
        task_tags = ",".join(t.value for t in dossier.risk_profile.tag_scores)
    system = (
        "你是代码审查证据分类器。只能从给定枚举里选择候选真正在声称的问题类别。"
        "task 标签仅供参考，不代表候选结论。不要解释、不要新增主张。"
    )
    human = (
        f"候选 type: {c.type}\n候选 claim: {c.claim}\n候选 suggestion: {c.suggestion}\n"
        f"（仅参考）task 先验标签: {task_tags}\n"
        f"可选类别: {sorted(valid)}\n返回 tag 与 confidence。"
    )
    try:
        structured = classifier_llm.with_structured_output(_LLMTagChoice, method=structured_method)
        result = invoke_with_retry(structured, [("system", system), ("human", human)], max_retries=1)
    except Exception:
        return None
    if not isinstance(result, _LLMTagChoice) or result.tag not in valid or result.confidence < 0.75:
        return None
    return CandidateTagResolution(
        tag=RiskTag(result.tag), confidence=result.confidence, source="llm",
        reason=f"LLM 受限分类 conf={result.confidence:.2f}",
    )
```

在 `__init__.py` 增加 `from codeguard_agent.pipeline.evidence_rules.classify import resolve_candidate_evidence_tag, CandidateTagResolution` 并加入 `__all__`。

- [x] **Step 3: 写测试**

`services/agent/tests/test_candidate_tag_resolution.py`（用轻量假 dossier，避免依赖尚未建的 planner——构造一个带 `.candidate` / `.risk_profile` 属性的简单对象）：

```python
from types import SimpleNamespace

from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence_rules.classify import is_ambiguous, resolve_candidate_evidence_tag
from codeguard_agent.pipeline.evidence_rules.terms import CANDIDATE_TAG_TERMS


def _dossier(type_="", claim="", suggestion=""):
    cand = CandidateIssue(
        id="c1", task_id="t1", source_agent="behavior", file="A.java",
        type=type_, severity_proposal=Severity.WARNING, claim=claim, suggestion=suggestion,
    )
    return SimpleNamespace(candidate=cand, risk_profile=None)


def test_terms_cover_all_concrete_tags():
    concrete = {t for t in RiskTag if t != RiskTag.GENERAL_REVIEW}
    assert set(CANDIDATE_TAG_TERMS) == concrete


def test_exact_type_returns_rule_without_llm():
    r = resolve_candidate_evidence_tag(_dossier(type_="空指针"), None, structured_method="function_calling")
    assert r.tag == RiskTag.NULL_STATE_SAFETY and r.source == "rule" and r.confidence == 0.95


def test_strong_claim_injection_no_llm():
    r = resolve_candidate_evidence_tag(
        _dossier(claim="动态拼接 SQL 可能导致注入"), None, structured_method="function_calling"
    )
    assert r.tag == RiskTag.INJECTION and r.source == "rule"


def test_ambiguous_without_llm_falls_back_general():
    r = resolve_candidate_evidence_tag(
        _dossier(type_="逻辑问题", claim="这里可能有问题"), None, structured_method="function_calling"
    )
    assert r.tag == RiskTag.GENERAL_REVIEW and r.source == "general"


def test_is_ambiguous_thresholds():
    assert is_ambiguous({}) is True
    assert is_ambiguous({RiskTag.INJECTION: 3}) is True          # top<4
    assert is_ambiguous({RiskTag.INJECTION: 6, RiskTag.SQL_DATA_ACCESS: 5}) is True  # 分差<2
    assert is_ambiguous({RiskTag.INJECTION: 6, RiskTag.SQL_DATA_ACCESS: 1}) is False
```

- [x] **Step 4: 跑测试**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_candidate_tag_resolution.py -v`
Expected: PASS（若 `test_terms_cover_all_concrete_tags` 失败，说明 Task 3 Step 1 术语表未补齐 23 标签——补齐再跑）。

- [x] **Step 5: 跑全套 + lint + type**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/ -q && conda run -n codeguard ruff check src/ && conda run -n codeguard mypy src/`
Expected: 除 Task 2 遗留的 `test_evidence_rules.py` 红灯外全绿。

- [x] **Step 6: 提交**

```bash
git add services/agent/src/codeguard_agent/pipeline/evidence_rules/ services/agent/tests/test_candidate_tag_resolution.py
git commit -m "feat(evidence): 候选证据主题解析与术语表"
```

---

## Task 4: 填充 23 标签全量策略（转绿完整性测试）

**Files:**
- Modify: `evidence_rules/security.py`（前 10 标签：AUTHORIZATION … DATA_EXPOSURE）
- Modify: `evidence_rules/behavior.py`（TRANSACTION_ATOMICITY … API_CONTRACT 共 9 标签）
- Modify: `evidence_rules/maintainability.py`（PERFORMANCE … OBSERVABILITY_TESTABILITY 共 4 标签）
- Modify: `evidence_rules/__init__.py`（加 GENERAL_REVIEW 策略；见下）
- Test: 复用 `tests/test_evidence_rules.py`（Task 2 已写）+ 增补 `build_tool_calls` 用例

数据源：设计稿 §6.4 全量策略语义表（每行的 counter/support 语义、context/工具）。每个具体标签至少 `<slug>.counter` + `<slug>.support` 两条。

- [x] **Step 1: 定义共享工具配方助手**

在 `evidence_rules/recipes.py`（新建）集中放 `build_tool_calls` 用的工厂，复用 `context_rules.resolve_method_name`：

```python
"""策略工具配方工厂。Python 不直接读仓库，只产出 Gateway 工具调用。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from codeguard_agent.pipeline.context_rules import resolve_method_name
from codeguard_agent.pipeline.evidence_rules.types import ToolCallSpec

if TYPE_CHECKING:
    from codeguard_agent.pipeline.evidence_planner import CandidateDossier


def file_only(dossier: "CandidateDossier") -> list[ToolCallSpec]:
    return [ToolCallSpec("get_file_content", (("target", dossier.task.file),))]


def file_and_sensitive(dossier: "CandidateDossier") -> list[ToolCallSpec]:
    return [
        ToolCallSpec("get_file_content", (("target", dossier.task.file),)),
        ToolCallSpec("find_sensitive_apis", ()),
    ]


def file_and_metrics(dossier: "CandidateDossier") -> list[ToolCallSpec]:
    return [
        ToolCallSpec("get_file_content", (("target", dossier.task.file),)),
        ToolCallSpec("get_code_metrics", (("file_path", dossier.task.file),)),
    ]


def callers_upstream(dossier: "CandidateDossier") -> list[ToolCallSpec]:
    """第二轮 counter_upstream 专用：解析不到方法名则返回空（Agent 记 no_method_resolved）。"""
    method = resolve_method_name(dossier)  # 见下方核对步骤
    if not method:
        return []
    return [ToolCallSpec("find_callers", (("query", f"{dossier.task.file}#{method}"),))]
```

> **核对 `resolve_method_name` 真实签名（必做）**：先 `grep -n "def resolve_method_name" services/agent/src/codeguard_agent/pipeline/context_rules.py`。若它接收的是 `(ReviewTask, ast_fact)` 而非 dossier，则在 `recipes.py` 内改为从 `dossier.task` + `dossier.context_bundle` 取 `ast_structure` fact 后调用，不要臆造参数。

- [x] **Step 2: 填充 security.py（示范 3 条，其余照抄模式）**

```python
"""security 域 EvidenceStrategy（前 10 标签）。"""

from __future__ import annotations

from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence_rules import recipes
from codeguard_agent.pipeline.evidence_rules.types import EvidenceStrategy

STRATEGIES: list[EvidenceStrategy] = [
    EvidenceStrategy(
        id="authorization.counter",
        tags=frozenset({RiskTag.AUTHORIZATION}),
        purpose="counter",
        priority=10,
        question_template="当前方法/类或调用方是否已有鉴权或资源归属校验，足以排除越权？",
        context_kinds=("sensitive_api", "ast_structure"),
        allowed_tools=("get_file_content", "find_sensitive_apis"),
        build_tool_calls=recipes.file_and_sensitive,
    ),
    EvidenceStrategy(
        id="authorization.support",
        tags=frozenset({RiskTag.AUTHORIZATION}),
        purpose="support",
        priority=20,
        question_template="该路径是否真实执行敏感操作或访问受保护资源？",
        context_kinds=("sensitive_api", "ast_structure"),
        allowed_tools=("get_file_content", "find_sensitive_apis"),
        build_tool_calls=recipes.file_and_sensitive,
    ),
    EvidenceStrategy(
        id="authorization.counter_upstream",
        tags=frozenset({RiskTag.AUTHORIZATION}),
        purpose="counter",
        priority=30,  # 只在第二轮被选（优先级低于初轮 counter）
        question_template="上游调用方是否已完成鉴权，使当前方法无需再校验？",
        context_kinds=("ast_structure",),
        allowed_tools=("find_callers",),
        build_tool_calls=recipes.callers_upstream,
    ),
    # AUTHENTICATION_SESSION / WEB_SECURITY_CONFIG / INPUT_VALIDATION / INJECTION /
    # SQL_DATA_ACCESS / FILE_PATH_IO / SSRF_OUTBOUND / CONFIG_SECURITY / DATA_EXPOSURE：
    # 各按设计稿 §6.4 对应行登记 counter+support，context_kinds/allowed_tools 取该行"context/工具"列，
    # build_tool_calls 选 recipes 中匹配的工厂（纯文件类→file_only，含 sensitive→file_and_sensitive）。
]
```

**执行者注意：** `priority` 约定——初轮 counter=10、support=20、第二轮 `counter_upstream`=30。`strategies_for` 已按 priority 升序，Planner 初轮取 counter(10)/support(20)，回环取剩余最小 priority。

- [x] **Step 3: 填充 behavior.py / maintainability.py**

同模式覆盖各自标签。behavior 域多数需要 caller 语义：counter 用 `file_only` 或 `file_and_sensitive`，第二轮 `counter_upstream` 用 `callers_upstream`。maintainability 域用 `file_and_metrics`。

- [x] **Step 4: GENERAL_REVIEW 策略（放 __init__.py 或单独 general.py）**

```python
# 追加到某领域文件或新建 general.py，并在 __init__ 的 _ALL 里纳入
GENERAL_STRATEGIES = [
    EvidenceStrategy(
        id="general.counter", tags=frozenset({RiskTag.GENERAL_REVIEW}), purpose="counter",
        priority=10, question_template="task 中是否存在直接推翻候选主张的保护或前置条件？",
        context_kinds=("task_patch",), allowed_tools=("get_file_content",),
        build_tool_calls=recipes.file_only,
    ),
    EvidenceStrategy(
        id="general.support", tags=frozenset({RiskTag.GENERAL_REVIEW}), purpose="support",
        priority=20, question_template="task 中是否存在候选主张所依赖的直接事实？",
        context_kinds=("task_patch",), allowed_tools=("get_file_content",),
        build_tool_calls=recipes.file_only,
    ),
]
```

- [x] **Step 5: 增补 build_tool_calls 单测**

在 `tests/test_evidence_rules.py` 追加：

```python
from types import SimpleNamespace
from codeguard_agent.models.tasks import ReviewTask
from codeguard_agent.pipeline.evidence_rules import strategies_for
from codeguard_agent.models.tasks import RiskTag


def _dossier_for_recipe():
    task = ReviewTask(id="t1", file="a/B.java", patch="", changed_lines=[10])
    return SimpleNamespace(task=task, context_bundle=None, risk_profile=None)


def test_counter_upstream_no_method_returns_empty():
    """解析不到方法名时 counter_upstream 配方返回空，不产生 file#line 伪查询。"""
    strat = next(s for s in strategies_for(RiskTag.AUTHORIZATION) if s.id.endswith("counter_upstream"))
    calls = strat.build_tool_calls(_dossier_for_recipe())
    assert calls == []  # 无 ast fact → 无 method → 空


def test_authorization_counter_builds_file_and_sensitive():
    strat = next(s for s in strategies_for(RiskTag.AUTHORIZATION, purpose="counter") if s.id == "authorization.counter")
    calls = strat.build_tool_calls(_dossier_for_recipe())
    names = [c.tool_name for c in calls]
    assert "get_file_content" in names and "find_sensitive_apis" in names
```

- [x] **Step 6: 跑完整性测试（应全绿）**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_evidence_rules.py -v`
Expected: PASS（`test_every_tag_registered` 等全过——若报某标签缺 counter/support，补该标签策略）。

- [x] **Step 7: 跑全套 + lint + type**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/ -q && conda run -n codeguard ruff check src/ && conda run -n codeguard mypy src/`
Expected: 全绿（5A 至此套件应完全无红灯）。

- [x] **Step 8: 提交**

```bash
git add services/agent/src/codeguard_agent/pipeline/evidence_rules/ services/agent/tests/test_evidence_rules.py
git commit -m "feat(evidence): 覆盖 23 个 RiskTag 的补证/反证策略"
```

---

## Task 5: EvidencePlanner 纯函数

**Files:**
- Create: `services/agent/src/codeguard_agent/pipeline/evidence_planner.py`
- Test: `services/agent/tests/test_evidence_planner.py`（新建）

- [x] **Step 1: 定义 dossier / plan / plan_evidence**

```python
"""EvidencePlanner：候选证据主题 → 策略选择 → EvidenceRequest。纯函数，不调工具。"""

from __future__ import annotations

from dataclasses import dataclass, field

from codeguard_agent.models.council import (
    CandidateIssue,
    EvidenceNote,
    EvidenceRequest,
    Verdict,
)
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import ReviewTask, RiskProfile, TaskContextBundle
from codeguard_agent.pipeline.evidence_rules import resolve_candidate_evidence_tag, strategies_for


@dataclass(frozen=True)
class CandidateDossier:
    candidate: CandidateIssue
    task: ReviewTask
    risk_profile: RiskProfile | None
    context_bundle: TaskContextBundle | None
    requests: tuple[EvidenceRequest, ...]
    notes: tuple[EvidenceNote, ...]
    latest_verdict: Verdict | None


@dataclass
class EvidencePlan:
    requests: list[EvidenceRequest] = field(default_factory=list)
    trace: list[tuple[str, str]] = field(default_factory=list)  # (event, detail)


def _decide_tier_react(profile: RiskProfile | None) -> bool:
    """沿用 Phase 4 高风险定义：max(tag_scores) >= 2。"""
    if profile is None or not profile.tag_scores:
        return False
    return max(profile.tag_scores.values()) >= 2


def _needs_support(cand: CandidateIssue, profile: RiskProfile | None) -> bool:
    return (
        cand.severity_proposal == Severity.CRITICAL
        or _decide_tier_react(profile)
        or cand.confidence < 0.9
    )


def _make_request(dossier: CandidateDossier, strat, question: str) -> EvidenceRequest:
    calls = strat.build_tool_calls(dossier)
    tools: list[str] = []
    for c in calls:
        if c.tool_name not in tools:
            tools.append(c.tool_name)
    return EvidenceRequest(
        candidate_id=dossier.candidate.id,
        strategy_id=strat.id,
        purpose=strat.purpose,
        target=dossier.task.file,
        question=question,
        preferred_tools=tools,
    )


def _existing_strategy_ids(dossier: CandidateDossier) -> set[str]:
    ids = {r.strategy_id for r in dossier.requests}
    # note 关联的 request 也算已执行
    return ids


def plan_evidence(
    dossiers: list[CandidateDossier],
    *,
    evidence_round: int,
    classifier_llm,
    structured_method: str,
) -> EvidencePlan:
    plan = EvidencePlan()
    if evidence_round == 0:
        _plan_initial(dossiers, plan, classifier_llm, structured_method)
    else:
        _plan_followup(dossiers, plan)
    return plan


def _plan_initial(dossiers, plan, classifier_llm, structured_method) -> None:
    resolved: dict[str, object] = {}
    for d in dossiers:
        res = resolve_candidate_evidence_tag(d, classifier_llm, structured_method=structured_method)
        resolved[d.candidate.id] = res
        plan.trace.append(
            ("candidate_evidence_tag_resolved", f"{d.candidate.id}->{res.tag.value}({res.source})")
        )
    # 第一遍：所有候选 counter
    for d in dossiers:
        tag = resolved[d.candidate.id].tag
        counters = strategies_for(tag, purpose="counter")
        if not counters:
            plan.trace.append(("evidence_plan_skipped", f"{d.candidate.id} 无 counter 策略"))
            continue
        strat = counters[0]
        plan.requests.append(_make_request(d, strat, strat.question_template))
        plan.trace.append(("evidence_planned", f"{d.candidate.id} counter={strat.id}"))
    # 第二遍：高风险候选追加 support
    for d in dossiers:
        if not _needs_support(d.candidate, d.risk_profile):
            continue
        tag = resolved[d.candidate.id].tag
        supports = strategies_for(tag, purpose="support")
        if not supports:
            continue
        strat = supports[0]
        plan.requests.append(_make_request(d, strat, strat.question_template))
        plan.trace.append(("evidence_planned", f"{d.candidate.id} support={strat.id}"))


def _plan_followup(dossiers, plan) -> None:
    for d in dossiers:
        v = d.latest_verdict
        if v is None or v.action != "needs_more_evidence":
            continue
        if v.requested_purpose is None:
            plan.trace.append(("evidence_plan_invalid_verdict", d.candidate.id))
            continue
        done = _existing_strategy_ids(d)
        # 需重新解析 tag：followup 也用规则（无 llm 也可，歧义走 GENERAL）
        from codeguard_agent.pipeline.evidence_rules import resolve_candidate_evidence_tag as _r
        tag = _r(d, None, structured_method="function_calling").tag
        nexts = [s for s in strategies_for(tag, purpose=v.requested_purpose) if s.id not in done]
        if not nexts:
            plan.trace.append(("evidence_plan_exhausted", f"{d.candidate.id} purpose={v.requested_purpose}"))
            continue
        strat = nexts[0]
        plan.requests.append(_make_request(d, strat, strat.question_template))
        plan.trace.append(("evidence_planned", f"{d.candidate.id} followup={strat.id}"))
```

> **核对 `TaskContextBundle` 导入位置**：`grep -n "class TaskContextBundle" services/agent/src/codeguard_agent/models/tasks.py`。若在别的模块，改 import。

- [x] **Step 2: 写测试**

`services/agent/tests/test_evidence_planner.py`：

```python
from codeguard_agent.models.council import CandidateIssue, EvidenceRequest, Verdict
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import ReviewTask, RiskProfile, RiskTag
from codeguard_agent.pipeline.evidence_planner import CandidateDossier, plan_evidence


def _cand(cid, claim="空指针 null 解引用", conf=1.0, sev=Severity.WARNING):
    return CandidateIssue(
        id=cid, task_id="t1", source_agent="behavior", file="A.java",
        type="空指针", severity_proposal=sev, claim=claim, confidence=conf,
    )


def _dossier(cand, profile=None, verdict=None, requests=(), notes=()):
    task = ReviewTask(id="t1", file="A.java", patch="", changed_lines=[1])
    return CandidateDossier(
        candidate=cand, task=task, risk_profile=profile, context_bundle=None,
        requests=tuple(requests), notes=tuple(notes), latest_verdict=verdict,
    )


def test_initial_every_candidate_gets_counter():
    ds = [_dossier(_cand("c1")), _dossier(_cand("c2"))]
    plan = plan_evidence(ds, evidence_round=0, classifier_llm=None, structured_method="function_calling")
    counters = [r for r in plan.requests if r.purpose == "counter"]
    assert {r.candidate_id for r in counters} == {"c1", "c2"}


def test_high_risk_adds_support():
    prof = RiskProfile(task_id="t1", tag_scores={RiskTag.NULL_STATE_SAFETY: 3})
    ds = [_dossier(_cand("c1", conf=1.0), profile=prof)]
    plan = plan_evidence(ds, evidence_round=0, classifier_llm=None, structured_method="function_calling")
    purposes = {r.purpose for r in plan.requests if r.candidate_id == "c1"}
    assert purposes == {"counter", "support"}


def test_low_conf_adds_support():
    ds = [_dossier(_cand("c1", conf=0.5))]
    plan = plan_evidence(ds, evidence_round=0, classifier_llm=None, structured_method="function_calling")
    assert any(r.purpose == "support" for r in plan.requests)


def test_thirty_candidates_all_get_counter_no_cap():
    ds = [_dossier(_cand(f"c{i}", conf=1.0)) for i in range(30)]
    plan = plan_evidence(ds, evidence_round=0, classifier_llm=None, structured_method="function_calling")
    counters = {r.candidate_id for r in plan.requests if r.purpose == "counter"}
    assert len(counters) == 30  # 无 20 截断


def test_followup_only_needs_more():
    v_more = Verdict(candidate_id="c1", action="needs_more_evidence", reason_code="x", requested_purpose="counter")
    prior = EvidenceRequest(candidate_id="c1", strategy_id="null_state_safety.counter", purpose="counter")
    ds = [
        _dossier(_cand("c1"), verdict=v_more, requests=[prior]),
        _dossier(_cand("c2"), verdict=Verdict(candidate_id="c2", action="keep", reason_code="x")),
    ]
    plan = plan_evidence(ds, evidence_round=1, classifier_llm=None, structured_method="function_calling")
    assert all(r.candidate_id == "c1" for r in plan.requests)
    assert all(r.strategy_id != "null_state_safety.counter" for r in plan.requests)  # 排除已执行


def test_followup_exhausted_records_trace():
    v = Verdict(candidate_id="c1", action="needs_more_evidence", reason_code="x", requested_purpose="support")
    # 把该 tag 所有 support 策略都标记为已执行
    from codeguard_agent.pipeline.evidence_rules import strategies_for
    done = [EvidenceRequest(candidate_id="c1", strategy_id=s.id, purpose="support")
            for s in strategies_for(RiskTag.NULL_STATE_SAFETY, purpose="support")]
    ds = [_dossier(_cand("c1"), verdict=v, requests=done)]
    plan = plan_evidence(ds, evidence_round=1, classifier_llm=None, structured_method="function_calling")
    assert any(ev == "evidence_plan_exhausted" for ev, _ in plan.trace)
```

> 此处 `Verdict(requested_purpose=...)` 依赖 5B-2 的字段。因此 **Task 5 需把 `Verdict.requested_purpose` 提前到本任务加**（加法、带默认 None、planner 立即消费——非死字段）。在 `models/council.py` 的 `Verdict` dataclass 和 `JudgeDecision` 增加 `requested_purpose: EvidencePurpose | None = None`，`suggested_tools` 暂留（5B 删）。

- [x] **Step 3: 加 Verdict.requested_purpose（加法）**

`models/council.py`：`Verdict` dataclass 增 `requested_purpose: "EvidencePurpose | None" = None`；`JudgeDecision` 增 `requested_purpose: EvidencePurpose | None = None`。

- [x] **Step 4: 跑测试**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_evidence_planner.py -v`
Expected: PASS。

- [x] **Step 5: 跑全套 + lint + type**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/ -q && conda run -n codeguard ruff check src/ && conda run -n codeguard mypy src/`
Expected: 全绿。

- [x] **Step 6: 提交**

```bash
git add services/agent/src/codeguard_agent/pipeline/evidence_planner.py services/agent/src/codeguard_agent/models/council.py services/agent/tests/test_evidence_planner.py
git commit -m "feat(evidence): EvidencePlanner 两遍规划与回环选择"
```

**5A 验收：** `pytest tests/ -q` 全绿、`ruff`/`mypy` clean。图运行时行为未变，Planner/策略尚未接线。

---

# Phase 5B：原子破坏性替换 + 删净旧代码（结束时绿）

5B 每个 commit 内部可能短暂破坏 `graph.py`，但**每个 commit 结束时必须全套绿**。顺序：先重写模型（连带改 graph 旧读点为新读点）→ 新 Agent → 新 Judge → 图接线删旧。为保证原子性，**5B-1 到 5B-3 若无法各自独立通过，可合并为一个大 commit**；优先保证可验收而非 commit 数。

---

## Task 6: EvidenceNote → findings 模型重写 + Agent 重写（原子）

**Files:**
- Modify: `models/council.py`（`EvidenceNote` 重写、加 `EvidenceFinding`、删 `EvidenceNoteStatus`/`EvidenceJudgment`/`build_evidence_requests`）
- Create: `services/agent/src/codeguard_agent/pipeline/evidence_agent.py`（`collect_evidence`）
- Modify: `graph.py`（`_evidence_agent_node` 改为薄 adapter 调 `collect_evidence`；删旧内联实现）
- Test: `services/agent/tests/test_evidence_agent.py`（新建）；改写受影响的旧证据测试

- [x] **Step 1: 重写模型（council.py）**

删除 `EvidenceNoteStatus`（97 行）、`EvidenceJudgment`（102-108 行）、`build_evidence_requests`（184-204 行）。`EvidenceNote`（207-221 行）替换为：

```python
class EvidenceFinding(BaseModel):
    evidence_id: str
    source: str                   # task_patch / context:<kind> / tool:<name>
    observation: str
    relation: Literal["supports", "contradicts", "insufficient"]
    strength: Literal["direct", "contextual"]
    limitation: str = ""


class EvidenceNote(BaseModel):
    request_id: str
    candidate_id: str
    findings: list[EvidenceFinding] = Field(default_factory=list)
```

- [x] **Step 2: 写 collect_evidence（evidence_agent.py）**

```python
"""EvidenceAgent：按策略取事实，LLM 只解释关系；未找到永远 insufficient。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel

from codeguard_agent.models.council import EvidenceFinding, EvidenceNote, EvidenceRequest
from codeguard_agent.pipeline.evidence_planner import CandidateDossier
from codeguard_agent.pipeline.evidence_rules import STRATEGIES_BY_ID


class _RelationJudgment(BaseModel):
    relation: Literal["supports", "contradicts", "insufficient"]
    strength: Literal["direct", "contextual"]
    observation: str = ""
    limitation: str = ""


@dataclass
class EvidenceBatch:
    notes: list[EvidenceNote] = field(default_factory=list)
    trace: list[tuple[str, str]] = field(default_factory=list)


def _canonical_args(args: tuple[tuple[str, str], ...]) -> str:
    return json.dumps(dict(args), sort_keys=True, ensure_ascii=False)


def _tool_evidence_id(tool_name: str, args_json: str, raw: str) -> str:
    return "tool:" + sha256(f"{tool_name}\0{args_json}\0{raw}".encode()).hexdigest()[:16]


def _insufficient(evidence_id: str, source: str, limitation: str) -> EvidenceFinding:
    return EvidenceFinding(
        evidence_id=evidence_id or "none", source=source, observation="",
        relation="insufficient", strength="contextual", limitation=limitation,
    )


def collect_evidence(
    dossiers: dict[str, CandidateDossier],
    pending_requests: list[EvidenceRequest],
    *,
    tool_client,
    analyst_llm,
    structured_method: str,
) -> EvidenceBatch:
    batch = EvidenceBatch()
    cache: dict[tuple[str, str], tuple[bool, str]] = {}  # (tool,args_json)->(success, raw)

    for req in pending_requests:
        dossier = dossiers.get(req.candidate_id)
        note = EvidenceNote(request_id=req.id, candidate_id=req.candidate_id, findings=[])
        if dossier is None:
            note.findings.append(_insufficient("", "none", "dossier_missing"))
            batch.notes.append(note)
            continue

        strat = STRATEGIES_BY_ID.get(req.strategy_id)
        if strat is None or strat.purpose != req.purpose:
            note.findings.append(_insufficient("", "none", "request_strategy_mismatch"))
            batch.notes.append(note)
            continue

        specs = strat.build_tool_calls(dossier) if tool_client is not None else []
        if not specs:
            note.findings.append(_insufficient("", "none", "no_method_resolved"))
            batch.notes.append(note)
            continue

        for spec in specs:
            if spec.tool_name not in strat.allowed_tools:
                note.findings.append(_insufficient("", f"tool:{spec.tool_name}", "tool_not_allowed"))
                continue
            args_json = _canonical_args(spec.arguments)
            key = (spec.tool_name, args_json)
            if key in cache:
                success, raw = cache[key]
                batch.trace.append(("evidence_tool_reused", f"{req.id} {spec.tool_name}"))
            else:
                success, raw = _invoke_tool(tool_client, spec)
                cache[key] = (success, raw)
            eid = _tool_evidence_id(spec.tool_name, args_json, raw)
            if not success or not raw.strip():
                note.findings.append(_insufficient(eid, f"tool:{spec.tool_name}", "tool_failed_or_empty"))
                continue
            finding = _interpret(req, dossier, spec.tool_name, raw, eid, analyst_llm, structured_method)
            note.findings.append(finding)

        batch.notes.append(note)
        batch.trace.append(("evidence_finding_recorded", f"{req.id} n={len(note.findings)}"))

    return batch


def _invoke_tool(tool_client, spec) -> tuple[bool, str]:
    args = dict(spec.arguments)
    try:
        if spec.tool_name == "get_file_content":
            resp = tool_client.get_file_content(args.get("target"))
        elif spec.tool_name == "find_sensitive_apis":
            resp = tool_client.find_sensitive_apis()
        elif spec.tool_name == "find_callers":
            resp = tool_client.find_callers(args.get("query"))
        elif spec.tool_name == "get_code_metrics":
            resp = tool_client.get_code_metrics(args.get("file_path"))
        else:
            return False, ""
    except Exception:
        return False, ""
    raw = resp.as_tool_output() if hasattr(resp, "as_tool_output") else str(resp)
    return bool(getattr(resp, "success", True)), raw


_DIRECT_AUTH = ("@preauthorize", "@postauthorize", "@secured", "@rolesallowed")
_DIRECT_TX = ("@transactional",)


def _deterministic(req, raw: str) -> tuple[str, str] | None:
    """返回 (relation, note) 若命中强模式；否则 None 交给 LLM。仅 counter 用。"""
    low = raw.lower()
    if req.strategy_id.startswith("authorization.") and req.purpose == "counter":
        if any(a in low for a in _DIRECT_AUTH):
            return "contradicts", "命中方法级鉴权注解"
    if req.strategy_id.startswith("transaction") and req.purpose == "counter":
        if any(a in low for a in _DIRECT_TX):
            return "contradicts", "命中 @Transactional"
    return None


def _interpret(req, dossier, tool_name, raw, eid, analyst_llm, structured_method) -> EvidenceFinding:
    hit = _deterministic(req, raw)
    if hit is not None:
        return EvidenceFinding(
            evidence_id=eid, source=f"tool:{tool_name}", observation=hit[1],
            relation=hit[0], strength="direct",
        )
    if analyst_llm is None:
        return _insufficient(eid, f"tool:{tool_name}", "no_analyst_llm")
    judged = _analyse(req, dossier, tool_name, raw, analyst_llm, structured_method)
    if judged is None:
        return _insufficient(eid, f"tool:{tool_name}", "llm_none")
    return EvidenceFinding(
        evidence_id=eid, source=f"tool:{tool_name}", observation=judged.observation,
        relation=judged.relation, strength=judged.strength, limitation=judged.limitation,
    )


def _analyse(req, dossier, tool_name, raw, analyst_llm, structured_method):
    from codeguard_agent.llm.client import invoke_with_retry

    c = dossier.candidate
    system = (
        "你判断给定工具事实与候选主张的关系，只输出 supports/contradicts/insufficient。"
        "‘未找到保护’永远是 insufficient，不能反向支持漏洞。"
        "只有当前路径可直接定位的保护事实才是 direct，模糊/全局/命名猜测最多 contextual。"
        "不得新增候选中不存在的漏洞主张。"
    )
    human = (
        f"目的: {req.purpose}\n策略问题: {req.question}\n"
        f"候选主张: {c.claim}\n候选类型: {c.type}\n"
        f"任务补丁:\n{dossier.task.patch[:2000]}\n"
        f"工具 {tool_name} 返回:\n{raw[:3000]}"
    )
    try:
        structured = analyst_llm.with_structured_output(_RelationJudgment, method=structured_method)
        result = invoke_with_retry(structured, [("system", system), ("human", human)], max_retries=1)
    except Exception:
        return None
    return result if isinstance(result, _RelationJudgment) else None
```

- [x] **Step 3: graph.py `_evidence_agent_node` 改薄 adapter**

把 905-1090 行的整个 `_evidence_agent_node` 内联实现替换为：组装 dossier（复用 5A 的 `CandidateDossier`）→ 调 `collect_evidence` → 写回 `evidence_notes` / `evidence_round` / trace。dossier 组装需要 `review_tasks` / `risk_profiles` / `task_context_bundles` / `evidence_requests` / `evidence_notes` / `council_verdicts`，全从 state 取。示意：

```python
def _evidence_agent_node(tool_client=None, judge_llm=None):
    def _node(state: ReviewState) -> dict:
        processed = {n.request_id for n in state.get("evidence_notes") or []}
        pending = [r for r in state.get("evidence_requests") or [] if r.id not in processed]
        dossiers = _assemble_dossiers(state)
        batch = collect_evidence(
            dossiers, pending, tool_client=tool_client, analyst_llm=judge_llm,
            structured_method=state.get("structured_method", "function_calling"),
        )
        return {
            "evidence_notes": batch.notes,
            "evidence_round": state.get("evidence_round", 0) + 1,
            "council_trace": [CouncilTrace(node="evidence_agent", event=ev, detail=d)
                              for ev, d in batch.trace] or
                             [CouncilTrace(node="evidence_agent", event="noop", detail="no pending")],
        }
    return _node
```

`_assemble_dossiers(state)` 新增为 graph.py 模块级 helper，产出 `dict[candidate_id, CandidateDossier]`：按 `candidate.task_id` 关联 task/profile/bundle，按 candidate_id 归组 requests/notes，`latest_verdict` 取 `council_verdicts` 中同 candidate 最后一条。task 找不到时跳过该候选并记 `evidence_dossier_missing_task` trace（Judge 端按无效处理）。

- [x] **Step 4: 写 Agent 测试 + 改写旧测试**

`tests/test_evidence_agent.py`：

```python
from types import SimpleNamespace

from codeguard_agent.models.council import CandidateIssue, EvidenceRequest
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import ReviewTask
from codeguard_agent.pipeline.evidence_agent import collect_evidence
from codeguard_agent.pipeline.evidence_planner import CandidateDossier


class _Resp:
    def __init__(self, success, out): self.success, self._out = success, out
    def as_tool_output(self): return self._out


class _Client:
    def __init__(self, out="", success=True): self._out, self._success = out, success
        # 记录调用次数验证缓存
    def get_file_content(self, target): self.calls = getattr(self, "calls", 0) + 1; return _Resp(self._success, self._out)
    def find_sensitive_apis(self): return _Resp(self._success, self._out)
    def find_callers(self, q): return _Resp(self._success, self._out)
    def get_code_metrics(self, f): return _Resp(self._success, self._out)


def _dossier(cid="c1"):
    cand = CandidateIssue(id=cid, task_id="t1", source_agent="threat_model", file="A.java",
                          type="越权", severity_proposal=Severity.CRITICAL, claim="缺少鉴权")
    task = ReviewTask(id="t1", file="A.java", patch="void f(){}", changed_lines=[1])
    return CandidateDossier(cand, task, None, None, (), (), None)


def _req(strategy_id="authorization.counter", purpose="counter", cid="c1"):
    return EvidenceRequest(candidate_id=cid, strategy_id=strategy_id, purpose=purpose,
                           target="A.java", preferred_tools=["get_file_content", "find_sensitive_apis"])


def test_empty_result_is_insufficient():
    d = {"c1": _dossier()}
    batch = collect_evidence(d, [_req()], tool_client=_Client(out=""), analyst_llm=None,
                             structured_method="function_calling")
    assert all(f.relation == "insufficient" for n in batch.notes for f in n.findings)


def test_no_llm_non_direct_is_insufficient():
    d = {"c1": _dossier()}
    batch = collect_evidence(d, [_req()], tool_client=_Client(out="some code no annotation"),
                             analyst_llm=None, structured_method="function_calling")
    assert all(f.relation == "insufficient" for n in batch.notes for f in n.findings)


def test_direct_auth_annotation_is_direct_contradiction():
    d = {"c1": _dossier()}
    batch = collect_evidence(d, [_req()], tool_client=_Client(out="@PreAuthorize(\"x\") void f(){}"),
                             analyst_llm=None, structured_method="function_calling")
    findings = [f for n in batch.notes for f in n.findings]
    assert any(f.relation == "contradicts" and f.strength == "direct" for f in findings)


def test_strategy_mismatch_insufficient():
    d = {"c1": _dossier()}
    bad = EvidenceRequest(candidate_id="c1", strategy_id="nonexistent.strategy", purpose="counter")
    batch = collect_evidence(d, [bad], tool_client=_Client(out="x"), analyst_llm=None,
                             structured_method="function_calling")
    assert batch.notes[0].findings[0].limitation == "request_strategy_mismatch"


def test_each_request_gets_its_own_note():
    d = {"c1": _dossier()}
    reqs = [_req(strategy_id="authorization.counter", purpose="counter"),
            _req(strategy_id="authorization.support", purpose="support")]
    batch = collect_evidence(d, reqs, tool_client=_Client(out="code"), analyst_llm=None,
                             structured_method="function_calling")
    assert len(batch.notes) == 2
    assert {n.request_id for n in batch.notes} == {r.id for r in reqs}
```

删除/改写旧证据测试：搜索 `grep -rln "build_evidence_requests\|EvidenceJudgment\|\.supports\b\|status=\"supported\"" services/agent/tests/`，把断言"有 raw output 即 supported""ContextBundle 含文件名即 supported"的用例改为断言 `insufficient`；删除测 `build_evidence_requests` 按 source_agent 选工具、高置信跳过证据的用例。

- [x] **Step 5: 跑测试**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/test_evidence_agent.py tests/ -q`
Expected: PASS（若 graph judge 仍读旧字段导致红灯，说明 Task 7 必须与本任务同 commit——见 5B 顶部说明，合并提交）。

- [x] **Step 6: 提交**

```bash
git add -A
git commit -m "feat(evidence): EvidenceNote 收敛为结构化 finding 并重写 EvidenceAgent"
```

---

## Task 7: CouncilJudge 裁决矩阵重写 + 删旧规则 + 修 c_file bug

**Files:**
- Create: `services/agent/src/codeguard_agent/pipeline/council_judge.py`（`judge_candidates` 候选级裁决纯函数）
- Modify: `graph.py`（`_council_judge_node` 改薄 adapter；删 `_rule_strong_support`；`_rule_contradicted` 改读 finding strength；修 1312 附近 `c_file` 回映射 bug）
- Test: `services/agent/tests/test_council_judge.py`（新建）；改写旧 judge 测试

- [x] **Step 1: 候选级裁决纯函数（council_judge.py）**

按设计稿 §9 裁决矩阵实现 `judge_candidate(dossier, findings_by_purpose) -> Verdict`：direct+contradicts(counter)→drop；severity direct contradicts→交 LLM 只允许 downgrade/keep；support direct 但 counter insufficient→不 fast keep；全 insufficient+CRITICAL→无 LLM downgrade WARNING；全 insufficient 非 CRITICAL→无 LLM conservative keep。findings 按 `request_id → request.purpose` 关联。

```python
"""CouncilJudge 候选级确定性裁决。全局去重/合并仍在 graph 节点。"""

from __future__ import annotations

from codeguard_agent.models.council import EvidenceFinding, EvidenceNote, EvidenceRequest, Verdict
from codeguard_agent.models.schemas import Severity


def _purpose_of(req_id: str, requests: list[EvidenceRequest]) -> str:
    for r in requests:
        if r.id == req_id:
            return r.purpose
    return "counter"


def judge_candidate(
    candidate, notes: list[EvidenceNote], requests: list[EvidenceRequest], *, has_llm: bool,
    evidence_round: int, max_rounds: int,
) -> Verdict | None:
    """返回确定性 Verdict；None 表示交给 LLM 终审。"""
    findings: list[tuple[str, EvidenceFinding]] = []  # (purpose, finding)
    for n in notes:
        purpose = _purpose_of(n.request_id, requests)
        for f in n.findings:
            findings.append((purpose, f))

    counter_direct = [f for p, f in findings if p == "counter" and f.relation == "contradicts" and f.strength == "direct"]
    if counter_direct:
        return Verdict(candidate.id, "drop", "direct_counter_evidence", "命中直接反证")

    all_insufficient = bool(findings) and all(f.relation == "insufficient" for _, f in findings)
    if all_insufficient:
        if candidate.severity_proposal == Severity.CRITICAL and not has_llm:
            return Verdict(candidate.id, "downgrade", "critical_insufficient_evidence",
                           "CRITICAL 但证据全不足，降级", severity_override=Severity.WARNING)
        if candidate.severity_proposal != Severity.CRITICAL and not has_llm:
            return Verdict(candidate.id, "keep", "conservative_keep", "证据不足保守保留")
    return None  # 交 LLM
```

- [x] **Step 2: graph.py 改造**

删除 `_rule_strong_support`（1133-1147）及其在 `_COUNCIL_RULES`（1152-1157）的引用。`_rule_contradicted`（1103-1110）改为读 finding：

```python
def _rule_contradicted(candidate, notes, _bundle):
    for n in notes:
        for f in n.findings:
            if f.relation == "contradicts" and f.strength == "direct":
                return Verdict(candidate.id, "drop", "direct_counter_evidence", "直接反证")
    return None
```

`_council_judge_node` 里 `_build_llm_prompt` 的证据渲染（1210-1218 行读 `note.supports/contradicts/unknowns`）改为遍历 `note.findings` 渲染 `relation/strength/observation/limitation`。LLM 终审后设置 `requested_purpose`（当 action=needs_more_evidence）。删除 `Verdict.suggested_tools` 相关渲染与 `decision.suggested_tools` 传递（1426、1463 行）。

**修 c_file bug（1312 附近）：** 全局去重回映射里，把内层循环遗留的 `c_file` 改为使用当前正在处理的 `best.file` 派生的文件名。定位：`grep -n "c_file" graph.py`，确认 1312/1352 处回映射用的是外层 `best`/`dedup_issue` 的 file，而非上一轮内层循环残留值。

- [x] **Step 3: 写测试**

`tests/test_council_judge.py`：

```python
from codeguard_agent.models.council import CandidateIssue, EvidenceFinding, EvidenceNote, EvidenceRequest
from codeguard_agent.models.schemas import Severity
from codeguard_agent.pipeline.council_judge import judge_candidate


def _cand(sev=Severity.WARNING, cid="c1"):
    return CandidateIssue(id=cid, task_id="t1", source_agent="threat_model", file="A.java",
                          type="越权", severity_proposal=sev, claim="缺鉴权")


def _note(req_id, relation, strength):
    return EvidenceNote(request_id=req_id, candidate_id="c1",
                        findings=[EvidenceFinding(evidence_id="e", source="tool:x",
                                                  observation="", relation=relation, strength=strength)])


def test_direct_counter_drops():
    req = EvidenceRequest(candidate_id="c1", strategy_id="authorization.counter", purpose="counter", target="A.java")
    v = judge_candidate(_cand(), [_note(req.id, "contradicts", "direct")], [req],
                        has_llm=False, evidence_round=1, max_rounds=2)
    assert v.action == "drop" and v.reason_code == "direct_counter_evidence"


def test_support_direct_does_not_fast_keep():
    req = EvidenceRequest(candidate_id="c1", strategy_id="authorization.support", purpose="support", target="A.java")
    v = judge_candidate(_cand(), [_note(req.id, "supports", "direct")], [req],
                        has_llm=True, evidence_round=1, max_rounds=2)
    assert v is None  # 交 LLM，不直接 keep


def test_all_insufficient_critical_downgrades_without_llm():
    req = EvidenceRequest(candidate_id="c1", strategy_id="authorization.counter", purpose="counter", target="A.java")
    v = judge_candidate(_cand(Severity.CRITICAL), [_note(req.id, "insufficient", "contextual")], [req],
                        has_llm=False, evidence_round=1, max_rounds=2)
    assert v.action == "downgrade" and v.severity_override == Severity.WARNING
```

改写旧 judge 测试：删 `strong_support_fast_track` 用例；把 Judge 直接生成 EvidenceRequest 的用例改为断言 Judge 只设 `requested_purpose`。

- [x] **Step 4: 跑全套**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/ -q && conda run -n codeguard ruff check src/ && conda run -n codeguard mypy src/`
Expected: 全绿。

- [x] **Step 5: 提交**

```bash
git add -A
git commit -m "feat(council): Judge 按 finding 强度裁决并修复去重回映射"
```

---

## Task 8: 图接线 EvidencePlanner + 删净旧证据请求路径

**Files:**
- Modify: `graph.py`（新增 `_evidence_planner_node`；改边；删 reviewer collect 里两处 `build_evidence_requests` 调用及 `MAX_TOTAL` 截断；`capped_evidence_request_reducer` 去掉 `[:20]`）
- Modify: `models/council.py`（删 `MAX_TOTAL_EVIDENCE_REQUESTS`；删 `Verdict.suggested_tools` / `JudgeDecision.suggested_tools`）
- Test: `services/agent/tests/test_graph_orchestration.py`（增补 planner 拓扑用例）

- [x] **Step 1: 新增 `_evidence_planner_node`**

```python
def _evidence_planner_node(judge_llm=None):
    def _node(state: ReviewState) -> dict:
        dossiers = list(_assemble_dossiers(state).values())
        plan = plan_evidence(
            dossiers, evidence_round=state.get("evidence_round", 0),
            classifier_llm=judge_llm,
            structured_method=state.get("structured_method", "function_calling"),
        )
        return {
            "evidence_requests": plan.requests,
            "council_trace": [CouncilTrace(node="evidence_planner", event=ev, detail=d)
                              for ev, d in plan.trace] or
                             [CouncilTrace(node="evidence_planner", event="noop", detail="no plan")],
        }
    return _node
```

- [x] **Step 2: 改图接线（1540-1570 区）**

```python
    g.add_node("evidence_planner", _evidence_planner_node(judge_llm=fp_verify_llm))
    # … 现有 add_node("evidence_agent"...) / ("council_judge"...) 保留
    g.add_edge("council_coordinator", "evidence_planner")   # 新：coordinator → planner
    g.add_edge("evidence_planner", "evidence_agent")        # planner → agent
    g.add_edge("evidence_agent", "council_judge")
    g.add_conditional_edges(
        "council_judge", _route_after_council_judge,
        {"evidence_planner": "evidence_planner", "END": END},   # 回环改回 planner
    )
```

`_route_after_council_judge` 的返回值 `"evidence_agent"` 改为 `"evidence_planner"`。

- [x] **Step 3: 删 reviewer collect 里的 build_evidence_requests**

搜索 `grep -n "build_evidence_requests" graph.py`（699、827 行两处）。删除这两段"逐候选生成 evidence_requests"的代码块及 `"evidence_requests": requests[:MAX_TOTAL_EVIDENCE_REQUESTS]`（725、854 行）——collect 节点不再产出证据请求，改由 planner 统一产出。collect 返回值移除 `evidence_requests` 键。

- [x] **Step 4: 删 cap 与 suggested_tools**

- `capped_evidence_request_reducer`（106-116）：把 `return unique[:MAX_TOTAL_EVIDENCE_REQUESTS]` 改为 `return unique`（保留去重，去掉截断）。函数可改名 `dedup_evidence_request_reducer`（同步改 218 行 `Annotated`）。
- `models/council.py`：删 `MAX_TOTAL_EVIDENCE_REQUESTS`（22 行）、`Verdict.suggested_tools`（39 行）、`JudgeDecision.suggested_tools`（50 行）。
- `graph.py` import 区删 `MAX_TOTAL_EVIDENCE_REQUESTS`（40 行）、`build_evidence_requests`（42 行）。

- [x] **Step 5: 增补拓扑测试**

在 `tests/test_graph_orchestration.py` 增：

```python
def test_planner_between_coordinator_and_agent():
    """构图后：coordinator → evidence_planner → evidence_agent → council_judge。"""
    # 复用该文件现有 build_review_graph fixture；断言节点存在且边正确。
    # 若现有测试用编译后 graph 的 get_graph().edges 检查，照同一方式断言：
    #   ("council_coordinator","evidence_planner"),("evidence_planner","evidence_agent") in edges
```

> 执行者：参照 `test_graph_orchestration.py` 现有断边风格补断言，不要新造检查手法。

- [x] **Step 6: 跑全套 + mock 冒烟**

Run:
```
conda run -n codeguard --no-capture-output python -m pytest tests/ -q
conda run -n codeguard ruff check src/ && conda run -n codeguard mypy src/
$env:CODEGUARD_PROVIDER="mock"; conda run -n codeguard python -m codeguard_agent review --repo . --base HEAD
```
Expected: 全套绿；mock 审查 EXIT=0 且能打印 ReviewResult。

- [x] **Step 7: 提交**

```bash
git add -A
git commit -m "feat(graph): 接入 EvidencePlanner 并删除旧证据请求路径"
```

---

## Task 9: trace 事件、mock 安全回退、eval 与台账收尾

**Files:**
- Modify: `graph.py`（确保 trace 事件名齐全）
- Modify: `docs/superpowers/specs/2026-07-10-risk-routed-review-orchestration-design.md`（§2 拓扑、§3.3 State 表、§4.8、§5 台账——设计稿 §11 清单）
- Modify: `docs/superpowers/specs/2026-07-12-risk-routed-evidence-planning-phase5-design.md`（台账 planned→done）
- Test: eval 用例（`services/agent/evals/dataset/`）

- [x] **Step 1: 核对 trace 事件齐全**

确认这些事件在 planner/agent 节点产出：`candidate_evidence_tag_resolved`、`evidence_planned`、`evidence_plan_skipped`、`evidence_plan_exhausted`、`evidence_plan_invalid_verdict`、`evidence_finding_recorded`、`evidence_tool_reused`。`judge_requested_more_evidence` 在 judge 节点 LLM 返回 needs_more 时补一条 `CouncilTrace`。

- [x] **Step 2: mock / None 全链路冒烟**

Run: `$env:CODEGUARD_PROVIDER="mock"; conda run -n codeguard python -m codeguard_agent review --repo . --base HEAD`
Expected: EXIT=0。确认 planner/agent 在无 tool_client、无 llm 时全部走 no-op / insufficient，不抛异常。

- [x] **Step 3: 加 eval 用例**

往 `services/agent/evals/dataset/vuln/` 加 2 个 YAML：一个"敏感操作 + 入口有 @PreAuthorize"（期望 direct counter → drop/downgrade），一个"多写操作无事务边界"（期望 insufficient 不误判为反证）。往 `evals/dataset/clean/` 加 1 个"有完整保护的诱饵"（期望不报）。

Run: `conda run -n codeguard python -m evals.runner --profile pipeline-notools --runs 1`
Expected: 出报告 `evals/reports/pipeline.md`，无崩溃。

- [x] **Step 4: 更新主设计文档（§11 清单四处）**

按 Phase 5 设计稿 §11：
1. 主设计 §2 拓扑图插入 `EvidencePlanner`，回环改 `Judge→EvidencePlanner`，补一句受控修订说明。
2. §3.3 State 写入表 `evidence_requests` 主写节点改为 `EvidencePlanner`。
3. §4.8 EvidenceAgent「按 RiskTag 分流」改为「按候选证据主题分流，task RiskTag 仅先验」。
4. §5 台账：补记 Phase 4 实际已落地；Phase 5 行 `planned→done`，写入落地内容/State 变更（"无新增顶层字段；EvidenceNote 载荷重写；删 build_evidence_requests/MAX_TOTAL/旧规则"）/验证证据（测试文件 + commit）/刻意未做。

- [x] **Step 5: 更新 Phase 5 设计稿状态**

设计稿头部 `状态` 改为「已实施」，并在文末追加一段落地复盘（实际测试数、eval 观测到的工具调用数）。

- [x] **Step 6: 全量回归 + 提交**

Run: `conda run -n codeguard --no-capture-output python -m pytest tests/ -q && conda run -n codeguard ruff check src/ && conda run -n codeguard mypy src/`
Expected: 全绿。

```bash
git add -A
git commit -m "docs(orchestration): 记录 Phase 5 证据规划链落地"
```

**5B 验收：** 全套单测绿、mock 冒烟 EXIT=0、eval 报告更新、主设计四处修订落地、两份设计稿状态更新、旧代码（build_evidence_requests / MAX_TOTAL / _rule_strong_support / EvidenceNoteStatus / EvidenceJudgment / suggested_tools）已全删。

---

## 全局删除清单（5B 结束时 grep 应零命中）

执行完 5B 后运行以下命令，任一有命中即说明有死代码残留：

```bash
cd services/agent
grep -rn "build_evidence_requests\|MAX_TOTAL_EVIDENCE_REQUESTS\|EvidenceNoteStatus\|EvidenceJudgment\|_rule_strong_support\|suggested_tools" src/
grep -rn "\.supports\b\|\.contradicts\b\|\.unknowns\b\|note.status\|\.evidence_ids" src/
```
Expected: 无命中（`evidence-analysis.txt` prompt 若不再被引用也一并删除）。

---

## 自审记录（spec 覆盖核对）

- 设计稿 §2 六条决策 → Task 1(strategy_id/purpose)、Task 6(finding 切净)、Task 8(删 cap/suggested_tools)、classifier 复用 judge_llm(Task 3/5/8 传 `fp_verify_llm`)。✅
- §3 拓扑修订 → Task 8 接线 + Task 9 主文档同步。✅
- §5.1 字段硬约束 → 每字段在对应 Task 有写入者+消费者+测试（strategy_id/purpose→Task1/5/6；finding 各字段→Task6/7；requested_purpose→Task5/7/8）。✅
- §6 策略注册表 + 候选分类 → Task 2/3/4。✅
- §7 Planner 两遍+回环 → Task 5。✅
- §8 Agent 规则取事实+安全回退 → Task 6。✅
- §9 Judge 裁决矩阵+删旧规则+c_file 修复 → Task 7。✅
- §10 trace/eval → Task 9。✅
- §11 主文档四处修订 → Task 9 Step 4。✅
