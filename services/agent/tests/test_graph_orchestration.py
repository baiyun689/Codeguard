"""LangGraph supervisor 编排的针对性测试。

覆盖:State reducer、supervisor 决策(确定性/智能/护栏/兜底)、审查员节点错误隔离与 mock、
以及门面侧信道(gathered_context 只进 trace_sink、不进 ReviewResult)。
不调真实 LLM:用桩函数 / monkeypatch 隔离节点内部。
"""

from __future__ import annotations

from codeguard_agent.models.schemas import Issue, ReviewResult, Severity
from codeguard_agent.pipeline import graph as G
from codeguard_agent.pipeline.engines import GatheredContext
from codeguard_agent.pipeline.orchestrator import PipelineOrchestrator


# --------------------------------------------------------------------------- #
# 2.3 reducer
# --------------------------------------------------------------------------- #


def test_dedup_gathered_reducer_dedups_by_tool_args_keep_order():
    a = GatheredContext("get_file_content", "A.java", "x")
    b = GatheredContext("get_file_content", "B.java", "y")
    a_dup = GatheredContext("get_file_content", "A.java", "x-again")
    out = G.dedup_gathered_reducer([a], [b, a_dup])
    assert [g.args for g in out] == ["A.java", "B.java"]  # A 去重保留首次,顺序稳定


def test_dedup_gathered_reducer_handles_none():
    assert G.dedup_gathered_reducer(None, None) == []
    one = [GatheredContext("t", "a", "c")]
    assert G.dedup_gathered_reducer(None, one) == one


def test_issues_fanin_uses_add_semantics():
    # State 注解上 issues 用 operator.add:两节点各返回一部分应被拼接。
    import operator

    assert operator.add([1], [2, 3]) == [1, 2, 3]


# --------------------------------------------------------------------------- #
# 4.x supervisor 决策
# --------------------------------------------------------------------------- #


def _base_state(**over):
    st = {
        "diff_text": "d",
        "llm": object(),  # 非 None
        "enable_supervisor": False,
        "max_review_rounds": G.DEFAULT_MAX_ROUNDS,
        "dispatched": set(),
        "iteration": 0,
    }
    st.update(over)
    return st


def test_supervisor_deterministic_first_round_dispatches_all():
    out = G.supervisor_node(_base_state(enable_supervisor=False))
    assert out["route"] == "dispatch"
    assert sorted(out["dispatch"]) == sorted(G._ALL_REVIEWER_NAMES)
    assert out["iteration"] == 1


def test_supervisor_deterministic_finishes_after_all_dispatched():
    out = G.supervisor_node(
        _base_state(enable_supervisor=False, dispatched=set(G._ALL_REVIEWER_NAMES))
    )
    assert out["route"] == "finish"


def test_supervisor_mock_llm_none_is_deterministic():
    # 即便 enable_supervisor=True,llm None(mock)也走确定性,不发决策。
    out = G.supervisor_node(_base_state(llm=None, enable_supervisor=True))
    assert out["route"] == "dispatch"
    assert sorted(out["dispatch"]) == sorted(G._ALL_REVIEWER_NAMES)


def test_supervisor_iteration_guard_forces_finish():
    out = G.supervisor_node(
        _base_state(enable_supervisor=True, iteration=G.DEFAULT_MAX_ROUNDS, dispatched={"security"})
    )
    assert out["route"] == "finish"
    assert "上限" in out["supervisor_log"][0]


def test_supervisor_smart_dispatches_subset(monkeypatch):
    monkeypatch.setattr(
        G,
        "_decide_dispatch",
        lambda state: G.SupervisorDecision(action="dispatch", reviewers=["security"], reason="仅安全"),
    )
    out = G.supervisor_node(_base_state(enable_supervisor=True))
    assert out["route"] == "dispatch"
    assert out["dispatch"] == ["security"]


def test_supervisor_smart_finish_bottoms_out_when_nothing_dispatched(monkeypatch):
    # 决策 finish 但尚无任何审查产出 → 兜底强制全派一轮(不漏审)。
    monkeypatch.setattr(
        G, "_decide_dispatch", lambda state: G.SupervisorDecision(action="finish", reason="空")
    )
    out = G.supervisor_node(_base_state(enable_supervisor=True, dispatched=set()))
    assert out["route"] == "dispatch"
    assert sorted(out["dispatch"]) == sorted(G._ALL_REVIEWER_NAMES)


def test_supervisor_smart_finish_when_already_dispatched(monkeypatch):
    monkeypatch.setattr(
        G, "_decide_dispatch", lambda state: G.SupervisorDecision(action="finish", reason="够了")
    )
    out = G.supervisor_node(_base_state(enable_supervisor=True, dispatched={"security"}))
    assert out["route"] == "finish"


# --------------------------------------------------------------------------- #
# 3.2 审查员节点错误隔离 + mock
# --------------------------------------------------------------------------- #


def test_reviewer_node_error_isolation(monkeypatch):
    class _Boom:
        def review(self, *a, **k):
            raise RuntimeError("engine down")

    monkeypatch.setattr(G, "_make_engine", lambda state: _Boom())
    node = G.make_reviewer_node(G.DEFAULT_REVIEWERS[0])
    out = node({"diff_text": "d", "llm": object(), "file_groups": {}})
    # 不抛断;贡献空 issues、记录告警、标记已派发。
    assert out.get("issues", []) == []
    assert out["dispatched"] == {G.DEFAULT_REVIEWERS[0].name}
    assert any("失败" in s for s in out.get("supervisor_log", []))


def test_reviewer_node_mock_only_security_returns_issues():
    sec = G.make_reviewer_node(G.Reviewer("security", "security.txt"))
    other = G.make_reviewer_node(G.Reviewer("logic", "logic.txt"))
    sec_out = sec({"llm": None})
    other_out = other({"llm": None})
    assert len(sec_out["issues"]) >= 1
    assert other_out.get("issues", []) == []
    assert other_out["dispatched"] == {"logic"}


# --------------------------------------------------------------------------- #
# 3.6 门面侧信道:gathered_context 只进 trace_sink,不进 ReviewResult
# --------------------------------------------------------------------------- #


def test_run_routes_gathered_context_to_trace_sink_not_result(monkeypatch):
    gc = [GatheredContext("get_file_content", "X.java", "body")]
    issues = [Issue(severity=Severity.WARNING, file="X.java", line=1, type="t", message="m")]

    class _FakeGraph:
        def invoke(self, initial, config=None):
            return {"summary": "s", "final_issues": issues, "gathered_context": gc}

    monkeypatch.setattr(G, "build_review_graph", lambda **k: _FakeGraph())
    # orchestrator 经 `from ... import build_review_graph` 绑定到本模块名,需同步替换。
    monkeypatch.setattr(
        "codeguard_agent.pipeline.orchestrator.build_review_graph", lambda **k: _FakeGraph()
    )

    trace: list = []
    result = PipelineOrchestrator().run(object(), "some diff", trace_sink=trace)

    assert isinstance(result, ReviewResult)
    assert result.issues == issues
    assert trace == gc  # 工具上下文进了侧信道
    # ReviewResult 结构上不含工具痕迹字段(守 ADR-001)。
    assert not hasattr(result, "gathered_context")


def test_run_empty_diff_short_circuits():
    result = PipelineOrchestrator().run(object(), "   ")
    assert result.issues == []
    assert "没有检测到代码变更" in result.summary


# --------------------------------------------------------------------------- #
# 确定性模式端到端连通(mock)
# --------------------------------------------------------------------------- #


def test_deterministic_mock_end_to_end():
    diff = "diff --git a/A.java b/A.java\n--- a/A.java\n+++ b/A.java\n@@ -1 +1,2 @@\n+int x=1;\n"
    result = PipelineOrchestrator().run(None, diff)
    assert isinstance(result, ReviewResult)
    assert len(result.issues) == 1  # 单条 mock,不被三审查员三倍化


# --------------------------------------------------------------------------- #
# 真实编译图集成:fan-in + supervisor 循环(用假引擎,不调真实 LLM)
# --------------------------------------------------------------------------- #


class _Stub:
    def invoke(self, _msgs):
        return None  # 让 AggregationStage 第二段回退到规则去重


class _FakeLLM:
    def with_structured_output(self, *a, **k):
        return _Stub()


class _FakeEngine:
    """每个审查员返回 1 条带自身标识的 issue + 1 条工具上下文。"""

    def review(self, llm, *, system_prompt, user_prompt, reviewer_name, max_retries, structured_method):
        from codeguard_agent.pipeline.engines import ReviewOutcome

        iss = [Issue(severity=Severity.WARNING, file=f"{reviewer_name}.java", line=1,
                     type=reviewer_name, message="m")]
        gc = [GatheredContext("get_file_content", f"{reviewer_name}.java", "x")]
        return ReviewOutcome(ReviewResult(summary=f"sum-{reviewer_name}", issues=iss), gc)


_DIFF = "diff --git a/A.java b/A.java\n--- a/A.java\n+++ b/A.java\n@@ -1 +1,2 @@\n+int x=1;\n"


def test_graph_fanin_three_reviewers(monkeypatch):
    monkeypatch.setattr(G, "_make_engine", lambda state: _FakeEngine())
    orch = PipelineOrchestrator(enable_summary=False)  # 跳过摘要 LLM,确定性全派
    trace: list = []
    r = orch.run(_FakeLLM(), _DIFF, trace_sink=trace)
    assert {i.type for i in r.issues} == {"security", "logic", "quality"}  # 三路 fan-in
    assert len(trace) == 3  # 三条不同来源的工具上下文进侧信道


def test_graph_supervisor_loop_subset_then_finish(monkeypatch):
    monkeypatch.setattr(G, "_make_engine", lambda state: _FakeEngine())
    calls = {"n": 0}

    def fake_decide(state):
        calls["n"] += 1
        if calls["n"] == 1:
            return G.SupervisorDecision(action="dispatch", reviewers=["security"], reason="只看安全")
        return G.SupervisorDecision(action="finish", reason="够了")

    monkeypatch.setattr(G, "_decide_dispatch", fake_decide)
    orch = PipelineOrchestrator(enable_summary=False, enable_supervisor=True)
    r = orch.run(_FakeLLM(), _DIFF)
    assert [i.type for i in r.issues] == ["security"]  # 仅派发 security 一路
    assert calls["n"] >= 2  # 决策至少两轮:派发 → finish(证明 supervisor 循环成立)
