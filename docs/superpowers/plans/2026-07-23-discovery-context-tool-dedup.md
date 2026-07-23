# Discovery Context and Tool Deduplication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让三个 task-scoped 发现者准确消费 ContextProvider 的既有事实，并在单个发现者范围内确定性消除重复工具执行和重复上下文注入。

**Architecture:** Prompt 组合层统一注入一份上下文数据契约，三个领域 prompt 只保留角色特有的事实缺口规则。执行层新增 review-scoped、reviewer-isolated 的同步工具协调器：同一发现者的并发 task 共享 single-flight/cache，每个 ReAct 对话使用独立 wrapper 记录本地已见结果；协调器必须在 reviewer node 每次执行时创建，不能在图编译期创建。

**Tech Stack:** Python 3.11、LangGraph 1.2+、LangChain `create_agent`、Pydantic 2、同步 `httpx` ToolClient、`concurrent.futures.Future`、pytest、ruff、mypy。

## Global Constraints

- 保持 Python 智能层 → Java Gateway 单向边界；本计划不修改 Java。
- 不修改 `Issue` / `ReviewResult` 产品协议。
- 不在 ThreatModel、Behavior、Maintainability 三个发现者之间共享缓存或记忆。
- 缓存仅存在于一次 pipeline review 的单个发现者节点执行期间，不跨 review、仓库、SHA 或 tool session。
- 缓存键固定为 `(tool_name, canonical_arguments)`；路径统一 `/` 和冗余 `.` 段，但不得转小写。
- 只缓存 `success=True` 且 `result` 非空的 `ToolResponse`；失败、异常、空结果不得进入完成缓存。
- 同一 ReAct 对话重复调用相同键时返回短标记；同一发现者的其他 task 首次请求该键时仍须获得完整内容。
- Prompt 必须说明每类上下文的来源、范围、证明力和局限；上下文充分时必须略过工具。
- 每次工具调用前必须明确缺失事实和已有上下文不能回答的原因；“重新确认”“了解完整代码”“继续看看”不是合法理由。
- 实施按 Task 0→5 顺序执行；Tasks 2、3、4 存在接口依赖，不要并行修改这些文件。
- 所有命令在 `services/agent` 下执行，并使用 `conda run -n codeguard --no-capture-output ...`。
- 不触碰工作区已有的未跟踪 `services/agent/src/codeguard_agent/prompts/knowledge/threat_model/DESERIALIZATION.txt` 与 `trace/`。

---

## File Structure

### 新建

- `services/agent/src/codeguard_agent/prompts/discovery-context-contract.txt`：三个发现者共享的上下文数据字典与工具调用硬门槛。
- `services/agent/src/codeguard_agent/pipeline/discovery_tools.py`：发现者专用缓存键规范化、single-flight 协调器和 per-conversation ToolClient wrapper。
- `services/agent/tests/test_discovery_tools.py`：协调器并发、失败、隔离和本地重复测试。
- `services/agent/evals/dataset/repo/discovery_context_complete_patch_001/case.yaml`：完整新增文件的回归用例元数据。
- `services/agent/evals/dataset/repo/discovery_context_complete_patch_001/changes.diff`：完整新增文件 diff。
- `services/agent/evals/dataset/repo/discovery_context_complete_patch_001/repo/src/main/java/com/demo/BatchCounter.java`：变更后的仓库快照。

### 修改

- `services/agent/src/codeguard_agent/pipeline/stages/reviewer_stage.py`：成为发现者 system prompt 组合的唯一入口。
- `services/agent/src/codeguard_agent/pipeline/graph.py`：在每次 reviewer node 执行时创建独立协调器，并向每个 task 注入独立 wrapper。
- `services/agent/src/codeguard_agent/pipeline/engines.py`：使用 task 注入的 ToolClient；只收集真实 Gateway 工具并按工具/参数保留首次结果。
- `services/agent/src/codeguard_agent/prompts/threat-model-base.txt`：安全领域允许补取的事实缺口。
- `services/agent/src/codeguard_agent/prompts/behavior-base.txt`：行为领域允许补取的事实缺口。
- `services/agent/src/codeguard_agent/prompts/maintainability-base.txt`：维护性领域允许补取的事实缺口。
- `services/agent/tests/test_prompt_contracts.py`：验证三个发现者的有效 prompt，而非只验证单个文件。
- `services/agent/tests/test_graph_orchestration.py`：验证协调器生命周期、同 reviewer 共享和跨 reviewer/review 隔离。
- `services/agent/tests/test_engines.py`：验证伪工具过滤和 GatheredContext 去重。

---

### Task 0: 建立基线与保护现场

**Files:**
- Read: `docs/superpowers/specs/2026-07-23-discovery-context-tool-dedup-design.md`
- Read: `services/agent/evals/profiles.yaml`
- Output only: `services/agent/evals/reports/discovery-context-before.md`

**Interfaces:**
- Consumes: 当前 `master`、现有 `pipeline-file` profile、Java Gateway `http://localhost:9090`。
- Produces: 改动前确定性测试结果和工具使用/质量基线；不产生 commit。

- [ ] **Step 1: 确认分支和工作区，只记录不清理**

Run from repository root:

```powershell
git branch --show-current
git status --short
```

Expected: 当前分支可提交；输出中允许保留用户的 `DESERIALIZATION.txt` 和 `trace/`，不得删除、暂存或修改它们。

- [ ] **Step 2: 跑与本改动直接相关的现有测试**

Run:

```powershell
Set-Location services/agent
conda run -n codeguard --no-capture-output python -m pytest tests/test_engines.py tests/test_prompt_contracts.py tests/test_graph_orchestration.py -q
```

Expected: PASS。若基线失败，先记录失败并停止实施，不得把既有失败归因于本改动。

- [ ] **Step 3: 记录工具档质量基线**

先确保 Gateway 已启动且 `.env` 配置真实 LLM，然后运行：

```powershell
$env:CODEGUARD_TOOL_SERVER_URL="http://localhost:9090"
conda run -n codeguard --no-capture-output python -m evals.runner --profile pipeline-file --runs 1 --report evals/reports/discovery-context-before.md
```

Expected: 命令完成并生成 `evals/reports/discovery-context-before.md`。记录 Precision、Recall、F1、关键问题 recall、工具调用数和 `files_read`；该报告用于 Task 5 对照，不提交生成的归档或报告。

---

### Task 1: 建立共享上下文 Prompt 契约

**Files:**
- Create: `services/agent/src/codeguard_agent/prompts/discovery-context-contract.txt`
- Modify: `services/agent/src/codeguard_agent/pipeline/stages/reviewer_stage.py`
- Modify: `services/agent/src/codeguard_agent/pipeline/graph.py:544-555`
- Modify: `services/agent/src/codeguard_agent/prompts/threat-model-base.txt:78-86`
- Modify: `services/agent/src/codeguard_agent/prompts/behavior-base.txt:72-80`
- Modify: `services/agent/src/codeguard_agent/prompts/maintainability-base.txt:72-80`
- Test: `services/agent/tests/test_prompt_contracts.py`
- Test: `services/agent/tests/test_graph_orchestration.py:957-989`

**Interfaces:**
- Consumes: `Reviewer`, `_load_prompt(name: str) -> str`、RiskTag 知识文本。
- Produces: `build_reviewer_system_prompt(reviewer: Reviewer, task_knowledge: str = "") -> str`，供 `graph.build_reviewer_subgraph()` 唯一调用。

- [ ] **Step 1: 先写有效 Prompt 的失败测试**

在 `test_prompt_contracts.py` 从 `reviewer_stage` 导入 `build_reviewer_system_prompt`，新增：

```python
def test_effective_reviewer_prompts_explain_prefetched_context_and_hard_tool_gate() -> None:
    required = (
        "task patch 是当前 hunk",
        "不保证包含整个文件",
        "AST structure",
        "类、方法、方法行范围、控制流节点和可解析的调用边",
        "sensitive API",
        "不等于漏洞成立",
        "find callers",
        "未找到直接调用方",
        "code metrics",
        "不能仅凭指标阈值报告问题",
        "风险画像是审查先验",
        "标签知识是检查清单",
        "truncated=true",
        "明确当前候选缺少的具体事实",
        "已有上下文为什么不能回答",
        "每次工具调用都会增加",
        "上下文充分时必须略过工具",
    )
    forbidden_reasons = ("重新确认", "了解完整代码", "看看还有没有其他问题")

    for reviewer in DEFAULT_REVIEWERS:
        text = build_reviewer_system_prompt(reviewer, "KNOWLEDGE_MARKER")
        assert all(phrase in text for phrase in required)
        assert all(reason in text for reason in forbidden_reasons)
        assert text.count("KNOWLEDGE_MARKER") == 1
```

再新增三领域差异断言：

```python
def test_effective_reviewer_prompts_keep_domain_specific_context_gaps() -> None:
    by_name = {
        reviewer.name: build_reviewer_system_prompt(reviewer)
        for reviewer in DEFAULT_REVIEWERS
    }
    assert "输入来源、传播路径、防护或敏感 sink" in by_name["ThreatModelAgent"]
    assert "调用方契约、状态变化、错误路径或业务不变量" in by_name["BehaviorAgent"]
    assert "复杂度、重复、资源所有权或跨文件设计" in by_name["MaintainabilityAgent"]
```

- [ ] **Step 2: 运行测试并确认因接口/契约缺失而失败**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_prompt_contracts.py -q
```

Expected: FAIL，首先表现为无法导入 `build_reviewer_system_prompt`，实现接口后则会因契约短语缺失继续失败。

- [ ] **Step 3: 在 reviewer_stage.py 增加唯一 Prompt 组合接口**

在 `_load_prompt` 后增加：

```python
_DISCOVERY_CONTEXT_CONTRACT = "discovery-context-contract.txt"


def build_reviewer_system_prompt(
    reviewer: Reviewer,
    task_knowledge: str = "",
) -> str:
    """组合角色方法论、共享上下文契约和 task-scoped 标签知识。"""
    parts = [
        _load_prompt(reviewer.prompt_file).strip(),
        _load_prompt(_DISCOVERY_CONTEXT_CONTRACT).strip(),
    ]
    if task_knowledge.strip():
        parts.append(task_knowledge.strip())
    return "\n\n".join(parts)
```

在 `graph.py` 导入该函数，并把子图内 `_system_prompt` 改为：

```python
def _system_prompt(state: ReviewerState) -> str:
    return build_reviewer_system_prompt(
        reviewer,
        state.get("task_knowledge") or "",
    )
```

- [ ] **Step 4: 创建共享契约文件**

`discovery-context-contract.txt` 必须完整写入以下内容；允许润色，但不得删减语义：

```text
## 前置节点提供的上下文

你收到的上下文由管线在当前发现阶段之前构建。先消费这些事实，再判断是否存在必须通过工具填补的缺口。

- task patch 是当前 hunk 的 unified diff，也是本轮 Issue 的唯一合法定位范围。它包含变更行和有限上下文行，不保证包含整个文件；新增文件的 patch 可能已经包含完整文件。候选所需代码已经出现时，不得用 get_file_content 重读当前文件。
- 整体变更摘要只用于理解 PR 意图和 task 关系，可能省略细节，不能单独证明 Issue，也不能用于报告当前 task 之外的问题。
- 风险画像是根据路径和 diff 变化生成的审查先验，只解释为什么路由到当前领域，不表示缺陷已经存在。
- AST structure 是 Java Gateway 对变更文件的静态解析，并已切到当前 hunk 所在文件。它可能提供类、方法、方法行范围、控制流节点和可解析的调用边；它不代表存在问题，也不保证覆盖反射、动态调用或无法解析的代码。AST 已回答结构问题时，不得读取全文取得相同信息。
- sensitive API 是对变更文件的敏感 API 静态扫描，并已筛选到当前文件和当前 hunk 行范围。它包含 API、位置、调用参数和规则危险等级；该等级只描述 API 敏感度，不等于漏洞成立或最终 severity，也不证明输入可控、路径可达或缺少防护。
- find callers 是在风险标签需要且 AST 能定位当前方法时预取的直接调用方。它用于判断契约和影响范围；未找到直接调用方只表示静态工具未发现，不覆盖反射、框架绑定和动态调用。
- code metrics 是风险标签需要时预取的当前文件方法级圈复杂度、LOC、嵌套深度和参数数量。它只能辅助解释具体维护成本，不能仅凭指标阈值报告问题。
- 标签知识是当前风险标签的方法论、正例和反例，是检查清单，不是仓库事实。
- truncated=true 表示事实受字符预算截断；只有候选确实依赖被截掉的部分时，截断才构成工具调用理由。

## 工具调用硬门槛

调用任何工具前必须完成：
1. 明确当前候选缺少的具体事实。
2. 检查 task patch、摘要、风险画像、AST、敏感 API、调用方、代码指标和标签知识。
3. 确认已有上下文无法回答，并判断缺口来自未覆盖范围、截断、解析局限还是其他文件。
4. 选择能填补该缺口的最小工具和最小参数范围。
5. 工具返回目标事实后停止扩展；只有新结果暴露另一个会改变候选结论的具体缺口时才继续。

“重新确认”“了解完整代码”“看看还有没有其他问题”不是合法调用理由。每次工具调用都会增加执行成本，并把结果追加进当前对话上下文；上下文充分时必须略过工具。
```

- [ ] **Step 5: 收紧三个领域 Prompt 的工具段**

保留各文件现有工具名和无工具降级规则，把泛化的“导航→细读”说明分别补成：

```text
# threat-model-base.txt
只有当前候选缺少输入来源、传播路径、防护或敏感 sink 的事实，且共享上下文没有回答时，才使用最小必要工具。不得为重读当前 patch、复核已有 sensitive API 命中或泛化探索调用工具。

# behavior-base.txt
只有当前候选缺少调用方契约、状态变化、错误路径或业务不变量的事实，且共享上下文没有回答时，才使用最小必要工具。不得重复查询已有调用方结果或重读已充分展示的实现。

# maintainability-base.txt
只有当前候选缺少复杂度、重复、资源所有权或跨文件设计事实，且共享上下文没有回答时，才使用最小必要工具。不得重复获取已有 code metrics，或为获取 AST 已提供的结构读取全文。
```

- [ ] **Step 6: 验证 Prompt 与图注入测试通过**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_prompt_contracts.py tests/test_graph_orchestration.py::test_make_reviewer_node_injects_matched_tag_knowledge_into_system_prompt -q
```

Expected: PASS。

- [ ] **Step 7: 提交 Prompt 契约**

```powershell
git add services/agent/src/codeguard_agent/prompts/discovery-context-contract.txt services/agent/src/codeguard_agent/prompts/threat-model-base.txt services/agent/src/codeguard_agent/prompts/behavior-base.txt services/agent/src/codeguard_agent/prompts/maintainability-base.txt services/agent/src/codeguard_agent/pipeline/stages/reviewer_stage.py services/agent/src/codeguard_agent/pipeline/graph.py services/agent/tests/test_prompt_contracts.py services/agent/tests/test_graph_orchestration.py
git commit -m "feat(prompts): 明确发现上下文与工具调用门槛"
```

---

### Task 2: 实现 reviewer-scoped single-flight 协调器

**Files:**
- Create: `services/agent/src/codeguard_agent/pipeline/discovery_tools.py`
- Create: `services/agent/tests/test_discovery_tools.py`

**Interfaces:**
- Consumes: `ToolResponse`；底层客户端的 `get_file_content`、`find_sensitive_apis`、`find_callers`、`get_code_metrics`。
- Produces:
  - `DISCOVERY_GATEWAY_TOOLS: frozenset[str]`
  - `canonical_tool_key(tool_name: str, arguments: dict[str, Any]) -> tuple[str, str]`
  - `DiscoveryToolCoordinator.execute(key: tuple[str, str], call: Callable[[], ToolResponse]) -> ToolResponse`
  - `CoordinatedDiscoveryToolClient(delegate: Any, coordinator: DiscoveryToolCoordinator)`

- [ ] **Step 1: 写协调器失败测试**

创建 `test_discovery_tools.py`，至少包含以下测试骨架：

```python
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event, Lock

from codeguard_agent.pipeline.discovery_tools import (
    REPEATED_TOOL_RESULT,
    CoordinatedDiscoveryToolClient,
    DiscoveryToolCoordinator,
    canonical_tool_key,
)
from codeguard_agent.tools.tool_client import ToolResponse


class _FakeClient:
    def __init__(self, responses: list[ToolResponse] | None = None) -> None:
        self.calls = 0
        self._responses = list(responses or [ToolResponse(True, "FULL BODY")])
        self._lock = Lock()

    def get_file_content(self, file_path: str) -> ToolResponse:
        with self._lock:
            index = self.calls
            self.calls += 1
        return self._responses[min(index, len(self._responses) - 1)]


def test_canonical_key_normalizes_slashes_and_dot_segments_without_lowercasing() -> None:
    left = canonical_tool_key("get_file_content", {"file_path": "src\\.\\A.java"})
    right = canonical_tool_key("get_file_content", {"file_path": "src/A.java"})
    lower = canonical_tool_key("get_file_content", {"file_path": "src/a.java"})
    assert left == right
    assert left != lower


def test_same_conversation_repeated_read_returns_short_marker() -> None:
    raw = _FakeClient()
    client = CoordinatedDiscoveryToolClient(raw, DiscoveryToolCoordinator())
    first = client.get_file_content("src/A.java")
    second = client.get_file_content("src/A.java")
    assert first.result == "FULL BODY"
    assert second.result == REPEATED_TOOL_RESULT
    assert raw.calls == 1


def test_parallel_task_clients_share_single_flight_but_both_receive_full_result() -> None:
    raw = _FakeClient()
    coordinator = DiscoveryToolCoordinator()
    clients = [CoordinatedDiscoveryToolClient(raw, coordinator) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda c: c.get_file_content("src/A.java"), clients))
    assert [result.result for result in results] == ["FULL BODY", "FULL BODY"]
    assert raw.calls == 1


def test_empty_success_is_not_cached() -> None:
    raw = _FakeClient([ToolResponse(True, ""), ToolResponse(True, "RECOVERED")])
    coordinator = DiscoveryToolCoordinator()
    first = CoordinatedDiscoveryToolClient(raw, coordinator)
    second = CoordinatedDiscoveryToolClient(raw, coordinator)
    assert first.get_file_content("src/A.java").result == ""
    assert second.get_file_content("src/A.java").result == "RECOVERED"
    assert raw.calls == 2


def test_different_arguments_execute_separately() -> None:
    raw = _FakeClient()
    coordinator = DiscoveryToolCoordinator()
    client = CoordinatedDiscoveryToolClient(raw, coordinator)
    client.get_file_content("src/A.java")
    client.get_file_content("src/B.java")
    assert raw.calls == 2


def test_parameterless_tool_key_is_stable() -> None:
    assert canonical_tool_key("find_sensitive_apis", {}) == (
        "find_sensitive_apis",
        "{}",
    )


def test_find_callers_normalizes_only_query_path() -> None:
    left = canonical_tool_key("find_callers", {"query": "src\\.\\A.java#Run"})
    right = canonical_tool_key("find_callers", {"query": "src/A.java#Run"})
    different_method_case = canonical_tool_key(
        "find_callers", {"query": "src/A.java#run"}
    )
    assert left == right
    assert left != different_method_case


def test_separate_coordinators_do_not_share_cache() -> None:
    raw = _FakeClient()
    one = CoordinatedDiscoveryToolClient(raw, DiscoveryToolCoordinator())
    two = CoordinatedDiscoveryToolClient(raw, DiscoveryToolCoordinator())
    assert one.get_file_content("src/A.java").success
    assert two.get_file_content("src/A.java").success
    assert raw.calls == 2
```

- [ ] **Step 2: 运行测试并确认模块不存在**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_discovery_tools.py -q
```

Expected: FAIL with `ModuleNotFoundError: codeguard_agent.pipeline.discovery_tools`。

- [ ] **Step 3: 实现最小协调模块**

`discovery_tools.py` 使用以下完整结构，不增加异步接口或第三方依赖：

```python
from __future__ import annotations

import json
import posixpath
from collections.abc import Callable
from concurrent.futures import Future
from threading import Lock
from typing import Any

from codeguard_agent.tools.tool_client import ToolResponse

DISCOVERY_GATEWAY_TOOLS = frozenset({
    "get_file_content",
    "find_sensitive_apis",
    "find_callers",
    "get_code_metrics",
})
REPEATED_TOOL_RESULT = (
    "该工具和参数已经在当前对话中成功返回；请复用前述结果，不要重复读取。"
)
ToolKey = tuple[str, str]


def _normalize_path(value: str) -> str:
    normalized = posixpath.normpath(value.replace("\\", "/"))
    return "." if normalized == "" else normalized


def _canonical_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(arguments)
    file_path = normalized.get("file_path")
    if isinstance(file_path, str):
        normalized["file_path"] = _normalize_path(file_path)
    query = normalized.get("query")
    if isinstance(query, str) and "#" in query:
        path, method = query.split("#", 1)
        normalized["query"] = f"{_normalize_path(path)}#{method}"
    return normalized


def canonical_tool_key(tool_name: str, arguments: dict[str, Any]) -> ToolKey:
    payload = json.dumps(
        _canonical_arguments(arguments),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return tool_name, payload


def _cacheable(response: ToolResponse) -> bool:
    return response.success and bool((response.result or "").strip())


class DiscoveryToolCoordinator:
    def __init__(self) -> None:
        self._lock = Lock()
        self._completed: dict[ToolKey, ToolResponse] = {}
        self._in_flight: dict[ToolKey, Future[ToolResponse]] = {}

    def execute(
        self,
        key: ToolKey,
        call: Callable[[], ToolResponse],
    ) -> ToolResponse:
        with self._lock:
            cached = self._completed.get(key)
            if cached is not None:
                return cached
            future = self._in_flight.get(key)
            leader = future is None
            if future is None:
                future = Future()
                self._in_flight[key] = future

        if not leader:
            return future.result()

        try:
            try:
                response = call()
            except Exception as exc:  # noqa: BLE001
                response = ToolResponse(success=False, error=str(exc))
            with self._lock:
                if _cacheable(response):
                    self._completed[key] = response
                self._in_flight.pop(key, None)
            future.set_result(response)
            return response
        except BaseException as exc:
            with self._lock:
                self._in_flight.pop(key, None)
            future.set_exception(exc)
            raise


class CoordinatedDiscoveryToolClient:
    def __init__(self, delegate: Any, coordinator: DiscoveryToolCoordinator) -> None:
        self._delegate = delegate
        self._coordinator = coordinator
        self._seen: set[ToolKey] = set()

    def _invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        call: Callable[[], ToolResponse],
    ) -> ToolResponse:
        key = canonical_tool_key(tool_name, arguments)
        if key in self._seen:
            return ToolResponse(success=True, result=REPEATED_TOOL_RESULT)
        response = self._coordinator.execute(key, call)
        if _cacheable(response):
            self._seen.add(key)
        return response

    def get_file_content(self, file_path: str) -> ToolResponse:
        return self._invoke(
            "get_file_content",
            {"file_path": file_path},
            lambda: self._delegate.get_file_content(file_path),
        )

    def find_sensitive_apis(self) -> ToolResponse:
        return self._invoke(
            "find_sensitive_apis", {}, self._delegate.find_sensitive_apis
        )

    def find_callers(self, query: str) -> ToolResponse:
        return self._invoke(
            "find_callers",
            {"query": query},
            lambda: self._delegate.find_callers(query),
        )

    def get_code_metrics(self, file_path: str) -> ToolResponse:
        return self._invoke(
            "get_code_metrics",
            {"file_path": file_path},
            lambda: self._delegate.get_code_metrics(file_path),
        )
```

- [ ] **Step 4: 加强真正并发的失败 single-flight 测试**

增加以下测试。`started`/`release` 确保第二个 wrapper 在首次结果完成前进入协调器；不要用顺序调用冒充并发 single-flight。

```python
def test_parallel_failure_is_shared_then_later_call_retries(monkeypatch) -> None:
    started = Event()
    release = Event()
    waiter_entered = Event()
    original_future_result = Future.result

    def _observed_result(self, *args, **kwargs):
        waiter_entered.set()
        return original_future_result(self, *args, **kwargs)

    monkeypatch.setattr(Future, "result", _observed_result)

    class _BlockingFailureClient:
        def __init__(self) -> None:
            self.calls = 0
            self.lock = Lock()

        def get_file_content(self, file_path: str) -> ToolResponse:  # noqa: ARG002
            with self.lock:
                self.calls += 1
                call_number = self.calls
            if call_number == 1:
                started.set()
                assert release.wait(timeout=2)
                return ToolResponse(False, error="temporary")
            return ToolResponse(True, "RECOVERED")

    raw = _BlockingFailureClient()
    coordinator = DiscoveryToolCoordinator()
    clients = [CoordinatedDiscoveryToolClient(raw, coordinator) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(clients[0].get_file_content, "src/A.java")
        assert started.wait(timeout=2)
        second = pool.submit(clients[1].get_file_content, "src/A.java")
        assert waiter_entered.wait(timeout=2)
        release.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert all(result.success is False for result in results)
    assert raw.calls == 1

    retry = CoordinatedDiscoveryToolClient(raw, coordinator)
    assert retry.get_file_content("src/A.java").result == "RECOVERED"
    assert raw.calls == 2
```

- [ ] **Step 5: 跑协调器测试和静态检查**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_discovery_tools.py -q
conda run -n codeguard --no-capture-output ruff check src/codeguard_agent/pipeline/discovery_tools.py tests/test_discovery_tools.py
conda run -n codeguard --no-capture-output mypy src/codeguard_agent/pipeline/discovery_tools.py
```

Expected: 全部 PASS；mypy 不允许裸 `Future` 或不一致的返回类型。

- [ ] **Step 6: 提交协调器**

```powershell
git add services/agent/src/codeguard_agent/pipeline/discovery_tools.py services/agent/tests/test_discovery_tools.py
git commit -m "feat(pipeline): 增加发现者工具去重协调器"
```

---

### Task 3: 把协调器接入 task-scoped 发现者

**Files:**
- Modify: `services/agent/src/codeguard_agent/pipeline/graph.py:151-180, 544-625, 672-807`
- Test: `services/agent/tests/test_graph_orchestration.py:876-956`

**Interfaces:**
- Consumes: `DiscoveryToolCoordinator`、`CoordinatedDiscoveryToolClient`。
- Produces: `ReviewerState.review_tool_client: Any`；同一 reviewer node 的 task wrapper 共享协调器，但各自持有独立 `_seen`。

- [ ] **Step 1: 写同 reviewer 并发复用的失败测试**

在 `test_graph_orchestration.py` 增加以下 helper 和测试；同时导入 `Lock` 与 `ToolResponse`：

```python
class _CountingFileClient:
    def __init__(self) -> None:
        self.calls = 0
        self.lock = Lock()

    def get_file_content(self, file_path: str) -> ToolResponse:
        with self.lock:
            self.calls += 1
        return ToolResponse(success=True, result="FULL BODY")


def _two_behavior_task_state() -> dict:
    tasks = [
        G.ReviewTask(id="A.java#h0", file="A.java", patch="+a", changed_lines=[1]),
        G.ReviewTask(id="B.java#h0", file="B.java", patch="+b", changed_lines=[1]),
    ]
    return {
        "diff_text": "+a\n+b",
        "review_tasks": tasks,
        "risk_profiles": {
            task.id: G.RiskProfile(
                task_id=task.id,
                tag_scores={RiskTag.NULL_STATE_SAFETY: 2},
            )
            for task in tasks
        },
        "task_selection": G.TaskSelection(
            selected_task_ids=[task.id for task in tasks]
        ),
    }


def test_make_reviewer_node_shares_tool_coordinator_between_its_tasks(monkeypatch):
    raw_client = _CountingFileClient()
    returned_bodies: list[str] = []
    result_lock = Lock()

    def _invoke(payload, config=None):  # noqa: ARG001
        response = payload["review_tool_client"].get_file_content("Shared.java")
        with result_lock:
            returned_bodies.append(response.result or "")
        return {"issues": [], "council_trace": []}

    import types

    monkeypatch.setattr(
        G,
        "build_reviewer_subgraph",
        lambda *args, **kwargs: types.SimpleNamespace(invoke=_invoke),
    )
    node = G.make_reviewer_node(
        G.DEFAULT_REVIEWERS[1], llm=_FakeLLM(), tool_client=raw_client
    )

    node(_two_behavior_task_state())

    assert raw_client.calls == 1
    assert sorted(returned_bodies) == ["FULL BODY", "FULL BODY"]
```

再增加 review 生命周期隔离测试：

```python
def test_make_reviewer_node_does_not_cache_across_reviews(monkeypatch):
    raw_client = _CountingFileClient()

    def _invoke(payload, config=None):  # noqa: ARG001
        payload["review_tool_client"].get_file_content("Shared.java")
        return {"issues": [], "council_trace": []}

    import types

    monkeypatch.setattr(
        G,
        "build_reviewer_subgraph",
        lambda *args, **kwargs: types.SimpleNamespace(invoke=_invoke),
    )
    node = G.make_reviewer_node(
        G.DEFAULT_REVIEWERS[1], llm=_FakeLLM(), tool_client=raw_client
    )

    one_task = _two_behavior_task_state()
    one_task["review_tasks"] = one_task["review_tasks"][:1]
    first_id = one_task["review_tasks"][0].id
    one_task["risk_profiles"] = {first_id: one_task["risk_profiles"][first_id]}
    one_task["task_selection"] = G.TaskSelection(selected_task_ids=[first_id])
    node(one_task)
    node(one_task)

    assert raw_client.calls == 2
```

跨 reviewer 隔离已由 Task 2 的 `test_separate_coordinators_do_not_share_cache` 钉住；这里不通过复杂路由重复测试同一事实。两个层次共同证明：wrapper 共享只发生在单次 reviewer node 执行内部。

测试 fake 必须接受 `invoke(payload, config=None)`，避免被 `run_bounded_parallel` 当作签名错误吞掉。

- [ ] **Step 2: 运行新测试确认底层调用仍重复**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_graph_orchestration.py -k "tool_coordinator or tool_cache" -q
```

Expected: FAIL；当前 payload 没有 `review_tool_client`，或底层调用次数为 2。

- [ ] **Step 3: 扩展 ReviewerState 和引擎选择**

在 `ReviewerState` 增加：

```python
review_tool_client: Any
```

在 `build_reviewer_subgraph._review` 中只为 react tier选择 task 注入客户端：

```python
effective_tool_client = state.get("review_tool_client") or tool_client
engine = (
    _make_engine(state, tool_client=None)
    if tier == "direct"
    else _make_engine(state, tool_client=effective_tool_client)
)
```

保留 `_direct_fallback` 无工具行为，不把 wrapper 传入 DirectEngine。

- [ ] **Step 4: 在每次 reviewer node 执行时创建协调器**

在 `make_reviewer_node(...)._node` 函数体内、读取 state 后创建：

```python
coordinator = DiscoveryToolCoordinator() if tool_client is not None else None


def _task_tool_client():
    if tool_client is None or coordinator is None:
        return None
    return CoordinatedDiscoveryToolClient(tool_client, coordinator)
```

这个位置是硬约束：不得把 `coordinator` 放在 `make_reviewer_node` 外层闭包或 `build_reviewer_subgraph` 编译期，否则编译后的图重复 invoke 会跨 review 复用旧仓库事实。

在兼容路径的 subgraph payload 增加：

```python
"review_tool_client": _task_tool_client(),
```

在每个 `_invoke_one(task_id)` 的 payload 同样增加该字段。每次调用 `_task_tool_client()` 都必须创建新 wrapper，使不同 task 的 `_seen` 集合互不共享；它们只共享同一个 `coordinator`。

- [ ] **Step 5: 运行图编排回归**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_graph_orchestration.py tests/test_graph_phase5b.py tests/test_reviewer_stage.py -q
```

Expected: PASS；特别确认 direct tier、空结果 fallback、MemorySaver fan-out 和 tier 规划测试未回退。

- [ ] **Step 6: 提交图接入**

```powershell
git add services/agent/src/codeguard_agent/pipeline/graph.py services/agent/tests/test_graph_orchestration.py
git commit -m "feat(pipeline): 接入发现者级工具结果复用"
```

---

### Task 4: 过滤伪工具并去除重复 GatheredContext

**Files:**
- Modify: `services/agent/src/codeguard_agent/pipeline/engines.py:238-282`
- Modify: `services/agent/tests/test_engines.py:15-105`

**Interfaces:**
- Consumes: `DISCOVERY_GATEWAY_TOOLS`、AIMessage `tool_calls`、ToolMessage。
- Produces: `_extract_gathered_context(raw: Any) -> list[GatheredContext]` 只返回真实发现工具，并按 `(tool, canonical args)` 保留首次非空内容。

- [ ] **Step 1: 写过滤与去重失败测试**

在 `test_engines.py` 增加：

```python
def test_gathered_context_excludes_structured_response_tool_message() -> None:
    raw = {
        "messages": [
            _AIMsg([{"id": "r1", "name": "ReviewResult", "args": {"issues": []}}]),
            _ToolMsg("r1", "Returning structured response", name="ReviewResult"),
        ]
    }
    assert _extract_gathered_context(raw) == []


def test_gathered_context_keeps_first_result_for_duplicate_tool_and_args() -> None:
    raw = {
        "messages": [
            _AIMsg([{"id": "c1", "name": "get_file_content", "args": {"file_path": "A.java"}}]),
            _ToolMsg("c1", "FULL BODY"),
            _AIMsg([{"id": "c2", "name": "get_file_content", "args": {"file_path": "A.java"}}]),
            _ToolMsg("c2", "该工具和参数已经在当前对话中成功返回"),
        ]
    }
    got = _extract_gathered_context(raw)
    assert len(got) == 1
    assert got[0].content == "FULL BODY"


def test_gathered_context_keeps_same_tool_with_different_args() -> None:
    raw = {
        "messages": [
            _AIMsg([{"id": "a", "name": "get_file_content", "args": {"file_path": "A.java"}}]),
            _ToolMsg("a", "A"),
            _AIMsg([{"id": "b", "name": "get_file_content", "args": {"file_path": "B.java"}}]),
            _ToolMsg("b", "B"),
        ]
    }
    assert [item.content for item in _extract_gathered_context(raw)] == ["A", "B"]
```

- [ ] **Step 2: 运行测试并确认当前实现收集 ReviewResult/重复项**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_engines.py -q
```

Expected: 至少前两个新增测试 FAIL。

- [ ] **Step 3: 最小修改抽取逻辑**

从 `discovery_tools` 导入 `DISCOVERY_GATEWAY_TOOLS`。在第二遍 ToolMessage 扫描中加入 allowlist 和首次键集合：

```python
gathered: list[GatheredContext] = []
seen: set[tuple[str, str]] = set()
for msg in messages:
    if getattr(msg, "type", "") != "tool":
        continue
    cid = getattr(msg, "tool_call_id", None)
    name, args = call_meta.get(
        cid or "", (getattr(msg, "name", "") or "", "")
    )
    if name not in DISCOVERY_GATEWAY_TOOLS:
        continue
    key = (name, args)
    if key in seen:
        continue
    content = getattr(msg, "content", "")
    content = content if isinstance(content, str) else str(content)
    if not content.strip():
        continue
    seen.add(key)
    gathered.append(GatheredContext(tool=name, args=args, content=content))
```

不要用 `ReviewResult.__name__` 单点排除；使用真实 Gateway allowlist，避免未来其他 response-format 伪工具继续污染。

- [ ] **Step 4: 跑引擎与工具画像测试**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_engines.py tests/test_tool_usage.py tests/test_graph_orchestration.py::test_dedup_gathered_reducer_dedups_by_tool_args_keep_order -q
```

Expected: PASS。

- [ ] **Step 5: 提交上下文过滤**

```powershell
git add services/agent/src/codeguard_agent/pipeline/engines.py services/agent/tests/test_engines.py
git commit -m "fix(pipeline): 过滤重复与伪工具上下文"
```

---

### Task 5: 增加完整 Patch 用例并执行总体验证

**Files:**
- Create: `services/agent/evals/dataset/repo/discovery_context_complete_patch_001/case.yaml`
- Create: `services/agent/evals/dataset/repo/discovery_context_complete_patch_001/changes.diff`
- Create: `services/agent/evals/dataset/repo/discovery_context_complete_patch_001/repo/src/main/java/com/demo/BatchCounter.java`
- Verify: all Python sources/tests
- Output only: `services/agent/evals/reports/discovery-context-after.md`

**Interfaces:**
- Consumes: `pipeline-file` profile和 eval 工具画像中的 `files_read`。
- Produces: 一个 repo-backed 回归场景；改动后质量/工具调用对照记录。

- [ ] **Step 1: 创建 repo-backed 用例**

`case.yaml`：

```yaml
id: discovery_context_complete_patch_001
category: 边界错误
dimension: logic
capability: [diff-only]
description: >
  BatchCounter 是完整新增文件，task patch 已包含全部实现，AST 可提供类、方法和控制流。
  count 中使用 i <= items.size()，导致计数多 1；发现该问题不需要读取当前文件全文。
expected:
  - type_keywords: ["边界", "off-by-one", "越界", "计数"]
    file: BatchCounter.java
    line: 9
    tolerance: 3
    severity: WARNING
    note: 循环条件应为 i < items.size()，当前实现使 total 比元素数多 1
```

`repo/src/main/java/com/demo/BatchCounter.java`：

```java
package com.demo;

import java.util.List;

public class BatchCounter {
    public int count(List<String> items) {
        int total = 0;
        for (int i = 0; i <= items.size(); i++) {
            total++;
        }
        return total;
    }
}
```

`changes.diff` 必须是该文件从 `/dev/null` 新增的完整 unified diff，new-side 第 9 行对应 `total++` 或将 `case.yaml` 的期望行调整到实际问题行；不要手写不一致的行号。

使用以下精确内容：

```diff
diff --git a/src/main/java/com/demo/BatchCounter.java b/src/main/java/com/demo/BatchCounter.java
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/src/main/java/com/demo/BatchCounter.java
@@ -0,0 +1,13 @@
+package com.demo;
+
+import java.util.List;
+
+public class BatchCounter {
+    public int count(List<String> items) {
+        int total = 0;
+        for (int i = 0; i <= items.size(); i++) {
+            total++;
+        }
+        return total;
+    }
+}
```

- [ ] **Step 2: 验证数据集可加载**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/test_dataset.py tests/test_profiles.py -q
```

Expected: PASS，新 case 被加载为 repo-backed，`repo_path` 指向快照目录。

- [ ] **Step 3: 跑全部确定性检查**

Run:

```powershell
conda run -n codeguard --no-capture-output python -m pytest tests/ -q
conda run -n codeguard --no-capture-output ruff check src/ tests/
conda run -n codeguard --no-capture-output mypy src/
```

Expected: 全部 PASS。若 ruff 对历史 tests 报既有问题，至少要求 `ruff check src/` 和所有本次修改文件通过，并在交接说明中列出既有失败；不得静默忽略。

- [ ] **Step 4: 运行改动后工具档评测**

```powershell
$env:CODEGUARD_TOOL_SERVER_URL="http://localhost:9090"
conda run -n codeguard --no-capture-output python -m evals.runner --profile pipeline-file --runs 1 --report evals/reports/discovery-context-after.md
```

Expected:

- `discovery_context_complete_patch_001` 能召回边界错误；
- 该用例的 `files_read` 不包含 `BatchCounter.java`；
- 同一发现者内报告的重复工具键为 0；
- 总 `get_file_content`/上下文量相对 before 报告下降；
- Precision、Recall、F1 和关键问题 recall 无明显回退；
- `file_npe_contract_001` 等确实依赖未展示文件内容的用例仍能读取文件并召回。

真实 LLM 有方差；如果单次指标变化，先检查逐 case 工具画像和漏报原因，不得为了追求数字删除硬门槛。必要时按项目规范追加到 3 runs 后再判断。

- [ ] **Step 5: 提交 eval 用例**

```powershell
git add services/agent/evals/dataset/repo/discovery_context_complete_patch_001
git commit -m "test(evals): 增加完整上下文免工具用例"
```

- [ ] **Step 6: 最终检查提交边界**

Run from repository root:

```powershell
git status --short
git log -5 --oneline
```

Expected: 应用改动拆成上述 5 个 Conventional Commits；工作区只剩实施前已有的用户未跟踪文件和明确不提交的 eval 报告/归档。不要把 `.env`、trace、真实密钥或用户的 `DESERIALIZATION.txt` 纳入提交。

---

## Implementation Notes for the Receiving Agent

- `ToolAgentEngine` 每个 task 都会重新创建，因此 per-conversation `_seen` 最自然的归属是 `CoordinatedDiscoveryToolClient` 实例，不是共享 coordinator。
- `make_reviewer_node` 返回的节点闭包会被编译图长期持有；缓存若创建在 `_node` 外层会跨 review 泄漏，这是本实现最危险的错误。
- `run_bounded_parallel` 会吞掉 task 异常并返回 `None`。接入测试必须断言底层调用数和完整结果，不能只断言节点没有抛异常。
- ContextProvider 已经独立预取 AST、敏感 API、调用方和指标；不要把它切换到新协调器。新协调器只服务发现者 ReAct 工具。
- EvidenceAgent 已有自己的请求级工具去重；不要把本协调器接入 EvidenceAgent，也不要删除其缓存。
- `ReviewResult` 是 LangChain response format 产生的伪工具消息。过滤发生在 `_extract_gathered_context`，不修改 LangChain response format。
- 如果实现时发现工具 schema 必须增加 `missing_fact/context_gap` 才能可靠约束模型，停止并提出独立设计，不要在本计划中顺手扩大协议。
