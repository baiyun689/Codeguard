"""ADR-032 ReviewCouncil 编排测试。"""

from __future__ import annotations

from codeguard_agent.models.council import ContextBundle, ContextFact, EvidenceNote, Verdict
from codeguard_agent.models.schemas import Issue, ReviewResult, Severity
from codeguard_agent.pipeline import graph as G
from codeguard_agent.pipeline.engines import GatheredContext, ReviewOutcome
from codeguard_agent.pipeline.orchestrator import PipelineOrchestrator
from codeguard_agent.pipeline.stages.base import PipelineContext
from codeguard_agent.pipeline.stages.context_provider import ContextProviderStage


def test_dedup_gathered_reducer_dedups_by_tool_args_keep_order():
    a = GatheredContext("get_file_content", "A.java", "x")
    b = GatheredContext("get_file_content", "B.java", "y")
    a_dup = GatheredContext("get_file_content", "A.java", "x-again")
    out = G.dedup_gathered_reducer([a], [b, a_dup])
    assert [g.args for g in out] == ["A.java", "B.java"]


def test_context_bundle_render_truncates():
    bundle = ContextBundle(
        changed_files=["A.java"],
        diff_summary="摘要",
        facts=[ContextFact(source="diff", kind="changed_file", content="A.java")],
    )
    rendered = bundle.render(20)
    assert "A.java" in rendered
    assert "ContextBundle 已达预算上限" in rendered


def test_context_provider_builds_fact_bundle_without_judgement():
    diff = "diff --git a/A.java b/A.java\n--- a/A.java\n+++ b/A.java\n@@ -1 +1,2 @@\n+int x=1;\n"
    ctx = PipelineContext(diff_text=diff, diff_summary="新增字段")
    ContextProviderStage().execute(ctx)
    assert ctx.context_bundle.changed_files == ["A.java"]
    assert "diff" in ctx.context_bundle.sources
    text = ctx.context_bundle.render()
    assert "新增字段" in text
    assert "漏洞" not in text


def _base_state(**over):
    state = {
        "candidate_issues": [],
        "evidence_notes": [],
        "challenges": [],
        "council_verdicts": [],
        "evidence_round": 0,
        "judge_pass": 0,
        "max_evidence_rounds": 2,
    }
    state.update(over)
    return state


def _candidate(*, needs_evidence=False, confidence=0.9):
    issue = Issue(
        severity=Severity.WARNING,
        file="A.java",
        line=1,
        type="t",
        message="m",
        confidence=confidence,
    )
    candidate = G.CandidateIssue.from_issue(issue, agent="security", index=1)
    candidate.needs_evidence = needs_evidence
    return candidate


def test_coordinator_skips_evidence_and_judge_when_no_candidates():
    """无候选 → 直接进 council_judge。"""
    assert G._route_after_coordinator(_base_state()) == "council_judge"


def test_coordinator_routes_to_evidence_on_first_round():
    """第一轮有 evidence_requests → 进 evidence_agent。"""
    c = _candidate(needs_evidence=True)
    req = G.EvidenceRequest(candidate_id=c.id, target="A.java", preferred_tools=["get_file_content"])
    assert G._route_after_coordinator(_base_state(candidate_issues=[c], evidence_requests=[req])) == "evidence_agent"


def test_coordinator_routes_to_council_judge_when_no_evidence_needed():
    """无证据请求 → 直接进 council_judge。"""
    c = _candidate()
    assert G._route_after_coordinator(_base_state(candidate_issues=[c])) == "council_judge"


def test_coordinator_after_evidence_round_goes_to_council_judge():
    """evidence_round>0 时不再自动进 evidence_agent。"""
    c = _candidate(needs_evidence=True)
    assert G._route_after_coordinator(_base_state(candidate_issues=[c], evidence_round=1)) == "council_judge"


def test_route_after_council_judge_needs_more_loop():
    """council_judge 有 needs_more_evidence + 轮次未超 → evidence_agent。"""
    v = Verdict(candidate_id="c1", action="needs_more_evidence", reason_code="test")
    assert G._route_after_council_judge(_base_state(council_verdicts=[v], evidence_round=1)) == "evidence_agent"


def test_route_after_council_judge_exhausted_rounds():
    """轮次耗尽 → END。"""
    v = Verdict(candidate_id="c1", action="needs_more_evidence", reason_code="test")
    assert G._route_after_council_judge(_base_state(council_verdicts=[v], evidence_round=2)) == "END"


def test_route_after_council_judge_no_needs_more():
    """没有 needs_more_evidence → END。"""
    v = Verdict(candidate_id="c1", action="keep", reason_code="test")
    assert G._route_after_council_judge(_base_state(council_verdicts=[v])) == "END"


def test_reviewer_subgraph_exposes_internal_nodes():
    sub = G.build_reviewer_subgraph(G.DEFAULT_REVIEWERS[0])
    assert {"prepare", "review", "collect"} <= set(sub.get_graph().nodes)


def test_default_discoverers_are_methodology_roles_with_tool_boundaries():
    by_source = {r.source_agent: r for r in G.DEFAULT_REVIEWERS}
    assert set(by_source) == {"threat_model", "behavior", "maintainability"}
    assert by_source["threat_model"].name == "ThreatModelAgent"
    assert by_source["threat_model"].category == "security"
    assert by_source["threat_model"].tool_allowlist == [
        "get_file_content",
        "find_sensitive_apis",
    ]
    assert by_source["behavior"].name == "BehaviorAgent"
    assert by_source["behavior"].category == "logic"
    assert by_source["behavior"].tool_allowlist == ["get_file_content", "find_callers"]
    assert by_source["maintainability"].name == "MaintainabilityAgent"
    assert by_source["maintainability"].category == "quality"
    assert by_source["maintainability"].tool_allowlist == [
        "get_file_content",
        "get_code_metrics",
    ]


def test_reviewer_subgraph_mock_only_threat_model_returns_issues():
    sec = G.build_reviewer_subgraph(
        G.Reviewer(
            "ThreatModelAgent",
            "threat-model.txt",
            source_agent="threat_model",
            category="security",
        ),
        llm=None,
    )
    other = G.build_reviewer_subgraph(
        G.Reviewer(
            "BehaviorAgent",
            "behavior.txt",
            source_agent="behavior",
            category="logic",
        ),
        llm=None,
    )
    assert len(sec.invoke({})["issues"]) >= 1
    assert other.invoke({}).get("issues", []) == []


def test_council_judge_rule_invalid_file_drop():
    """文件路径为空 → drop。"""
    c = G.CandidateIssue(
        id="c1", source_agent="threat_model", file="", line=1, type="t",
        severity_proposal=Severity.WARNING, claim="m",
    )
    node = G._council_judge_node(llm=None)
    out = node({"candidate_issues": [c], "evidence_notes": [], "evidence_requests": [], "review_summaries": []})
    assert len(out["final_issues"]) == 0
    assert out["council_verdicts"][0].action == "drop"


def test_council_judge_rule_contradicted_drop():
    """有 contradicts + 低置信度 → drop。"""
    c = G.CandidateIssue(
        id="c1", source_agent="threat_model", file="A.java", line=1, type="t",
        severity_proposal=Severity.WARNING, claim="m", confidence=0.3,
    )
    notes = [EvidenceNote(candidate_id="c1", contradicts=["反证:已有校验"])]
    node = G._council_judge_node(llm=None)
    out = node({
        "candidate_issues": [c], "evidence_notes": notes,
        "evidence_requests": [], "review_summaries": [],
    })
    assert len(out["final_issues"]) == 0
    assert out["council_verdicts"][0].action == "drop"


def test_council_judge_rule_no_evidence_drop():
    """全部 not_found + 低置信度 → drop。"""
    c = G.CandidateIssue(
        id="c1", source_agent="threat_model", file="A.java", line=1, type="t",
        severity_proposal=Severity.WARNING, claim="m", confidence=0.3,
    )
    notes = [EvidenceNote(candidate_id="c1", status="not_found", unknowns=["x"])]
    node = G._council_judge_node(llm=None)
    out = node({
        "candidate_issues": [c], "evidence_notes": notes,
        "evidence_requests": [], "review_summaries": [],
    })
    assert len(out["final_issues"]) == 0


def test_council_judge_rule_quality_no_metrics_drop():
    """quality 类 + 无度量证据 + missing → drop。"""
    c = G.CandidateIssue(
        id="c1", source_agent="maintainability", category="quality", file="A.java",
        line=1, type="t", severity_proposal=Severity.WARNING, claim="m",
        evidence_status="missing",
    )
    node = G._council_judge_node(llm=None)
    out = node({
        "candidate_issues": [c], "evidence_notes": [],
        "evidence_requests": [], "review_summaries": [],
    })
    assert len(out["final_issues"]) == 0
    assert out["council_verdicts"][0].action == "drop"


def test_council_judge_rule_guard_detected_downgrade():
    """evidence 检测到 sanitize → downgrade。"""
    c = G.CandidateIssue(
        id="c1", source_agent="threat_model", file="A.java", line=1, type="t",
        severity_proposal=Severity.WARNING, claim="m", confidence=0.9,
    )
    notes = [EvidenceNote(candidate_id="c1", supports=["get_file_content: 代码含 sanitize 调用"])]
    node = G._council_judge_node(llm=None)
    out = node({
        "candidate_issues": [c], "evidence_notes": notes,
        "evidence_requests": [], "review_summaries": [],
    })
    assert out["council_verdicts"][0].action == "downgrade"
    assert out["council_verdicts"][0].severity_override is not None


def test_council_judge_rule_critical_partial_downgrade():
    """CRITICAL + evidence 不足 → downgrade。"""
    c = G.CandidateIssue(
        id="c1", source_agent="threat_model", file="A.java", line=1, type="t",
        severity_proposal=Severity.CRITICAL, claim="m", confidence=0.9,
    )
    notes = [EvidenceNote(candidate_id="c1", status="not_found", unknowns=["x"])]
    node = G._council_judge_node(llm=None)
    out = node({
        "candidate_issues": [c], "evidence_notes": notes,
        "evidence_requests": [], "review_summaries": [],
    })
    assert out["council_verdicts"][0].action == "downgrade"
    assert out["council_verdicts"][0].severity_override == Severity.WARNING


def test_council_judge_aggregation_dedup_same_file_line_type():
    """两段式去重：同文件同类型同行号 → 规则指纹去重，只保留一条。"""
    c1 = G.CandidateIssue(
        id="c1", source_agent="threat_model", file="A.java", line=10, type="sql_injection",
        severity_proposal=Severity.WARNING, claim="可能注入",
    )
    c2 = G.CandidateIssue(
        id="c2", source_agent="behavior", file="A.java", line=10, type="sql_injection",
        severity_proposal=Severity.WARNING, claim="拼接SQL",
    )
    node = G._council_judge_node(llm=None)
    out = node({
        "candidate_issues": [c1, c2], "evidence_notes": [],
        "evidence_requests": [], "review_summaries": [],
    })
    # 规则指纹去重（同文件+同行号+同类型）→ 只保留一条
    assert len(out["final_issues"]) == 1
    # 被合并的 candidate 产生 merge verdict
    actions = {v.action for v in out["council_verdicts"]}
    assert "merge" in actions


def test_council_judge_aggregation_keeps_different_lines():
    """不同行号 → 规则指纹去重不触发，两条都保留（llm=None 时）。"""
    c1 = G.CandidateIssue(
        id="c1", source_agent="threat_model", file="A.java", line=10, type="sql_injection",
        severity_proposal=Severity.WARNING, claim="可能注入",
    )
    c2 = G.CandidateIssue(
        id="c2", source_agent="behavior", file="A.java", line=42, type="sql_injection",
        severity_proposal=Severity.WARNING, claim="拼接SQL",
    )
    node = G._council_judge_node(llm=None)
    out = node({
        "candidate_issues": [c1, c2], "evidence_notes": [],
        "evidence_requests": [], "review_summaries": [],
    })
    # 行号不同 → 规则去重不合并，两条保留
    assert len(out["final_issues"]) == 2


def test_council_judge_rule_miss_conservative_keep():
    """规则全不命中 + llm=None → 保守 keep。"""
    c = G.CandidateIssue(
        id="c1", source_agent="threat_model", file="A.java", line=42, type="unusual_pattern",
        severity_proposal=Severity.WARNING, claim="异常模式", confidence=0.85,
    )
    node = G._council_judge_node(llm=None)
    out = node({
        "candidate_issues": [c], "evidence_notes": [],
        "evidence_requests": [], "review_summaries": [],
    })
    assert len(out["final_issues"]) == 1
    assert out["council_verdicts"][0].action == "keep"


def test_council_judge_needs_more_evidence_generates_request():
    """LLM 判 needs_more_evidence → 追加 EvidenceRequest 到 state。"""
    c = G.CandidateIssue(
        id="c1", source_agent="threat_model", file="A.java", line=1, type="t",
        severity_proposal=Severity.WARNING, claim="m", confidence=0.9,
    )

    class _JudgeLLM:
        def with_structured_output(self, *a, **k):
            class _Invoker:
                def invoke(self, _msgs):
                    return G.JudgeDecisions(decisions=[
                        G.JudgeDecision(
                            candidate_id="c1",
                            action="needs_more_evidence",
                            reason="证据不足，需要更多工具调用",
                        )
                    ])
            return _Invoker()

    node = G._council_judge_node(llm=_JudgeLLM())
    out = node({
        "candidate_issues": [c],
        "evidence_notes": [],
        "evidence_requests": [],
        "review_summaries": [],
    })
    # needs_more_evidence → 产生新 EvidenceRequest
    assert len(out.get("evidence_requests", [])) >= 1
    # 候选保留在 final_issues 中（needs_more 不 drop）
    assert len(out["final_issues"]) >= 1


def test_run_routes_gathered_context_to_trace_sink_and_council_metadata(monkeypatch):
    gc = [GatheredContext("get_file_content", "X.java", "body")]
    issues = [Issue(severity=Severity.WARNING, file="X.java", line=1, type="t", message="m")]

    class _Stats:
        def model_dump(self):
            return {"candidate_count": 1, "evidence_rounds": 0, "challenge_count": 1}

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


def test_run_empty_diff_short_circuits():
    result = PipelineOrchestrator().run(object(), "   ")
    assert result.issues == []
    assert "没有检测到代码变更" in result.summary


_DIFF = "diff --git a/A.java b/A.java\n--- a/A.java\n+++ b/A.java\n@@ -1 +1,2 @@\n+int x=1;\n"


def test_adr032_mock_end_to_end():
    result = PipelineOrchestrator(enable_summary=False).run(None, _DIFF)
    assert isinstance(result, ReviewResult)
    assert len(result.issues) == 1


class _Stub:
    def invoke(self, _msgs):
        return None


class _FakeLLM:
    def with_structured_output(self, *a, **k):
        return _Stub()


class _FakeEngine:
    def review(self, llm, *, system_prompt, user_prompt, reviewer_name, max_retries, structured_method, enable_hitl=False):
        issue = Issue(
            severity=Severity.WARNING,
            file=f"{reviewer_name}.java",
            line=1,
            type=reviewer_name,
            message="m",
        )
        gc = [GatheredContext("get_file_content", f"{reviewer_name}.java", "x")]
        return ReviewOutcome(ReviewResult(summary=f"sum-{reviewer_name}", issues=[issue]), gc)


def test_graph_fanin_three_discoverers(monkeypatch):
    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _FakeEngine())
    orch = PipelineOrchestrator(enable_summary=False)
    trace: list = []
    meta: dict = {}
    result = orch.run(_FakeLLM(), _DIFF, trace_sink=trace, metadata_sink=meta)
    assert {i.type for i in result.issues} == {
        "ThreatModelAgent",
        "BehaviorAgent",
        "MaintainabilityAgent",
    }
    assert len(trace) == 3
    assert meta["council"]["candidate_count"] == 3
    assert meta["council"]["challenge_count"] == 3
    assert meta["council"]["candidate_count_by_agent"] == {
        "threat_model": 1,
        "behavior": 1,
        "maintainability": 1,
    }


def test_build_graph_default_nodes_are_adr032():
    graph = G.build_review_graph(enable_summary=False, llm=None)
    names = set(graph.get_graph().nodes)
    assert "context_provider" in names
    assert "discover_threat_model" in names
    assert "discover_behavior" in names
    assert "discover_maintainability" in names
    assert "council_coordinator" in names
    assert "council_judge" in names
    assert "evidence_agent" in names
    # 旧节点已删除
    assert "challenge_agent" not in names
    assert "self_checker" not in names
    assert "supervisor" not in names
    assert "aggregation" not in names
    assert "fp_filter" not in names


def test_candidate_and_evidence_request_limits_are_enforced(monkeypatch):
    original_from_issue = G.CandidateIssue.from_issue

    def _many_candidates_from_issue(issue, *, source_agent, category, index):
        c = original_from_issue(
            issue,
            source_agent=source_agent,
            category=category,
            index=index,
        )
        c.needs_evidence = True
        c.evidence_requests = [
            G.EvidenceRequest(
                candidate_id=c.id,
                target=f"{issue.file}:{i}",
                question=f"q{i}",
                reason=f"r{i}",
            )
            for i in range(3)
        ]
        return c

    class _ManyIssueEngine:
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
            issues = [
                Issue(
                    severity=Severity.WARNING,
                    file=f"{reviewer_name}-{i}.java",
                    line=i + 1,
                    type=reviewer_name,
                    message="m",
                )
                for i in range(8)
            ]
            return ReviewOutcome(ReviewResult(summary=f"sum-{reviewer_name}", issues=issues))

    monkeypatch.setattr(G.CandidateIssue, "from_issue", _many_candidates_from_issue)
    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _ManyIssueEngine())
    orch = PipelineOrchestrator(enable_summary=False)
    meta: dict = {}
    result = orch.run(_FakeLLM(), _DIFF, metadata_sink=meta)

    # MAX_CANDIDATES_PER_AGENT=10, 每 agent 造 8 条 → 3×8=24 条全部通过不截断
    assert len(result.issues) == 24
    assert meta["council"]["candidate_count"] == 24
    assert meta["council"]["evidence_request_count"] == 20  # capped at MAX_TOTAL_EVIDENCE_REQUESTS
    assert meta["council"]["truncated_candidates"] == 0  # 8 < 10, 不触发截断


def test_checkpointer_factory_memory_creates_MemorySaver():
    from codeguard_agent.pipeline.orchestrator import _create_checkpointer
    from langgraph.checkpoint.memory import MemorySaver

    assert isinstance(_create_checkpointer("memory", ""), MemorySaver)


def test_checkpointer_factory_empty_returns_none():
    from codeguard_agent.pipeline.orchestrator import _create_checkpointer

    assert _create_checkpointer("", "") is None


def test_orchestrator_with_memory_checkpointer_produces_same_result():
    orch = PipelineOrchestrator(enable_summary=False, checkpoint_backend="memory")
    result = orch.run(None, _DIFF, thread_id="adr032-same-result")
    assert len(result.issues) >= 1


def test_hitl_is_ignored_in_adr032_default_path():
    orch = PipelineOrchestrator(
        enable_summary=False,
        checkpoint_backend="memory",
    )
    result = orch.run(None, _DIFF, thread_id="hitl-ignored")
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
        return _MockToolResponse(True, result="SQL injection at line 5")

    def find_callers(self, query: str = "") -> _MockToolResponse:
        self.calls.append(("find_callers", {"query": query}))
        return _MockToolResponse(True, result=f"callers of {query}")

    def get_code_metrics(self, file_path: str = "") -> _MockToolResponse:
        self.calls.append(("get_code_metrics", {"file_path": file_path}))
        return _MockToolResponse(True, result=f"CC=12 LOC=200 for {file_path}")


def test_evidence_agent_routes_find_callers_by_preferred_tools():
    """preferred_tools 含 find_callers → 调 tool_client.find_callers()。"""
    mock = _MockToolClient()
    req = G.EvidenceRequest(
        candidate_id="behavior-1-A.java:10:t",
        target="A.java",
        preferred_tools=["find_callers"],
    )
    candidate = G.CandidateIssue(
        id="behavior-1-A.java:10:t",
        source_agent="behavior",
        file="A.java",
        line=10,
        type="t",
        severity_proposal=Severity.WARNING,
        claim="m",
    )
    node = G._evidence_agent_node(tool_client=mock)
    out = node({
        "evidence_requests": [req],
        "candidate_issues": [candidate],
        "evidence_round": 0,
    })
    assert len(out["evidence_notes"]) == 1
    assert out["evidence_notes"][0].status == "supported"
    assert any("find_callers" in s for s in out["evidence_notes"][0].supports)
    called_tools = {c[0] for c in mock.calls}
    assert "find_callers" in called_tools


def test_evidence_agent_routes_find_sensitive_apis_by_preferred_tools():
    """preferred_tools 含 find_sensitive_apis → 调 tool_client.find_sensitive_apis()。"""
    mock = _MockToolClient()
    req = G.EvidenceRequest(
        candidate_id="threat_model-1-A.java:5:t",
        target="A.java",
        preferred_tools=["find_sensitive_apis"],
    )
    candidate = G.CandidateIssue(
        id="threat_model-1-A.java:5:t",
        source_agent="threat_model",
        file="A.java",
        line=5,
        type="t",
        severity_proposal=Severity.WARNING,
        claim="m",
    )
    node = G._evidence_agent_node(tool_client=mock)
    out = node({
        "evidence_requests": [req],
        "candidate_issues": [candidate],
        "evidence_round": 0,
    })
    assert out["evidence_notes"][0].status == "supported"
    assert any("find_sensitive_apis" in s for s in out["evidence_notes"][0].supports)
    called_tools = {c[0] for c in mock.calls}
    assert "find_sensitive_apis" in called_tools


def test_evidence_agent_dedups_same_file_tool():
    """同一文件+工具 → 只调一次。"""
    mock = _MockToolClient()
    req1 = G.EvidenceRequest(
        candidate_id="c1",
        target="A.java",
        preferred_tools=["get_code_metrics"],
    )
    req2 = G.EvidenceRequest(
        candidate_id="c2",
        target="A.java",
        preferred_tools=["get_code_metrics"],
    )
    candidate = G.CandidateIssue(
        id="c1",
        source_agent="maintainability",
        file="A.java",
        line=1,
        type="t",
        severity_proposal=Severity.WARNING,
        claim="m",
    )
    node = G._evidence_agent_node(tool_client=mock)
    out = node({
        "evidence_requests": [req1, req2],
        "candidate_issues": [candidate, G.CandidateIssue(
            id="c2", source_agent="maintainability", file="A.java", line=2,
            type="t", severity_proposal=Severity.WARNING, claim="m2",
        )],
        "evidence_round": 0,
    })
    # 两次请求，但只调一次 get_code_metrics
    metrics_calls = [c for c in mock.calls if c[0] == "get_code_metrics"]
    assert len(metrics_calls) == 1
    assert len(out["evidence_notes"]) == 2


def test_evidence_agent_fallback_context_bundle_when_no_tool_client():
    """tool_client=None → 回退 ContextBundle 字符串搜索。"""
    req = G.EvidenceRequest(
        candidate_id="c1",
        target="UserService.java",
        preferred_tools=[],  # 空
    )
    bundle = G.ContextBundle(
        changed_files=["UserService.java"],
        diff_summary="修改了 UserService",
    )
    node = G._evidence_agent_node(tool_client=None)
    out = node({
        "evidence_requests": [req],
        "candidate_issues": [
            G.CandidateIssue(
                id="c1", source_agent="threat_model", file="UserService.java",
                line=1, type="t", severity_proposal=Severity.WARNING, claim="m",
            )
        ],
        "context_bundle": bundle,
        "evidence_round": 0,
    })
    assert out["evidence_notes"][0].status == "supported"
    assert any("ContextBundle" in s for s in out["evidence_notes"][0].supports)


def test_evidence_agent_marks_tool_failure_as_unknown():
    """工具返回失败 → unknowns + not_found。"""
    mock = _MockToolClient()

    # 覆写为返回失败
    def _fail_get_file_content(file_path=""):
        mock.calls.append(("get_file_content", {"file_path": file_path}))
        return _MockToolResponse(False, error="file not found")

    mock.get_file_content = _fail_get_file_content

    req = G.EvidenceRequest(
        candidate_id="c1",
        target="Ghost.java",
        preferred_tools=["get_file_content"],
    )
    candidate = G.CandidateIssue(
        id="c1", source_agent="threat_model", file="Ghost.java",
        line=1, type="t", severity_proposal=Severity.WARNING, claim="m",
    )
    node = G._evidence_agent_node(tool_client=mock)
    out = node({
        "evidence_requests": [req],
        "candidate_issues": [candidate],
        "evidence_round": 0,
    })
    assert out["evidence_notes"][0].status == "not_found"
    assert len(out["evidence_notes"][0].unknowns) >= 1


def test_evidence_agent_increments_evidence_round():
    mock = _MockToolClient()
    req = G.EvidenceRequest(
        candidate_id="c1",
        target="A.java",
        preferred_tools=["get_file_content"],
    )
    candidate = G.CandidateIssue(
        id="c1", source_agent="behavior", file="A.java", line=1,
        type="t", severity_proposal=Severity.WARNING, claim="m",
    )
    node = G._evidence_agent_node(tool_client=mock)
    out = node({
        "evidence_requests": [req],
        "candidate_issues": [candidate],
        "evidence_round": 0,
    })
    assert out["evidence_round"] == 1
