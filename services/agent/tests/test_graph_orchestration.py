
"""ADR-032 ReviewCouncil 编排测试。"""

from __future__ import annotations

from pathlib import Path
from threading import Lock
import json

import codeguard_agent.pipeline.orchestrator as orchestrator_module
from codeguard_agent.models.council import ContextBundle, ContextFact
from codeguard_agent.models.knowledge import KnowledgeBundle
from codeguard_agent.models.schemas import Issue, ReviewResult, Severity
from codeguard_agent.models.tasks import (
    ReviewBudget,
    RiskCoverage,
    RiskHypothesis,
    RiskTag,
    TaskRiskPrior,
)
from codeguard_agent.pipeline import graph as G
from codeguard_agent.pipeline.engines import GatheredContext, ReviewOutcome
from codeguard_agent.pipeline.orchestrator import PipelineOrchestrator
from codeguard_agent.pipeline.context.base import PipelineContext
from codeguard_agent.pipeline.context.provider import ContextProviderStage
from codeguard_agent.tools.tool_client import ToolResponse


def _prior(task_id: str, tag: RiskTag | None = None, priority: int = 2) -> TaskRiskPrior:
    hypotheses = ()
    if tag is not None and tag is not RiskTag.GENERAL_REVIEW:
        hypotheses = (
            RiskHypothesis(
                tag=tag,
                match_confidence={1: 0.45, 2: 0.70, 3: 0.85}[priority],
                review_priority=priority,
                source_kind="diff_text",
                source="text:added:test",
                reason="test",
            ),
        )
    return TaskRiskPrior(
        task_id=task_id,
        hypotheses=hypotheses,
        coverage=(
            RiskCoverage.CONFIDENT if hypotheses else RiskCoverage.UNCLASSIFIED
        ),
    )


def test_dedup_gathered_reducer_dedups_by_tool_args_keep_order():
    a = GatheredContext("get_file_content", "A.java", "x")
    b = GatheredContext("get_file_content", "B.java", "y")
    a_dup = GatheredContext("get_file_content", "A.java", "x-again")
    out = G.dedup_gathered_reducer([a], [b, a_dup])
    assert [g.args for g in out] == ["A.java", "B.java"]


def test_dedup_gathered_reducer_canonicalizes_path_variants():
    first = GatheredContext(
        "get_file_content",
        json.dumps({"file_path": r"src\.\A.java"}),
        "first",
    )
    duplicate = GatheredContext(
        "get_file_content",
        json.dumps({"file_path": "src/A.java"}),
        "duplicate",
    )

    out = G.dedup_gathered_reducer([first], [duplicate])

    assert out == [first]


def test_context_bundle_render_truncates():
    bundle = ContextBundle(
        changed_files=["A.java"],
        facts=[
            ContextFact(
                source="tool:get_diff_ast",
                kind="ast_structure",
                content="A.java " * 20,
            )
        ],
    )
    rendered = bundle.render(20)
    assert "A.java" in rendered
    assert "ContextBundle 已达预算上限" in rendered


def test_context_provider_builds_fact_bundle_without_judgement():
    diff = "diff --git a/A.java b/A.java\n--- a/A.java\n+++ b/A.java\n@@ -1 +1,2 @@\n+int x=1;\n"
    ctx = PipelineContext(diff_text=diff, diff_summary="新增字段")
    ContextProviderStage().execute(ctx)
    assert ctx.context_bundle.changed_files == ["A.java"]
    text = ctx.context_bundle.render()
    assert "新增字段" not in text
    assert "漏洞" not in text


def test_context_provider_keeps_summary_and_files_out_of_facts():
    diff = "diff --git a/A.java b/A.java\n+++ b/A.java\n+class A {}"
    ctx = PipelineContext(diff_text=diff, diff_summary="新增 A")

    ContextProviderStage().execute(ctx)

    dumped = ctx.context_bundle.model_dump()
    assert set(dumped) == {"changed_files", "facts"}
    assert dumped["changed_files"] == ["A.java"]
    assert all(
        fact["kind"] not in {"changed_file", "summary"}
        for fact in dumped["facts"]
    )


def test_context_provider_records_tool_failures_as_diagnostics_not_facts():
    class _FailingBroadContextClient:
        def resolve_change_context(self, changes):  # noqa: ARG002
            return _MockToolResponse(False, error="graph timeout")

    ctx = PipelineContext(
        diff_text="diff --git a/A.java b/A.java\n+++ b/A.java\n+class A {}",
        tool_client=_FailingBroadContextClient(),
    )

    ContextProviderStage().execute(ctx)

    assert ctx.context_bundle.facts == []
    assert ctx.context_diagnostics == {"symbol_context": "graph timeout"}


def test_summary_prompts_only_request_summary():
    prompt_dir = Path(__file__).resolve().parents[1] / "src" / "codeguard_agent" / "prompts"
    combined = (
        (prompt_dir / "summary-system.txt").read_text(encoding="utf-8")
        + (prompt_dir / "summary-user.txt").read_text(encoding="utf-8")
    )
    for obsolete in (
        "changed_files",
        "change_types",
        "estimated_risk_level",
        "file_focus",
    ):
        assert obsolete not in combined
    assert "summary" in combined


def _base_state(**over):
    state = {
        "candidate_issues": [],
        "evidence_notes": [],
        "challenges": [],
    }
    state.update(over)
    return state


def _candidate(*, confidence=0.9):
    issue = Issue(
        severity=Severity.WARNING,
        file="A.java",
        line=1,

        type="t",
        message="m",
        confidence=confidence,
    )
    return G.CandidateIssue.from_issue(
        issue,
        source_agent="threat_model",
        index=1,
        task_id="A.java#h0",
    )


def test_reviewer_subgraph_exposes_internal_nodes():
    sub = G.build_reviewer_subgraph(G.DEFAULT_REVIEWERS[0])
    assert {"prepare", "review", "collect"} <= set(sub.get_graph().nodes)


def test_reviewer_state_excludes_retired_routing_fields():
    assert {
        "file_groups",
        "focus_notes",
        "enable_hitl",
        "dispatched",
        "eff_diff",
        "context_bundle",
        "task_risk_context",
    }.isdisjoint(G.ReviewerState.__annotations__)


def test_reviewer_prompt_contains_summary_once(monkeypatch):
    captured: dict[str, str] = {}

    class _CapturingEngine:
        def review(
            self,
            llm,
            *,
            system_prompt,
            user_prompt,
            reviewer_name,
            max_retries,
            structured_method,
            enable_hitl=False,
        ):
            captured["prompt"] = user_prompt
            return ReviewOutcome(ReviewResult(summary="", issues=[]))

    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _CapturingEngine())
    sub = G.build_reviewer_subgraph(G.DEFAULT_REVIEWERS[0], llm=_FakeLLM())

    sub.invoke({
        "diff_text": "diff --git a/A.java b/A.java\n+++ b/A.java\n+class A {}",
        "diff_summary": "唯一摘要标记-Task3",
        "structured_method": "function_calling",
        "review_task": G.ReviewTask(
            id="A.java#h0",
            file="A.java",
            patch="+class A {}",
            changed_lines=[1],
        ),
        "tier": "direct",
    })

    assert captured["prompt"].count("唯一摘要标记-Task3") == 1


def test_reviewer_review_uses_direct_engine_when_tier_is_direct(monkeypatch):
    calls = []

    class _ShouldNotBeCalledToolEngine:
        def __init__(self, *a, **k):
            calls.append("tool_agent")

    monkeypatch.setattr(G, "ToolAgentEngine", _ShouldNotBeCalledToolEngine)
    sub = G.build_reviewer_subgraph(
        G.DEFAULT_REVIEWERS[0], llm=_FakeLLM(), tool_client=object()
    )
    sub.invoke({
        "diff_text": "+x",
        "review_task": G.ReviewTask(
            id="A.java#h0", file="A.java", patch="+x", changed_lines=[1]
        ),
        "tier": "direct",
    })
    assert "tool_agent" not in calls


def test_review_tier_direct_empty_result_does_not_retry(monkeypatch):
    """tier=="direct" 时空结果是"确实没问题"的正确结论,不应二次调用兜底。"""
    calls = {"engine": 0, "fallback": 0}

    class _EmptyEngine:
        def review(self, llm, *, system_prompt, user_prompt, reviewer_name,
                    max_retries, structured_method, enable_hitl=False):
            calls["engine"] += 1
            return ReviewOutcome(ReviewResult(summary="", issues=[]))

    class _FallbackDirectEngine:
        def review(self, llm, *, system_prompt, user_prompt, reviewer_name,
                    max_retries, structured_method, enable_hitl=False):
            calls["fallback"] += 1
            return ReviewOutcome(ReviewResult(summary="", issues=[]))

    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _EmptyEngine())
    monkeypatch.setattr(G, "DirectEngine", _FallbackDirectEngine)
    sub = G.build_reviewer_subgraph(G.DEFAULT_REVIEWERS[0], llm=_FakeLLM())
    sub.invoke({
        "diff_text": "+x",
        "review_task": G.ReviewTask(
            id="A.java#h0", file="A.java", patch="+x", changed_lines=[1]
        ),
        "tier": "direct",
    })
    assert calls["engine"] == 1
    assert calls["fallback"] == 0


def test_review_tier_react_empty_result_still_retries_direct_fallback(monkeypatch):
    """tier=="react" 时保留原有的空结果降级复审(ReAct 偶发空响应的兜底)。"""
    calls = {"engine": 0, "fallback": 0}

    class _EmptyEngine:
        def review(self, llm, *, system_prompt, user_prompt, reviewer_name,
                    max_retries, structured_method, enable_hitl=False):
            calls["engine"] += 1
            return ReviewOutcome(ReviewResult(summary="", issues=[]))

    class _FallbackDirectEngine:
        def review(self, llm, *, system_prompt, user_prompt, reviewer_name,
                    max_retries, structured_method, enable_hitl=False):
            calls["fallback"] += 1
            return ReviewOutcome(ReviewResult(summary="fallback-summary", issues=[]))

    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _EmptyEngine())
    monkeypatch.setattr(G, "DirectEngine", _FallbackDirectEngine)
    sub = G.build_reviewer_subgraph(
        G.DEFAULT_REVIEWERS[0], llm=_FakeLLM(), tool_client=object()
    )
    sub.invoke({
        "diff_text": "+x",
        "review_task": G.ReviewTask(
            id="A.java#h0", file="A.java", patch="+x", changed_lines=[1]
        ),
        "tier": "react",
    })
    assert calls["engine"] == 1
    assert calls["fallback"] == 1


def test_review_tier_react_error_retries_direct_fallback(monkeypatch):
    calls = {"fallback": 0}

    class _FailingToolEngine:
        def review(self, *args, **kwargs):
            raise OSError("tool service unavailable")

    class _FallbackDirectEngine:
        def review(self, *args, **kwargs):
            calls["fallback"] += 1
            return ReviewOutcome(ReviewResult(summary="fallback", issues=[]))

    monkeypatch.setattr(
        G,
        "_make_engine",
        lambda state, tool_client=None: _FailingToolEngine(),
    )
    monkeypatch.setattr(G, "DirectEngine", _FallbackDirectEngine)
    sub = G.build_reviewer_subgraph(
        G.DEFAULT_REVIEWERS[0], llm=_FakeLLM(), tool_client=object()
    )

    output = sub.invoke({
        "diff_text": "+x",
        "review_task": G.ReviewTask(
            id="A.java#h0", file="A.java", patch="+x", changed_lines=[1]
        ),
        "tier": "react",
    })

    assert calls["fallback"] == 1
    assert any(
        trace.event == "react_degraded_error"
        for trace in output["council_trace"]
    )


def test_discoverer_prompts_do_not_reference_retired_routing():
    prompt_dir = Path(__file__).resolve().parents[1] / "src" / "codeguard_agent" / "prompts"
    for filename in (
        "threat-model-base.txt",
        "behavior-base.txt",
        "maintainability-base.txt",
    ):
        text = (prompt_dir / filename).read_text(encoding="utf-8")
        for obsolete in ("file_groups", "file_focus", "focus_notes", "Supervisor"):
            assert obsolete not in text


def test_default_discoverers_are_methodology_roles_with_tool_boundaries():
    by_source = {r.source_agent: r for r in G.DEFAULT_REVIEWERS}
    assert set(by_source) == {"threat_model", "behavior", "maintainability"}
    assert by_source["threat_model"].name == "ThreatModelAgent"
    assert by_source["threat_model"].tool_allowlist == [
        "get_file_content",
        "inspect_security_path",
    ]
    assert by_source["behavior"].name == "BehaviorAgent"
    assert by_source["behavior"].tool_allowlist == [
        "get_file_content",
        "inspect_change_impact",
    ]
    assert by_source["maintainability"].name == "MaintainabilityAgent"
    assert by_source["maintainability"].tool_allowlist == [
        "get_file_content",
        "inspect_structure",
    ]


def test_reviewer_subgraph_mock_only_threat_model_returns_issues():
    sec = G.build_reviewer_subgraph(
        G.Reviewer(
            "ThreatModelAgent",
            "threat-model-base.txt",
            source_agent="threat_model",
        ),
        llm=None,
    )
    other = G.build_reviewer_subgraph(
        G.Reviewer(
            "BehaviorAgent",
            "behavior-base.txt",
            source_agent="behavior",
        ),
        llm=None,
    )
    assert len(sec.invoke({})["issues"]) >= 1
    assert other.invoke({}).get("issues", []) == []


def test_run_routes_gathered_context_to_trace_sink_and_council_metadata(monkeypatch):
    gc = [GatheredContext("get_file_content", "X.java", "body")]
    issues = [Issue(severity=Severity.WARNING, file="X.java", line=1, type="t", message="m")]

    class _Stats:
        def model_dump(self):
            return {"candidate_count": 1, "verdict_count": 1}

    class _FakeGraph:
        def invoke(self, initial, config=None):
            return {
                "summary": "s",
                "final_issues": issues,
                "gathered_context": gc,
                "council_stats": _Stats(),
                "council_trace": [object(), object()],
            }

    monkeypatch.setattr(
        "codeguard_agent.pipeline.orchestrator.build_review_graph",
        lambda **k: _FakeGraph(),
    )

    trace: list = []
    meta: dict = {}
    result = PipelineOrchestrator().run(object(), "some diff", trace_sink=trace, metadata_sink=meta)

    assert isinstance(result, ReviewResult)
    assert result.issues == issues
    assert trace == gc
    assert meta["council"]["candidate_count"] == 1
    assert meta["council_trace_events"] == 2
    assert not hasattr(result, "candidate_issues")


def test_orchestrator_initial_state_omits_empty_runtime_outputs(monkeypatch):
    captured: dict = {}

    class _Graph:
        def invoke(self, initial, config=None):
            captured.update(initial)

            return {"summary": "", "final_issues": []}

    monkeypatch.setattr(
        orchestrator_module,
        "build_review_graph",
        lambda **_kwargs: _Graph(),
    )
    PipelineOrchestrator(enable_summary=False).run(None, _DIFF)

    assert {
        "gathered_context",
        "review_summaries",
        "candidate_issues",
        "evidence_requests",
        "evidence_notes",
        "council_verdicts",
        "council_trace",
        "judge_pass",
        "final_issues",
    }.isdisjoint(captured)
    assert captured["diff_text"] == _DIFF


def test_review_state_excludes_unused_judge_pass():
    assert "judge_pass" not in G.ReviewState.__annotations__


def test_run_empty_diff_short_circuits():
    result = PipelineOrchestrator().run(object(), "   ")
    assert result.issues == []
    assert "没有检测到代码变更" in result.summary


_DIFF = "diff --git a/A.java b/A.java\n--- a/A.java\n+++ b/A.java\n@@ -1 +1,2 @@\n+int x=1;\n"

_MOCK_DIFF = (
    "diff --git a/example/Demo.java b/example/Demo.java\n"
    "--- a/example/Demo.java\n"
    "+++ b/example/Demo.java\n"
    "@@ -42,0 +42,1 @@\n"
    "+int injected = 1;\n"
)

# 16 个文件的 diff —— 超过 medium_max_files=15，触发 large 模式，
# 确保管线测试走完整 pipeline 路径而非 direct_review 短路。
_LARGE_DIFF = "".join(
    f"diff --git a/F{i:02d}.java b/F{i:02d}.java\n"
    f"--- a/F{i:02d}.java\n"
    f"+++ b/F{i:02d}.java\n"
    f"@@ -1 +1,2 @@\n"
    f"+int x{i}=1;\n"
    for i in range(16)
)


def test_adr032_mock_end_to_end():
    result = PipelineOrchestrator(
        enable_summary=False,
        review_budget=ReviewBudget(small_max_files=0),
    ).run(None, _MOCK_DIFF)
    assert isinstance(result, ReviewResult)
    assert len(result.issues) == 1


class _Stub:
    def invoke(self, _msgs):
        return None


class _FakeLLM:
    def with_structured_output(self, *a, **k):
        return _Stub()


class _ValueStub:
    def __init__(self, value):
        self.value = value

    def invoke(self, _msgs):
        return self.value


class _ValueLLM:
    def __init__(self, value):
        self.value = value

    def with_structured_output(self, *a, **k):
        return _ValueStub(self.value)


def test_classify_mode_emits_structured_trace_route():
    output = G._classify_mode_node()({
        "diff_text": _DIFF,
        "review_budget": ReviewBudget(),
    })

    assert output["review_mode"] == "small"
    assert output["review_route"].initial_mode.value == "small"
    assert output["review_route"].selected_node == "direct_review"
    assert output["review_route"].metrics.file_count == 1
    assert output["review_route"].thresholds.small_max_files == 3
    assert output["council_trace"][0].event == "mode_selected"


def test_small_direct_review_missing_structured_output_requests_pipeline_fallback():
    output = G._direct_review_node(_FakeLLM())({
        "diff_text": _DIFF,
        "review_route": {
            "initial_mode": "small",
            "effective_mode": "small",
            "selected_node": "direct_review",
            "fallback": False,
        },
    })

    assert output["direct_review_status"] == "fallback"
    assert output["review_route"].effective_mode.value == "medium"
    assert output["review_route"].selected_node == "file_task_builder"
    assert output["review_route"].fallback_reason == "structured_output_missing"
    assert "final_issues" not in output
    assert output["council_trace"][0].event == "fallback"


def test_small_direct_review_success_ends_without_risk_pipeline_state():
    expected = ReviewResult(summary="clean", issues=[])
    output = G._direct_review_node(_ValueLLM(expected))({
        "diff_text": _DIFF,
        "review_route": {
            "initial_mode": "small",
            "effective_mode": "small",
            "selected_node": "direct_review",
            "fallback": False,
        },
    })

    assert output["direct_review_status"] == "completed"
    assert output["review_route"].outcome == "completed"
    assert output["summary"] == "clean"
    assert "risk_priors" not in output


class _EvidenceBatchStub:
    def invoke(self, messages):
        payload = json.loads(messages[-1][1])
        return {
            "findings": [
                {
                    "evidence_id": fact["evidence_id"],
                    "relation": "supports",
                    "strength": "contextual",
                    "observation": "the changed task patch supports the candidate",
                    "limitation": "",
                }
                for fact in payload["facts"]
            ]
        }


class _CouncilLLM:
    """Return valid evidence/synthesis structures for graph wiring tests."""

    def with_structured_output(self, schema, *args, **kwargs):
        if schema.__name__ == "_EvidenceAnalysisBatch":
            return _EvidenceBatchStub()
        if schema.__name__ == "CandidateEvidenceAssessment":
            return _ValueStub(
                {
                    "candidate_id": "C001",
                    "claim_status": "supported",
                    "counter_effect": "none",
                    "severity_factors": [],
                    "conflicts": [],
                    "reason": "supported for graph wiring test",
                }
            )
        return _Stub()


# 三路各发到自己的文件 line 1（都在各自 hunk 的 changed_lines，可绑定）；
# 不同文件天然不触发同文件邻行合并，三条候选独立存活。
_FAKE_TARGET = {
    "ThreatModelAgent": ("A.java", 1),
    "BehaviorAgent": ("B.java", 1),
    "MaintainabilityAgent": ("C.java", 1),
}

_FANIN_DIFF = "".join(
    f"diff --git a/{f} b/{f}\n--- a/{f}\n+++ b/{f}\n@@ -1 +1,2 @@\n+int x=1;\n"
    for f in ("A.java", "B.java", "C.java")
)


class _FakeEngine:
    def review(self, llm, *, system_prompt, user_prompt, reviewer_name, max_retries, structured_method, enable_hitl=False):
        file, line = _FAKE_TARGET.get(reviewer_name, ("A.java", 1))
        issue = Issue(
            severity=Severity.WARNING,
            file=file,
            line=line,
            type=reviewer_name,
            message="m",
        )
        gc = [GatheredContext("get_file_content", f"{reviewer_name}.java", "x")]
        return ReviewOutcome(ReviewResult(summary=f"sum-{reviewer_name}", issues=[issue]), gc)


def test_graph_fanin_three_discoverers(monkeypatch):
    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _FakeEngine())
    orch = PipelineOrchestrator(
        enable_summary=False,
        review_budget=ReviewBudget(small_max_files=0),
    )
    trace: list = []
    meta: dict = {}
    result = orch.run(
        _FakeLLM(),
        _FANIN_DIFF,
        fp_verify_llm=_CouncilLLM(),
        trace_sink=trace,
        metadata_sink=meta,
    )
    assert {i.type for i in result.issues} == {
        "ThreatModelAgent",
        "BehaviorAgent",
        "MaintainabilityAgent",
    }
    assert len(trace) == 3

    assert meta["council"]["candidate_count"] == 3
    assert meta["council"]["verdict_count"] == 3
    assert meta["council"]["candidate_count_by_agent"] == {
        "threat_model": 1,
        "behavior": 1,
        "maintainability": 1,
    }
    assert meta["council"]["judge_synthesis_failed_count"] == 0
    assert meta["council"]["severity_transitions"] == {"WARNING->INFO": 3}


def test_build_graph_default_nodes_are_adr032():
    graph = G.build_review_graph(enable_summary=False, llm=None)
    names = set(graph.get_graph().nodes)
    assert "context_provider" in names
    assert "discover_threat_model" in names
    assert "discover_behavior" in names
    assert "discover_maintainability" in names
    assert "council_coordinator" in names
    assert "council_judge" in names
    assert "evidence_strategist" in names
    assert "evidence_researcher" in names
    assert "impact_assessor" in names
    # 旧节点已删除
    assert "challenge_agent" not in names
    assert "self_checker" not in names
    assert "supervisor" not in names
    assert "aggregation" not in names
    assert "fp_filter" not in names


def test_checkpointer_factory_memory_creates_MemorySaver():
    from codeguard_agent.pipeline.orchestrator import _create_checkpointer
    from langgraph.checkpoint.memory import MemorySaver

    assert isinstance(_create_checkpointer("memory", ""), MemorySaver)


def test_checkpointer_factory_empty_returns_none():
    from codeguard_agent.pipeline.orchestrator import _create_checkpointer

    assert _create_checkpointer("", "") is None


def test_orchestrator_with_memory_checkpointer_produces_same_result():
    orch = PipelineOrchestrator(
        enable_summary=False,
        checkpoint_backend="memory",
        review_budget=ReviewBudget(small_max_files=0),
    )
    result = orch.run(None, _MOCK_DIFF, thread_id="adr032-same-result")
    assert len(result.issues) >= 1


def test_hitl_is_ignored_in_adr032_default_path():
    orch = PipelineOrchestrator(
        enable_summary=False,
        checkpoint_backend="memory",
        review_budget=ReviewBudget(small_max_files=0),
    )
    result = orch.run(None, _MOCK_DIFF, thread_id="hitl-ignored")
    assert len(result.issues) >= 1


# ── EvidenceAgent preferred_tools 路由测试 ──


class _MockToolResponse:
    """模拟 ToolResponse 信封。"""

    def __init__(self, success: bool, result: str = "", error: str = "") -> None:
        self.success = success
        self.result = result
        self.error = error

    def as_tool_output(self) -> str:
        if self.success:
            return self.result or ""
        return f"Error: {self.error or 'unknown error'}"


class _MockToolClient:
    """按工具名记录调用，返回预设结果。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def get_file_content(self, file_path: str = "") -> _MockToolResponse:
        self.calls.append(("get_file_content", {"file_path": file_path}))
        return _MockToolResponse(True, result=f"content of {file_path}")

    def find_sensitive_apis(self) -> _MockToolResponse:
        self.calls.append(("find_sensitive_apis", {}))
        return _MockToolResponse(
            True,
            result="| 🔴 HIGH | `Statement.execute` | A.java:12 | `sql` |",
        )

    def get_diff_ast(self, diff_text: str = "") -> _MockToolResponse:
        self.calls.append(("get_diff_ast", {"query": diff_text}))
        return _MockToolResponse(
            True,
            result=(
                "AST for: A.java\n"
                "  class: A\n"
                "    public void save(Order order) [L12-L18]\n"
            ),
        )

    def find_callers(self, query: str = "") -> _MockToolResponse:
        self.calls.append(("find_callers", {"query": query}))
        return _MockToolResponse(True, result=f"callers of {query}")

    def get_code_metrics(self, file_path: str = "") -> _MockToolResponse:
        self.calls.append(("get_code_metrics", {"file_path": file_path}))
        return _MockToolResponse(True, result=f"CC=12 LOC=200 for {file_path}")

    def resolve_change_context(self, changes) -> _MockToolResponse:
        self.calls.append(("resolve_change_context", {"changes": changes}))
        contexts = [
            {
                "file": change["file"],
                "file_id": f"file:{change['file']}",
                "symbol_id": f"java:{change['file']}#save()",
                "kind": "method",
                "start_line": min(change["lines"] or [1]),
                "end_line": max(change["lines"] or [1]),
                "signature": "void save()",
                "annotations": [],
                "control_flow": [],
                "resolution": "resolved",
            }
            for change in changes
        ]
        return _MockToolResponse(
            True,
            result=json.dumps(
                {
                    "status": "confirmed",
                    "coverage": "complete",
                    "contexts": contexts,
                    "limitations": [],
                }
            ),
        )


class _CountingToolClient:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def get_file_content(self, target):
        self.calls.append(("get_file_content", target))
        raise AssertionError("已处理请求不应再次调用工具")


def test_build_graph_has_task_prep_nodes():
    graph = G.build_review_graph(enable_summary=True, llm=None)
    names = set(graph.get_graph().nodes)
    assert {"diff_task_builder", "risk_triage", "task_rank", "review_coverage"} <= names


def test_task_prep_nodes_populate_state():
    from codeguard_agent.pipeline.risk import task_prep

    tasks = task_prep.build_tasks(_DIFF)
    assert [t.id for t in tasks] == ["A.java#h0"]
    priors = task_prep.triage_tasks(tasks).priors
    sel = task_prep.rank_tasks(tasks, priors, G.ReviewBudget())
    assert sel.selected_task_ids == ["A.java#h0"]


def test_review_coverage_node_assigns_selected_task_without_exclusive_risk_route():
    task = G.ReviewTask(id="Order.java#h0", file="Order.java", patch="+TokenBucket")
    profile = _prior(task.id, RiskTag.CONFIG_SECURITY, 2)
    out = G._review_coverage_node()(
        {
            "review_tasks": [task],
            "risk_priors": {task.id: profile},
            "task_selection": G.TaskSelection(selected_task_ids=[task.id]),
            "review_budget": G.ReviewBudget(max_react_assignments=1),
        }
    )

    assignments = out["review_coverage_plan"].tasks[0].assignments
    reviewers = {assignment.reviewer.value for assignment in assignments}
    assert "behavior" in reviewers
    assert "threat_model" in reviewers
    assert all(assignment.tier.value == "direct" for assignment in assignments)


def test_risk_triage_node_emits_prior_and_rule_failure_trace(monkeypatch):
    from codeguard_agent.pipeline.risk import task_prep
    from codeguard_agent.pipeline.risk.rules.catalog import (
        RuleDiagnostic,
        TriageResult,
    )

    profile = _prior("A.java#h0")
    monkeypatch.setattr(
        task_prep,
        "triage_tasks",
        lambda _tasks: TriageResult(
            priors={"A.java#h0": profile},
            diagnostics=(
                RuleDiagnostic(
                    task_id="A.java#h0", rule_id="broken", detail="detector error"
                ),
            ),
        ),
    )

    out = G._risk_triage_node()({"review_tasks": [G.ReviewTask(id="A.java#h0", file="A.java", patch="+x")]})

    assert out["risk_priors"] == {"A.java#h0": profile}
    assert [(trace.event, trace.detail) for trace in out["council_trace"]] == [
        ("priors_built", "priors=1"),
        ("rule_failed", "detector error"),
    ]


def test_review_state_has_task_chain_fields():
    ann = G.ReviewState.__annotations__
    for field in (
        "review_budget",
        "review_tasks",
        "risk_priors",
        "task_selection",
        "task_context_bundles",
    ):
        assert field in ann


def test_review_state_excludes_council_route():
    assert "council_route" not in G.ReviewState.__annotations__


def test_coordinator_edges_through_agentic_evidence_chain():
    graph = G.build_review_graph(enable_summary=False, llm=None)
    edges = graph.get_graph().edges
    pairs = {(e.source, e.target) for e in edges}
    # coordinator → concern → strategist → researcher → impact → judge
    assert ("council_coordinator", "concern_analyzer") in pairs
    assert ("concern_analyzer", "evidence_strategist") in pairs
    assert ("evidence_strategist", "evidence_researcher") in pairs
    assert ("evidence_researcher", "impact_assessor") in pairs
    assert ("impact_assessor", "council_judge") in pairs
    # 旧的 evidence → coordinator 回环已移除
    assert ("evidence_researcher", "council_coordinator") not in pairs
    # concern_analyzer 在非 discovery_only 图中存在
    assert "concern_analyzer" in set(graph.get_graph().nodes)


def test_evidence_agent_runs_once_before_judge(monkeypatch):
    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _FakeEngine())
    orch = PipelineOrchestrator(
        enable_summary=False,
        review_budget=ReviewBudget(small_max_files=0),
    )
    meta: dict = {}
    orch.run(_FakeLLM(), _DIFF, metadata_sink=meta)

    assert meta["council"]["verdict_count"] >= 1
    assert meta["council"]["investigation_plan_count"] >= 1
    assert "evidence_dossier_status_counts" in meta["council"]
    assert "evidence_react_candidate_count" in meta["council"]
    assert "evidence_rounds" not in meta["council"]


def test_make_reviewer_node_rejects_task_mismatch_candidate(monkeypatch):
    """候选文件不属于当前 task 时拒绝进入黑板并记录 trace。"""

    class _OutOfDiffEngine:
        def review(self, llm, *, system_prompt, user_prompt, reviewer_name,
                   max_retries, structured_method, enable_hitl=False):
            issue = Issue(
                severity=Severity.WARNING,
                file="NotInDiff.java",
                line=7,
                type="t",
                message="m",
            )
            return ReviewOutcome(ReviewResult(summary="s", issues=[issue]))

    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _OutOfDiffEngine())
    node = G.make_reviewer_node(G.DEFAULT_REVIEWERS[0], llm=_FakeLLM())
    out = node({
            "diff_text": "diff --git a/A.java b/A.java\n+++ b/A.java\n@@ -1 +1 @@\n+x",
            "review_tasks": [
                G.ReviewTask(id="A.java#h0", file="A.java", patch="", changed_lines=[1])
            ],
            "risk_priors": {
                "A.java#h0": _prior("A.java#h0", RiskTag.INJECTION, 2)
            },
            "task_selection": G.TaskSelection(selected_task_ids=["A.java#h0"]),
        })
    # 候选指向 NotInDiff.java，不在 review_tasks 中 → 被拒绝
    assert out["raw_candidate_issues"] == []
    events = {t.event for t in out["council_trace"]}
    assert "candidate_rejected_task_mismatch" in events


def test_make_reviewer_node_only_invokes_routed_and_selected_tasks(monkeypatch):
    """收集节点：未被 TaskRank 选中/未路由到本 reviewer 的 task 根本不会被调用。"""
    invoked_task_files = []

    class _RecordingEngine:
        def review(self, llm, *, system_prompt, user_prompt, reviewer_name,
                   max_retries, structured_method, enable_hitl=False):
            invoked_task_files.append(user_prompt)
            return ReviewOutcome(ReviewResult(summary="s"))

    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _RecordingEngine())
    node = G.make_reviewer_node(G.DEFAULT_REVIEWERS[0], llm=_FakeLLM())
    node({
        "diff_text": "+x\n+y",
        "review_tasks": [
            G.ReviewTask(id="A.java#h0", file="A.java", patch="+x", changed_lines=[1]),
            G.ReviewTask(id="B.java#h0", file="B.java", patch="+y", changed_lines=[1]),
        ],
        # 只选中 B.java#h0；A.java#h0 即使命中 ThreatModelAgent 的标签也不该被调用
        "task_selection": G.TaskSelection(selected_task_ids=["B.java#h0"]),
        "risk_priors": {
            "B.java#h0": _prior("B.java#h0", RiskTag.GENERAL_REVIEW, 1)
        },
    })
    assert len(invoked_task_files) == 1
    assert "+y" in invoked_task_files[0]
    assert "+x" not in invoked_task_files[0]


def test_make_reviewer_node_skips_reviewer_without_routed_tasks(monkeypatch):
    calls = 0

    class _FallbackRunsDirect:
        def review(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            return ReviewOutcome(ReviewResult(summary=""))

    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _FallbackRunsDirect())
    node = G.make_reviewer_node(G.DEFAULT_REVIEWERS[0], llm=_FakeLLM())
    task = G.ReviewTask(id="A.java#h0", file="A.java", patch="+query", changed_lines=[1])
    out = node(
        {
            "diff_text": "+query",
            "review_tasks": [task],
            "risk_priors": {
                task.id: _prior(task.id, RiskTag.SQL_DATA_ACCESS, 1)
            },
            "task_selection": G.TaskSelection(selected_task_ids=[task.id]),

        }
    )

    assert calls == 1
    assert out["raw_candidate_issues"] == []
    assert any(trace.event == "discover_done" for trace in out["council_trace"])


def test_make_reviewer_node_rejects_candidate_with_mismatched_file(monkeypatch):
    """收集节点：某 task 调用返回的 issue.file 和被调用 task 的 file 对不上 → 拒绝。"""

    class _WrongFileEngine:
        def review(self, llm, *, system_prompt, user_prompt, reviewer_name,
                   max_retries, structured_method, enable_hitl=False):
            issue = Issue(
                severity=Severity.WARNING, file="Unrelated.java", line=1,
                type="t", message="m",
            )
            return ReviewOutcome(ReviewResult(summary="s", issues=[issue]))

    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _WrongFileEngine())
    node = G.make_reviewer_node(G.DEFAULT_REVIEWERS[1], llm=_FakeLLM())
    task = G.ReviewTask(id="A.java#h0", file="A.java", patch="+sql", changed_lines=[1])
    out = node({
        "diff_text": "+sql",
        "review_tasks": [task],
        "risk_priors": {
            task.id: _prior(task.id, RiskTag.SQL_DATA_ACCESS, 1)
        },
        "task_selection": G.TaskSelection(selected_task_ids=[task.id]),
    })
    assert out["raw_candidate_issues"] == []
    events = {t.event for t in out["council_trace"]}
    assert "candidate_rejected_task_mismatch" in events


def test_make_reviewer_node_invokes_tasks_concurrently_with_correct_tier(monkeypatch):
    seen_tiers = {}

    def _fake_subgraph_invoke(payload, config=None):
        # 实现按 task 并发派发时，每次 invoke 都显式带了 config/thread_id（见
        # graph.py 里的注释：跨线程调用需要避开 checkpointer 对 ambient config 的依赖），
        # 这里的 fake 必须接受该形参，否则会被当成签名不匹配的失败。
        seen_tiers[payload["diff_text"]] = payload.get("tier")
        return {"issues": [], "council_trace": []}

    import types

    fake_subgraph = types.SimpleNamespace(invoke=_fake_subgraph_invoke)
    monkeypatch.setattr(
        G, "build_reviewer_subgraph", lambda *a, **k: fake_subgraph
    )
    node = G.make_reviewer_node(
        G.DEFAULT_REVIEWERS[1], llm=_FakeLLM(), tool_client=object()
    )
    tasks = [
        G.ReviewTask(id="A.java#h0", file="A.java", patch="+strong", changed_lines=[1]),
        G.ReviewTask(id="B.java#h0", file="B.java", patch="+weak", changed_lines=[1]),
    ]
    priors = {
        "A.java#h0": _prior("A.java#h0", RiskTag.CONCURRENCY_CONSISTENCY, 3),
        "B.java#h0": _prior("B.java#h0", RiskTag.SQL_DATA_ACCESS, 1),
    }
    selection = G.TaskSelection(
        selected_task_ids=["A.java#h0", "B.java#h0"]
    )
    coverage = G.plan_review_coverage(
        tasks,
        priors,
        selection,
        react_budget=1,
        tools_available=True,
    )
    out = node({
        "diff_text": "+strong\n+weak",
        "review_tasks": tasks,
        "risk_priors": priors,
        "review_coverage_plan": coverage,
        "task_selection": selection,
    })
    assert seen_tiers["+strong"] == "react"
    assert seen_tiers["+weak"] == "direct"
    tier_details = {
        trace.detail
        for trace in out["council_trace"]
        if trace.event == "task_tier_planned"
    }
    assert tier_details == {
        "task=A.java#h0 tier=react",
        "task=B.java#h0 tier=direct",
    }


def test_make_reviewer_node_without_tools_forces_strong_task_to_direct(monkeypatch):
    seen_tiers: list[str] = []

    def _fake_subgraph_invoke(payload, config=None):
        seen_tiers.append(payload.get("tier"))
        return {"issues": [], "council_trace": []}

    import types

    fake_subgraph = types.SimpleNamespace(invoke=_fake_subgraph_invoke)
    monkeypatch.setattr(G, "build_reviewer_subgraph", lambda *a, **k: fake_subgraph)
    task = G.ReviewTask(id="A.java#h0", file="A.java", patch="+strong")

    G.make_reviewer_node(G.DEFAULT_REVIEWERS[1], llm=_FakeLLM())(
        {
            "diff_text": task.patch,
            "review_tasks": [task],
            "risk_priors": {
                task.id: _prior(task.id, RiskTag.CONCURRENCY_CONSISTENCY, 3)
            },
            "task_selection": G.TaskSelection(selected_task_ids=[task.id]),
        }
    )

    assert seen_tiers == ["direct"]


def test_make_reviewer_node_injects_selected_knowledge_bundle_into_user_prompt(monkeypatch):
    captured: dict[str, str] = {}

    class _CapturingEngine:
        def review(self, llm, *, system_prompt, user_prompt, reviewer_name,
                   max_retries, structured_method, enable_hitl=False):
            captured["system_prompt"] = system_prompt
            captured["user_prompt"] = user_prompt
            return ReviewOutcome(ReviewResult(summary="s"))

    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _CapturingEngine())
    monkeypatch.setattr(
        G, "select_knowledge",
        lambda **kwargs: KnowledgeBundle(
            task_id=kwargs["task"].id,
            reviewer=kwargs["reviewer"],
            base=None,
            specialized=(),
            rendered_text="KNOWLEDGE_MARKER",
            truncated=False,
            omitted_topics=(),
            diagnostics=(),
        ),
    )
    node = G.make_reviewer_node(G.DEFAULT_REVIEWERS[1], llm=_FakeLLM())

    task = G.ReviewTask(id="A.java#h0", file="A.java", patch="+lock", changed_lines=[1])

    node({
        "diff_text": "+lock",
        "review_tasks": [task],
        "risk_priors": {
            task.id: _prior(task.id, RiskTag.CONCURRENCY_CONSISTENCY, 1)
        },
        "task_selection": G.TaskSelection(selected_task_ids=[task.id]),
    })

    assert "KNOWLEDGE_MARKER" not in captured["system_prompt"]
    assert "KNOWLEDGE_MARKER" in captured["user_prompt"]
    assert '<knowledge_bundle role="methodology_not_repository_fact">' in captured[
        "user_prompt"
    ]
    assert captured["user_prompt"].count("+lock") == 1
    assert '<risk_prior role="routing_prior_not_evidence"' in captured["user_prompt"]


def test_make_reviewer_node_fanout_survives_real_memory_checkpointer(monkeypatch):
    """回归钉子：per-task fan-out 必须显式传 config/thread_id，否则线程池里的
    subgraph.invoke() 在真实 MemorySaver checkpointer 下会因缺 thread_id 抛
    ValueError，被 run_bounded_parallel 吞成 None → task_review_failed。
    这里用真实 MemorySaver（不 mock），llm 不为 None（避免 _prepare/_review 因
    llm is None 短路，从而绕过 tier/engine 选择逻辑），2 个 task 路由到同一
    reviewer，真正走 selection is not None 的并发派发分支。
    """
    from langgraph.checkpoint.memory import MemorySaver

    invoked_files: list[str] = []

    class _RecordingEngine:
        def review(self, llm, *, system_prompt, user_prompt, reviewer_name,
                   max_retries, structured_method, enable_hitl=False):
            import re

            match = re.search(r'file="([^"]+)"', user_prompt)
            file = match.group(1) if match else "unknown"
            invoked_files.append(file)
            issue = Issue(severity=Severity.WARNING, file=file, line=1, type="t", message="m")
            return ReviewOutcome(ReviewResult(summary="s", issues=[issue]))

    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _RecordingEngine())
    node = G.make_reviewer_node(
        G.DEFAULT_REVIEWERS[1], checkpointer=MemorySaver(), llm=_FakeLLM(),
    )
    tasks = [
        G.ReviewTask(id="A.java#h0", file="A.java", patch="+sql", changed_lines=[1]),
        G.ReviewTask(id="B.java#h0", file="B.java", patch="+auth", changed_lines=[1]),
    ]
    out = node({
        "diff_text": "+sql\n+auth",
        "review_tasks": tasks,
        "risk_priors": {
            "A.java#h0": _prior("A.java#h0", RiskTag.SQL_DATA_ACCESS, 1),
            "B.java#h0": _prior("B.java#h0", RiskTag.CONCURRENCY_CONSISTENCY, 2),
        },
        "task_selection": G.TaskSelection(selected_task_ids=["A.java#h0", "B.java#h0"]),
    })

    # 两个 task 都必须真正被调用到（证明 fan-out 分支在跑，而不是短路成兼容路径）。
    assert set(invoked_files) == {"A.java", "B.java"}
    # 去掉 uuid.uuid4() 的 config/thread_id 后，两次 invoke 都会在工作线程里因
    # MemorySaver 缺 thread_id 抛异常，被 run_bounded_parallel 吞成 None，
    # 产出 task_review_failed 而不是候选——这条断言就是钉住这个修复的关键。
    events = [t.event for t in out["council_trace"]]
    assert "task_review_failed" not in events
    assert {c.file for c in out["raw_candidate_issues"]} == {"A.java", "B.java"}


def test_context_provider_node_fills_symbol_context_per_task():
    task = G.ReviewTask(
        id="A.java#h0",
        file="A.java",
        hunk_header="@@ -12,2 +12,2 @@",
        patch="+x",
        changed_lines=[12],
    )
    tool_client = _MockToolClient()

    out = G._context_provider_node(tool_client)(
        {
            "diff_text": (
                "diff --git a/A.java b/A.java\n--- a/A.java\n+++ b/A.java\n"
                "@@ -12,2 +12,2 @@\n+order.save();\n"
            ),
            "review_tasks": [task],
            "risk_priors": {
                task.id: _prior(task.id, RiskTag.RESOURCE_LIFECYCLE, 2)
            },
            "task_selection": G.TaskSelection(selected_task_ids=[task.id]),
            "review_budget": G.ReviewBudget(),
        }
    )


    bundle = out["task_context_bundles"][task.id]
    assert {fact.source for fact in bundle.facts} == {
        "tool:resolve_change_context",
    }
    assert not any(name == "find_callers" for name, _ in tool_client.calls)
    assert any(
        trace.event == "task_bundle_filled" and f"task={task.id}" in trace.detail
        for trace in out["council_trace"]
    )


def test_context_provider_node_records_skip_when_method_unresolved():
    task = G.ReviewTask(
        id="B.java#h0",
        file="B.java",
        hunk_header="@@ -1,1 +1,1 @@",
        patch="+x",
        changed_lines=[1],
    )
    class _UnresolvedClient(_MockToolClient):
        def resolve_change_context(self, changes):
            self.calls.append(("resolve_change_context", {"changes": changes}))
            return _MockToolResponse(
                True,
                result=json.dumps(
                    {
                        "status": "confirmed",
                        "coverage": "complete",
                        "contexts": [],
                        "limitations": [],
                    }
                ),
            )

    tool_client = _UnresolvedClient()

    out = G._context_provider_node(tool_client)(
        {
            "diff_text": "diff --git a/B.java b/B.java\n--- a/B.java\n+++ b/B.java\n@@ -1 +1 @@\n+x\n",
            "review_tasks": [task],
            "risk_priors": {
                task.id: _prior(task.id, RiskTag.API_CONTRACT, 2)
            },
            "task_selection": G.TaskSelection(selected_task_ids=[task.id]),
            "review_budget": G.ReviewBudget(),
        }
    )

    assert not any(call[0] == "find_callers" for call in tool_client.calls)
    statuses = out["task_context_bundles"][task.id].statuses
    assert any(
        status.kind == "symbol_context"
        and status.status == "unavailable"
        and status.reason == "no_resolved_symbol_for_current_hunk"
        for status in statuses
    )
    assert any("symbol_context=False" in trace.detail for trace in out["council_trace"])


def test_context_provider_node_general_review_gets_no_level1_call():
    task = G.ReviewTask(
        id="C.java#h0",
        file="C.java",
        hunk_header="@@ -1,1 +1,1 @@",
        patch="+x",
        changed_lines=[1],
    )
    tool_client = _MockToolClient()

    G._context_provider_node(tool_client)(
        {
            "diff_text": "diff --git a/C.java b/C.java\n--- a/C.java\n+++ b/C.java\n@@ -1 +1 @@\n+x\n",
            "review_tasks": [task],
            "risk_priors": {
                task.id: _prior(task.id, RiskTag.GENERAL_REVIEW, 1)
            },
            "task_selection": G.TaskSelection(selected_task_ids=[task.id]),
            "review_budget": G.ReviewBudget(),
        }
    )

    assert not any(call[0] in ("find_callers", "get_code_metrics") for call in tool_client.calls)


def test_context_provider_node_does_not_store_failed_graph_response_as_fact():
    class _FailingGraphClient(_MockToolClient):
        def resolve_change_context(self, changes) -> _MockToolResponse:
            self.calls.append(("resolve_change_context", {"changes": changes}))
            return _MockToolResponse(False, error="gateway timeout")

    task = G.ReviewTask(
        id="A.java#h0",
        file="A.java",
        hunk_header="@@ -12,1 +12,1 @@",
        patch="+x",
        changed_lines=[12],
    )
    out = G._context_provider_node(_FailingGraphClient())(
        {
            "diff_text": "diff --git a/A.java b/A.java\n+++ b/A.java\n@@ -12 +12 @@\n+x\n",
            "review_tasks": [task],
            "risk_priors": {
                task.id: _prior(task.id, RiskTag.RESOURCE_LIFECYCLE, 2)
            },
            "task_selection": G.TaskSelection(selected_task_ids=[task.id]),
            "review_budget": G.ReviewBudget(),
        }
    )

    facts = out["task_context_bundles"][task.id].facts
    assert all("gateway timeout" not in fact.content for fact in facts)
    statuses = out["task_context_bundles"][task.id].statuses
    assert any(
        status.kind == "symbol_context"
        and status.status == "failed"
        and "gateway timeout" in status.reason
        for status in statuses
    )

    assert any("gateway timeout" in trace.detail for trace in out["council_trace"])


# ── Task 3: 协调器接入测试 ──


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
        "risk_priors": {
            task.id: _prior(task.id, RiskTag.NULL_STATE_SAFETY, 2)
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
    one_task["risk_priors"] = {first_id: one_task["risk_priors"][first_id]}
    one_task["task_selection"] = G.TaskSelection(selected_task_ids=[first_id])
    node(one_task)
    node(one_task)

    assert raw_client.calls == 2


def test_make_reviewer_node_blocks_current_file_read_for_complete_new_file(monkeypatch):
    raw_client = _CountingFileClient()
    returned: list[str] = []

    def _invoke(payload, config=None):  # noqa: ARG001
        response = payload["review_tool_client"].get_file_content("A.java")
        returned.append(response.result or "")
        return {"issues": [], "council_trace": []}

    import types

    monkeypatch.setattr(
        G,
        "build_reviewer_subgraph",
        lambda *args, **kwargs: types.SimpleNamespace(invoke=_invoke),
    )
    task = G.ReviewTask(
        id="A.java#h0",
        file="A.java",
        hunk_header="@@ -0,0 +1,2 @@",
        patch="+class A {}",
        changed_lines=[1],
    )
    node = G.make_reviewer_node(
        G.DEFAULT_REVIEWERS[1], llm=_FakeLLM(), tool_client=raw_client
    )

    node(
        {
            "review_tasks": [task],
            "risk_priors": {
                task.id: _prior(task.id, RiskTag.NULL_STATE_SAFETY, 2)
            },
            "task_selection": G.TaskSelection(selected_task_ids=[task.id]),
        }
    )

    assert raw_client.calls == 0
    assert returned == [
        "当前 task patch 已包含该新增文件的完整内容；请直接复用 patch，不要重复读取。"
    ]


# ── 跨维度去重守卫测试 ──


def _c(
    agent: str,
    fid: str,
    file: str,
    line: int,
    typ: str,
    claim: str = "test claim",
) -> G.CandidateIssue:
    return G.CandidateIssue(
        id=f"{agent}-{fid}",
        task_id=f"{file}#h0",
        source_agent=agent,
        file=file,
        line=line,
        type=typ,
        severity_proposal="WARNING",
        claim=claim,
    )


class TestCandidateCollectReducer:
    """验证 collect_candidate_reducer 仅按 ID 去重，不做语义合并。"""

    def test_candidate_reducer_only_removes_identical_ids(self):
        first = _c("behavior", "1", "OrderService.java", 30, "ERROR_HANDLING",
                   "payment failure")
        same_id = first.model_copy(update={"claim": "conflicting duplicate payload"})
        distinct = _c("behavior", "2", "OrderService.java", 30, "ERROR_HANDLING",
                      "audit failure")

        result = G.collect_candidate_reducer([first], [same_id, distinct])

        assert [candidate.id for candidate in result] == [first.id, distinct.id]
        assert result[0].claim == "payment failure"

    def test_candidate_reducer_keeps_adjacent_same_type_candidates(self):
        first = _c("behavior", "1", "OrderService.java", 30, "ERROR_HANDLING")
        second = _c("behavior", "2", "OrderService.java", 32, "ERROR_HANDLING")
        assert G.collect_candidate_reducer([first], [second]) == [first, second]


def test_coordinator_batches_tag_resolution_and_emits_complete_trace(monkeypatch):
    from codeguard_agent.pipeline.council.dedup import (
        CandidateGroup,
        CandidateBlockFailure,
        CandidateDedupResult,
    )

    first = _c("behavior", "1", "OrderService.java", 30, "ERROR_HANDLING")
    second = _c("threat_model", "2", "OrderService.java", 31, "错误处理")
    second = second.model_copy(update={"task_id": first.task_id})
    task = G.ReviewTask(
        id=first.task_id,
        file=first.file,
        patch="+ riskyCall();",
        changed_lines=[30, 31],
    )
    resolution = G.CandidateTagResolution(
        tag=RiskTag.ERROR_HANDLING,
        confidence=0.95,
        source="rule",
        reason="test",
    )
    calls: list[list[str]] = []

    def resolve(dossiers, **kwargs):
        calls.append([dossier.candidate.id for dossier in dossiers])
        assert "max_workers" not in kwargs
        return {dossier.candidate.id: resolution for dossier in dossiers}

    monkeypatch.setattr(G, "resolve_candidate_tags", resolve)
    monkeypatch.setattr(
        G,
        "deduplicate_candidates",
        lambda candidates, **kwargs: CandidateDedupResult(
            candidates=(first, second),
            raw_candidate_count=2,
            block_count=1,
            multi_member_block_count=1,
            llm_call_count=1,
            accepted_groups=(
                CandidateGroup(
                    id="candidate-group-test",
                    members=(first, second),
                    primary_risk_tag=RiskTag.ERROR_HANDLING,
                    severity_proposal=first.severity_proposal,
                    confidence=0.99,
                    shared_root_cause="same defect",
                    shared_behavior="same behavior",
                    shared_fix="same fix",
                ),
            ),
            rejected_groups=(),
            block_failures=(
                CandidateBlockFailure("block-1", "empty_response"),
            ),
        ),
    )

    output = G._coordinator_node(object())(
        {
            "raw_candidate_issues": [first, second],
            "review_tasks": [task],
            "risk_priors": {},
            "task_context_bundles": {},
            "structured_method": "function_calling",
        }
    )

    assert calls == [[first.id, second.id]]
    assert output["candidate_issues"] == [first, second]
    assert output["candidate_groups"][0].members == (first, second)
    assert output["candidate_dedup_stats"] == {
        "raw_candidate_count": 2,
        "logical_candidate_count": 1,
        "grouped_member_count": 1,
        "removed_count": 0,
        "llm_call_count": 1,
        "block_failure_count": 1,
    }
    traces = {trace.event: trace.detail for trace in output["council_trace"]}
    assert "rule=2" in traces["candidate_tags_resolved"]
    assert "singleton=0" in traces["candidate_dedup_blocks_built"]
    assert f"members=['{first.id}', '{second.id}']" in traces["candidate_dedup_group_accepted"]
    assert "root_cause=same defect" in traces["candidate_dedup_group_accepted"]
    assert "reason=empty_response" in traces["candidate_dedup_block_failed"]
    assert "logical=1" in traces["candidate_dedup_completed"]
    assert "grouped=1" in traces["candidate_dedup_completed"]


def test_coordinator_scopes_large_diff_patch_before_classification_and_dedup(
    monkeypatch,
):
    from codeguard_agent.pipeline.council.dedup import CandidateDedupResult

    candidate = _c("behavior", "1", "OrderService.java", 30, "ERROR_HANDLING")
    original_patch = "+" + ("x" * 13_000)
    task = G.ReviewTask(
        id=candidate.task_id,
        file=candidate.file,
        patch=original_patch,
        patch_complete=True,
        changed_lines=[30],
    )
    captured: dict[str, object] = {}
    resolution = G.CandidateTagResolution(
        tag=RiskTag.ERROR_HANDLING,
        confidence=0.95,
        source="rule",
        reason="test",
    )

    def resolve(dossiers, **kwargs):
        captured["dossier_task"] = dossiers[0].task
        return {candidate.id: resolution}

    def dedup(candidates, **kwargs):
        captured["dedup_task"] = kwargs["tasks_by_id"][candidate.task_id]
        return CandidateDedupResult(
            candidates=(candidate,),
            raw_candidate_count=1,
            block_count=1,
            multi_member_block_count=0,
            llm_call_count=0,
            accepted_groups=(),
            rejected_groups=(),
            block_failures=(),
        )

    monkeypatch.setattr(G, "resolve_candidate_tags", resolve)
    monkeypatch.setattr(G, "deduplicate_candidates", dedup)

    G._coordinator_node(object())(
        {
            "diff_text": "\n".join("+ changed" for _ in range(5001)),
            "raw_candidate_issues": [candidate],
            "review_tasks": [task],
            "risk_priors": {},
            "task_context_bundles": {},
            "review_budget": G.ReviewBudget(),
        }
    )

    for scoped_task in (
        captured["dossier_task"],
        captured["dedup_task"],
    ):
        assert scoped_task.patch != original_patch
        assert scoped_task.patch.endswith("...(大 diff 单任务 patch 已截断)")
        assert scoped_task.patch_complete is False
