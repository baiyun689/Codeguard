"""ADR-032 ReviewCouncil 编排测试。"""

from __future__ import annotations

from codeguard_agent.models.council import Challenge, ContextBundle, ContextFact, EvidenceNote
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
        "evidence_round": 0,
        "max_evidence_rounds": 1,
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


def test_coordinator_skips_evidence_and_challenge_when_no_candidates():
    assert G._route_after_coordinator(_base_state()) == "self_checker"


def test_coordinator_routes_to_evidence_by_structured_flag():
    c = _candidate(needs_evidence=True)
    assert G._route_after_coordinator(_base_state(candidate_issues=[c])) == "evidence_agent"


def test_coordinator_runs_challenge_when_candidates_exist():
    c = _candidate()
    assert G._route_after_coordinator(_base_state(candidate_issues=[c])) == "challenge_agent"


def test_coordinator_respects_evidence_round_limit():
    c = _candidate(needs_evidence=True)
    ch = Challenge(candidate_id=c.id, verdict="needs_more_evidence")
    state = _base_state(candidate_issues=[c], challenges=[ch], evidence_round=1)
    assert G._route_after_coordinator(state) == "self_checker"


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


def test_challenge_agent_does_not_modify_candidates():
    c = _candidate(needs_evidence=True, confidence=0.8)
    node = G._challenge_agent_node()
    out = node({"candidate_issues": [c], "evidence_notes": [EvidenceNote(candidate_id=c.id, unknowns=["x"])]})
    assert out["challenges"][0].verdict == "needs_more_evidence"
    assert c.needs_evidence is True


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
    assert "self_checker" in names
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
                kind="open_question",
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

    assert len(result.issues) == 15
    assert meta["council"]["candidate_count"] == 15
    assert meta["council"]["evidence_request_count"] == 20
    assert meta["council"]["truncated_candidates"] == 9


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
        enable_human_in_the_loop=True,
    )
    result = orch.run(None, _DIFF, thread_id="hitl-ignored")
    assert len(result.issues) >= 1


def test_config_hitl_default_false(monkeypatch):
    from codeguard_agent.config import Settings

    monkeypatch.setenv("CODEGUARD_ENABLE_HITL", "")
    settings = Settings.from_env()
    assert settings.enable_human_in_the_loop is False
