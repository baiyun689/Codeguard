# 知识图谱按 RiskTag 拆分注入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **本计划暂不执行**——用户已明确这是紧接 Phase4(task 级并发 + 引擎分层)之后的
> 下一个子阶段,本次只出计划,不动手。执行前需先确认 Phase4 的计划
> (`docs/superpowers/plans/2026-07-11-risk-routed-discovery-phase4.md`)已完成并
> 全量测试通过,因为本计划的 Task 4(组装进 `_prepare`)依赖 Phase4 已经改造好的
> 单 task `_prepare` 逻辑。

**Goal:** 把三个发现者 prompt(`threat-model.txt`/`behavior.txt`/`maintainability.txt`)里内嵌的知识图谱,从"每次调用全量携带"改成"按该 task 命中的 RiskTag 只拼接相关片段",同时借这次机会重写/扩写现有知识内容中和 23 个 RiskTag 对不上的部分(如 behavior 域缺失的事务/幂等/缓存/消息队列知识,maintainability 域缺失的性能/资源生命周期知识)。

**Architecture:** 每个 domain prompt 拆成 `<domain>-base.txt`(角色/方法论/严重级别/置信度/误报判例/排除项/输出规范,每次必带)+ `prompts/knowledge/<domain>/<TAG>.txt`(按 RiskTag 枚举值命名的知识片段,只在该 task 命中对应标签时才拼接)。新增 `pipeline/knowledge_rules.py` 提供 `load_knowledge(domain, tags) -> str` 纯查表函数,在 Phase4 已经改造好的 `_prepare` 里,除了 task 的 risk 信息和 context bundle,额外拼接该 task 命中标签对应的知识文本。

**Tech Stack:** Python 3 / pytest / 纯文本 prompt 文件(不依赖新库)。

---

## 前置说明

- 本计划实现的是 `docs/superpowers/specs/2026-07-11-tag-scoped-knowledge-injection-design.md`。
- 依赖 Phase4 计划已落地的 `_prepare`(单 task 调用,消费 `task_risk_context`)。
- 内容编写(33 个知识片段文件的具体文字)是本计划里工作量最大的部分,且**不是可
  预先在计划文档里逐字写死的代码**——它是领域知识的组织和扩写,产出质量只能靠
  实施时人工通读 + 后续 eval 观察 recall 变化判断,和项目一贯的"tests 测工程正确性、
  evals 测审查质量"原则一致(design 文档 §6 已如实说明)。计划里给出每个片段
  **必须覆盖的模板结构**(design 文档 §4)和**每个 domain 需要覆盖的标签清单**
  (design 文档 §2),但具体措辞由实施者(执行本计划的 agent)在写入时创作。

---

## Task 1: 拆出 `<domain>-base.txt`(去掉知识图谱小节)

**Files:**
- Create: `src/codeguard_agent/prompts/threat-model-base.txt`
- Create: `src/codeguard_agent/prompts/behavior-base.txt`
- Create: `src/codeguard_agent/prompts/maintainability-base.txt`
- Modify: `src/codeguard_agent/pipeline/stages/reviewer_stage.py`(`DEFAULT_REVIEWERS` 的 `prompt_file`)
- Test: `tests/test_reviewer_stage.py`(如无则新建)

- [ ] **Step 1: 写失败的测试**

```python
def test_default_reviewers_point_to_base_prompt_files():
    names = {r.name: r.prompt_file for r in DEFAULT_REVIEWERS}
    assert names["ThreatModelAgent"] == "threat-model-base.txt"
    assert names["BehaviorAgent"] == "behavior-base.txt"
    assert names["MaintainabilityAgent"] == "maintainability-base.txt"


def test_base_prompts_do_not_contain_knowledge_graph_heading():
    for filename in (
        "threat-model-base.txt", "behavior-base.txt", "maintainability-base.txt",
    ):
        text = _load_prompt(filename)
        assert "知识图谱" not in text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_reviewer_stage.py -v`
Expected: FAIL(文件不存在 / `prompt_file` 仍指向旧文件名)

- [ ] **Step 3: 创建三个 base 文件**

把现有 `threat-model.txt`(222 行)去掉第 13-142 行(`## 安全漏洞知识图谱`整节,
含其下 8 个 `###` 子节),保留第 1-12 行 + 第 143-222 行,另存为
`threat-model-base.txt`。同样处理:

- `behavior.txt` 去掉第 13-168 行(`## 运行时缺陷知识图谱`整节,含其下 8 个子节),
  保留第 1-12 行 + 第 169-242 行,存为 `behavior-base.txt`。
- `maintainability.txt` 去掉第 13-155 行(`## 可维护性知识图谱`整节,含其下 8 个
  子节),保留第 1-12 行 + 第 156-229 行,存为 `maintainability-base.txt`。

三个文件里紧跟 `## 你的赛道` 之后原本引用"下面的知识图谱"之类的衔接句,如果有,
改写成引用"你会在 user prompt 里收到该 task 命中的风险标签对应知识"(具体措辞
实施时定,不强制文字)。

- [ ] **Step 4: 把 `DEFAULT_REVIEWERS` 的 `prompt_file` 改指向 base 文件**

修改 `src/codeguard_agent/pipeline/stages/reviewer_stage.py`:

```python
DEFAULT_REVIEWERS: tuple[Reviewer, ...] = (
    Reviewer(
        "ThreatModelAgent",
        "threat-model-base.txt",
        source_agent="threat_model",
        tool_allowlist=["get_file_content", "find_sensitive_apis"],
    ),
    Reviewer(
        "BehaviorAgent",
        "behavior-base.txt",
        source_agent="behavior",
        tool_allowlist=["get_file_content", "find_callers"],
    ),
    Reviewer(
        "MaintainabilityAgent",
        "maintainability-base.txt",
        source_agent="maintainability",
        tool_allowlist=["get_file_content", "get_code_metrics"],
    ),
)
```

- [ ] **Step 5: 删除旧的三个全量 prompt 文件**

```bash
git rm src/codeguard_agent/prompts/threat-model.txt
git rm src/codeguard_agent/prompts/behavior.txt
git rm src/codeguard_agent/prompts/maintainability.txt
```

(内容已迁移到 base 文件 + 后续 Task 里的知识片段文件,不是死代码防御性保留——
按 CLAUDE.md"不留死代码"的项目惯例直接删。)

- [ ] **Step 6: 运行测试确认通过 + 全量回归**

Run:
```bash
python -m pytest tests/ -v
```
Expected: 全部通过。特别检查:任何硬编码引用旧文件名 `threat-model.txt` /
`behavior.txt` / `maintainability.txt` 的测试或代码(`grep -rn "threat-model.txt\|behavior.txt\|maintainability.txt" src/ tests/`)都已同步改成 `*-base.txt`。

- [ ] **Step 7: 提交**

```bash
git add src/codeguard_agent/prompts/*.txt src/codeguard_agent/pipeline/stages/reviewer_stage.py tests/test_reviewer_stage.py
git commit -m "refactor(prompts): 拆出 domain base prompt,知识图谱移出主 prompt"
```

---

## Task 2: `pipeline/knowledge_rules.py`(查表 + 组装)

**Files:**
- Create: `src/codeguard_agent/pipeline/knowledge_rules.py`
- Test: `tests/test_knowledge_rules.py`

- [ ] **Step 1: 写失败的测试**

```python
import pytest

from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.knowledge_rules import load_knowledge


def test_load_knowledge_empty_tags_returns_empty_string():
    assert load_knowledge("threat_model", []) == ""


def test_load_knowledge_concatenates_matched_tags(tmp_path, monkeypatch):
    domain_dir = tmp_path / "threat_model"
    domain_dir.mkdir(parents=True)
    (domain_dir / "AUTHORIZATION.txt").write_text("AUTH_CONTENT", encoding="utf-8")
    (domain_dir / "INJECTION.txt").write_text("INJECTION_CONTENT", encoding="utf-8")
    import codeguard_agent.pipeline.knowledge_rules as kr

    monkeypatch.setattr(kr, "_KNOWLEDGE_DIR", tmp_path)
    result = kr.load_knowledge("threat_model", [RiskTag.AUTHORIZATION, RiskTag.INJECTION])
    assert "AUTH_CONTENT" in result
    assert "INJECTION_CONTENT" in result


def test_load_knowledge_skips_missing_files_silently(tmp_path, monkeypatch):
    domain_dir = tmp_path / "threat_model"
    domain_dir.mkdir(parents=True)
    import codeguard_agent.pipeline.knowledge_rules as kr

    monkeypatch.setattr(kr, "_KNOWLEDGE_DIR", tmp_path)
    result = kr.load_knowledge("threat_model", [RiskTag.AUTHORIZATION])
    assert result == ""


def test_load_knowledge_covers_every_phase2_routed_tag_for_each_domain():
    """完整性校验：Phase2 路由表覆盖到的每个 (domain, tag) 组合都必须有对应文件。

    这条测试在 Task 3 的知识文件全部写完之前会失败——它就是本子阶段"内容写完了
    没有"的验收标准，留到 Task 3 结束时才应转绿。
    """
    from codeguard_agent.pipeline.risk_routing import _REVIEWER_NAMES
    from codeguard_agent.pipeline.risk_rules.catalog import reviewers_for_tag
    from codeguard_agent.pipeline.knowledge_rules import _KNOWLEDGE_DIR

    domain_by_reviewer = {v: k for k, v in _REVIEWER_NAMES.items()}
    missing = []
    for tag in RiskTag:
        if tag is RiskTag.GENERAL_REVIEW:
            continue
        for reviewer_name in reviewers_for_tag(tag):
            domain = domain_by_reviewer.get(reviewer_name, reviewer_name)
            path = _KNOWLEDGE_DIR / domain / f"{tag.value}.txt"
            if not path.exists():
                missing.append(str(path))
    assert not missing, f"缺失知识文件: {missing}"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_knowledge_rules.py -v`
Expected: FAIL(`knowledge_rules` 模块不存在;最后一条完整性测试预期本阶段结束前
持续失败,是正常状态)

- [ ] **Step 3: 实现 `knowledge_rules.py`**

```python
"""按 RiskTag 查表组装领域知识片段（知识图谱按标签拆分注入子阶段）。

固定注入而非工具：Direct 档（Phase4 低危 task）没有工具循环，做成工具会让它
彻底拿不到知识；且"该查哪个标签"已经是 RiskProfile 里的确定性信息，不需要模型
自己决策要不要查。见 design 文档 §5。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from codeguard_agent.models.tasks import RiskTag

_KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "prompts" / "knowledge"


def load_knowledge(domain: str, tags: Iterable[RiskTag]) -> str:
    """按 domain + 命中的 tag 集合，拼接对应知识片段。

    找不到文件的 tag 静默跳过（理论上不应发生，由
    test_load_knowledge_covers_every_phase2_routed_tag_for_each_domain 的完整性
    测试兜底覆盖率，而不是在运行时抛异常影响审查主链路）。
    """
    parts: list[str] = []
    for tag in tags:
        path = _KNOWLEDGE_DIR / domain / f"{tag.value}.txt"
        if path.exists():
            parts.append(path.read_text(encoding="utf-8").strip())
    return "\n\n".join(parts)
```

- [ ] **Step 4: 运行测试确认通过(除完整性测试)**

Run: `python -m pytest tests/test_knowledge_rules.py -v -k "not covers_every_phase2"`
Expected: PASS(前 3 条);完整性测试仍红,留到 Task 3 结束时转绿。

- [ ] **Step 5: 提交**

```bash
git add src/codeguard_agent/pipeline/knowledge_rules.py tests/test_knowledge_rules.py
git commit -m "feat(pipeline): 新增 knowledge_rules 按标签查表组装领域知识"
```

---

## Task 3: 编写 33 个知识片段文件(内容创作)

**Files:**
- Create: `src/codeguard_agent/prompts/knowledge/threat_model/{9 个文件}.txt`
- Create: `src/codeguard_agent/prompts/knowledge/behavior/{18 个文件}.txt`
- Create: `src/codeguard_agent/prompts/knowledge/maintainability/{6 个文件}.txt`

每个文件必须覆盖以下模板结构(design 文档 §4,原文摘录):

```
## <RiskTag 中文名>(<该标签在 Phase2 词典里定义的"唯一审查问题">)

### 典型模式
该标签在 Java/Spring 代码里通常长什么样(API、注解、代码结构),用具体代码片段
或伪代码示例说明,不用抽象描述。

### 判定要点
命中这个模式后,还需要确认哪些条件才真正构成问题(不是"看到关键字就报")。

### 严重级别参考
这个标签下,什么情况打 CRITICAL、什么情况打 WARNING/INFO——给该标签专属的校准
参考,而不是套用一份全局笼统的严重级别表。

### 已知误报判例
命中该标签特征、但实际不构成问题的具体场景。

### 排除项
明确不属于这个标签、容易被误当成这个标签的相邻情况。
```

各 domain 需要覆盖的标签清单(design 文档 §2,直接复用 Phase2 路由表,不重新定义):

- [ ] **Step 1: threat_model(9 个)** —
  `AUTHORIZATION` / `AUTHENTICATION_SESSION` / `WEB_SECURITY_CONFIG` /
  `INPUT_VALIDATION` / `INJECTION` / `FILE_PATH_IO` / `SSRF_OUTBOUND` /
  `CONFIG_SECURITY` / `DATA_EXPOSURE`。
  素材来源:旧 `threat-model.txt` 第 13-142 行(注入类/认证授权/敏感数据/加密/
  跨站点/反序列化/文件操作/配置部署 8 节)按标签重新切分、改写;`WEB_SECURITY_CONFIG`
  对应"跨站点安全"+"配置与部署安全"里 CORS/CSRF/permitAll/Actuator 暴露部分。

- [ ] **Step 2: behavior(18 个)** —
  `AUTHORIZATION` / `AUTHENTICATION_SESSION` / `INPUT_VALIDATION` / `INJECTION` /
  `SQL_DATA_ACCESS` / `FILE_PATH_IO` / `SSRF_OUTBOUND` / `DATA_EXPOSURE` /
  `TRANSACTION_ATOMICITY` / `CONCURRENCY_CONSISTENCY` / `IDEMPOTENCY_RETRY` /
  `CACHE_CONSISTENCY` / `MESSAGE_DELIVERY` / `ERROR_HANDLING` /
  `NULL_STATE_SAFETY` / `RESOURCE_LIFECYCLE` / `API_CONTRACT` / `PERFORMANCE`。
  素材来源:旧 `behavior.txt` 第 13-168 行(空值安全/边界与范围/异常处理/资源管理/
  并发与线程安全/状态与契约/调用链影响分析/常见逻辑错误模式)覆盖了
  `NULL_STATE_SAFETY`/`ERROR_HANDLING`/`RESOURCE_LIFECYCLE`/
  `CONCURRENCY_CONSISTENCY`/`API_CONTRACT` 的基础内容,改写扩充;
  `AUTHORIZATION`/`AUTHENTICATION_SESSION`/`INPUT_VALIDATION`/`INJECTION`/
  `FILE_PATH_IO`/`SSRF_OUTBOUND`/`DATA_EXPOSURE` 需要从 behavior 的"业务逻辑
  是否正确"视角**新写**(不是简单照抄 threat_model 对应文件——那是"能不能被绕过"
  视角);`SQL_DATA_ACCESS`/`TRANSACTION_ATOMICITY`/`IDEMPOTENCY_RETRY`/
  `CACHE_CONSISTENCY`/`MESSAGE_DELIVERY`/`PERFORMANCE` 现有 prompt **完全没有对应
  内容**,需要参照 Phase2 设计文档 §5"Java/Spring 规则范围"里列出的 API/注解
  线索(`@Transactional`/幂等键/`@Cacheable`/`@KafkaListener` 等)**新写**。

- [ ] **Step 3: maintainability(6 个)** —
  `RESOURCE_LIFECYCLE` / `API_CONTRACT` / `PERFORMANCE` /
  `COMPLEXITY_CONTROL_FLOW` / `DUPLICATION_DESIGN` / `OBSERVABILITY_TESTABILITY`。
  素材来源:旧 `maintainability.txt` 第 13-155 行(错误处理质量/硬编码与魔法值/
  复杂度与规模/重复与抽象/耦合与内聚/可测试性/设计退化信号/命名与文档质量)
  里"复杂度与规模"→`COMPLEXITY_CONTROL_FLOW`、"重复与抽象"→`DUPLICATION_DESIGN`、
  "可测试性"→`OBSERVABILITY_TESTABILITY` 有较好现成对应,改写即可;
  `RESOURCE_LIFECYCLE`/`API_CONTRACT`/`PERFORMANCE` 从维护性视角(不是 behavior
  的正确性视角,而是"这样写好不好维护/扩展")**新写**。

- [ ] **Step 4: 运行完整性测试确认全部覆盖**

Run: `python -m pytest tests/test_knowledge_rules.py -v`
Expected: 全部 4 条 PASS,包括 Task 2 里写的
`test_load_knowledge_covers_every_phase2_routed_tag_for_each_domain`。

- [ ] **Step 5: 人工通读校验**

对每个文件做一次通读检查(不是自动化测试,是实施者的人工步骤):
- 是否覆盖了模板的全部 5 个小节。
- 是否给出了具体 Java/Spring 代码片段而非空泛描述。
- 已知误报判例是否和该标签直接相关(不是从别的标签抄一份改改字段名)。

- [ ] **Step 6: 提交(建议按 domain 分 3 次提交,而非一次性大 commit)**

```bash
git add src/codeguard_agent/prompts/knowledge/threat_model/
git commit -m "feat(prompts): 编写 threat_model 域 9 个 RiskTag 知识片段"

git add src/codeguard_agent/prompts/knowledge/behavior/
git commit -m "feat(prompts): 编写 behavior 域 18 个 RiskTag 知识片段"

git add src/codeguard_agent/prompts/knowledge/maintainability/
git commit -m "feat(prompts): 编写 maintainability 域 6 个 RiskTag 知识片段"
```

---

## Task 4: 接入 `_prepare`(组装进单 task prompt)

**Files:**
- Modify: `src/codeguard_agent/pipeline/graph.py`(`make_reviewer_node` 的 `_invoke_one`)
- Test: `tests/test_graph_orchestration.py`

- [ ] **Step 1: 写失败的测试**

```python
def test_invoke_one_task_includes_matched_tag_knowledge(monkeypatch):
    captured = {}

    class _CapturingEngine:
        def review(self, llm, *, system_prompt, user_prompt, reviewer_name,
                   max_retries, structured_method, enable_hitl=False):
            captured["system_prompt"] = system_prompt
            return ReviewOutcome(ReviewResult(summary="s"))

    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _CapturingEngine())
    monkeypatch.setattr(
        G, "load_knowledge", lambda domain, tags: "KNOWLEDGE_MARKER" if tags else ""
    )
    node = G.make_reviewer_node(G.DEFAULT_REVIEWERS[1], llm=_FakeLLM())
    task = G.ReviewTask(id="A.java#h0", file="A.java", patch="+lock", changed_lines=[1])
    node({
        "diff_text": "+lock",
        "review_tasks": [task],
        "risk_profiles": {
            task.id: G.RiskProfile(
                task_id=task.id, tag_scores={RiskTag.CONCURRENCY_CONSISTENCY: 2},
            )
        },
        "task_selection": G.TaskSelection(selected_task_ids=[task.id]),
    })
    assert "KNOWLEDGE_MARKER" in captured["system_prompt"]
```

(这条测试断言知识文本出现在 `system_prompt` 而不是 `user_prompt`——组装位置在
Step 3 里说明。)

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_graph_orchestration.py -k invoke_one_task_includes_matched_tag_knowledge -v`
Expected: FAIL(`load_knowledge` 还没接入)

- [ ] **Step 3: 在 `_invoke_one` 里拼接知识文本**

在 `src/codeguard_agent/pipeline/graph.py` 顶部导入区加入:

```python
from codeguard_agent.pipeline.knowledge_rules import load_knowledge
```

修改 `make_reviewer_node` 内 `_invoke_one` 函数(Phase4 计划 Task 6 已经写好的那个),
在构造 payload 之前算出该 task 命中的标签、查知识,并把知识文本传给子图(新增
`ReviewerState` 字段 `task_knowledge`,在 `build_reviewer_subgraph` 的 `_review`
里拼进 system_prompt——放在 system prompt 而不是 user prompt,因为它是审查员该
具备的领域知识,和 base prompt 的角色定位属于同一层次,不是"这次要看的证据"):

```python
        def _invoke_one(task_id: str) -> dict:
            task = task_by_id[task_id]
            profile = profiles.get(task_id)
            tier = decide_tier(profile)
            risk_text = render_single_task_risk(task, profile) if profile is not None else ""
            bundle = task_context_bundles.get(task_id)
            bundle_text = bundle.render() if bundle is not None else ""
            task_risk_context = "\n\n".join(p for p in (risk_text, bundle_text) if p)
            active_tags = (
                [tag for tag, score in profile.tag_scores.items() if score > 0]
                if profile is not None
                else []
            )
            task_knowledge = load_knowledge(reviewer.source_agent, active_tags)
            return subgraph.invoke(
                {
                    "diff_text": task.patch,
                    "enabled_tools": effective_tools,
                    "max_retries": state.get("max_retries", 3),
                    "structured_method": state.get("structured_method", "function_calling"),
                    "diff_summary": state.get("diff_summary", ""),
                    "react_recursion_limit": state.get("react_recursion_limit", 24),
                    "task_risk_context": task_risk_context,
                    "task_knowledge": task_knowledge,
                    "tier": tier,
                }
            )
```

`ReviewerState` 新增字段:

```python
    task_risk_context: str
    task_knowledge: str
    tier: str
```

`_review` 里构造 system_prompt 的地方(原来直接 `system_prompt=_load_prompt(reviewer.prompt_file)`)
改为:

```python
        base_prompt = _load_prompt(reviewer.prompt_file)
        task_knowledge = state.get("task_knowledge") or ""
        system_prompt = (
            f"{base_prompt}\n\n{task_knowledge}" if task_knowledge else base_prompt
        )
        ...
        outcome = engine.review(
            llm,
            system_prompt=system_prompt,
            user_prompt=state.get("user_prompt", ""),
            ...
        )
```

(`_direct_fallback` 里同样用到 `_load_prompt(reviewer.prompt_file)` 的地方也要
同步改成带 `task_knowledge` 的 `system_prompt`,否则撞递归上限降级时会丢知识。)

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_graph_orchestration.py -v`
Expected: PASS(全部)

- [ ] **Step 5: 全量回归 + lint + mypy**

```bash
python -m pytest tests/ -q
ruff check src/
mypy src/
```
Expected: 全部通过

- [ ] **Step 6: mock CLI 冒烟**

```bash
python -m codeguard_agent review --repo . --base HEAD
```
Expected: 退出码 0

- [ ] **Step 7: 提交**

```bash
git add src/codeguard_agent/pipeline/graph.py tests/test_graph_orchestration.py
git commit -m "feat(graph): _prepare 组装按task命中标签的领域知识进system prompt"
```

---

## Task 5: 更新实施台账

- [ ] **Step 1:** 打开 `docs/superpowers/specs/2026-07-11-tag-scoped-knowledge-injection-design.md`
  第 8 节,把状态改为 `done`,填入实际改动文件、测试通过数字和 commit hash。

- [ ] **Step 2: 提交**

```bash
git add docs/superpowers/specs/2026-07-11-tag-scoped-knowledge-injection-design.md
git commit -m "docs: 记录知识图谱按标签拆分注入落地"
```
