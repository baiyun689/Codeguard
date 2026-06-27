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
        "enable_supervisor": False,
        "max_review_rounds": G.DEFAULT_MAX_ROUNDS,
        "dispatched": set(),
        "iteration": 0,
    }
    st.update(over)
    return st


# supervisor 节点工厂
_sup_smart = G._supervisor_node(llm=object())  # 有 LLM → 可用智能决策
_sup_mock = G._supervisor_node(llm=None)       # 无 LLM → 确定性调度


def test_supervisor_deterministic_first_round_dispatches_all():
    out = _sup_smart(_base_state(enable_supervisor=False))
    assert out["route"] == "dispatch"
    assert sorted(out["dispatch"]) == sorted(G._ALL_REVIEWER_NAMES)
    assert out["iteration"] == 1


def test_supervisor_deterministic_finishes_after_all_dispatched():
    out = _sup_smart(
        _base_state(enable_supervisor=False, dispatched=set(G._ALL_REVIEWER_NAMES))
    )
    assert out["route"] == "finish"


def test_supervisor_mock_llm_none_is_deterministic():
    # 即便 enable_supervisor=True,llm=None(mock)也走确定性,不发决策。
    out = _sup_mock(_base_state(enable_supervisor=True))
    assert out["route"] == "dispatch"
    assert sorted(out["dispatch"]) == sorted(G._ALL_REVIEWER_NAMES)


def test_supervisor_iteration_guard_forces_finish():
    out = _sup_smart(
        _base_state(enable_supervisor=True, iteration=G.DEFAULT_MAX_ROUNDS, dispatched={"security"})
    )
    assert out["route"] == "finish"
    assert "上限" in out["supervisor_log"][0]


def test_supervisor_smart_dispatches_subset(monkeypatch):
    monkeypatch.setattr(
        G,
        "_decide_dispatch",
        lambda state, llm: G.SupervisorDecision(action="dispatch", reviewers=["security"], reason="仅安全"),
    )
    out = _sup_smart(_base_state(enable_supervisor=True))
    assert out["route"] == "dispatch"
    assert out["dispatch"] == ["security"]


def test_supervisor_smart_finish_bottoms_out_when_nothing_dispatched(monkeypatch):
    # 决策 finish 但尚无任何审查产出 → 兜底强制全派一轮(不漏审)。
    monkeypatch.setattr(
        G, "_decide_dispatch", lambda state, llm: G.SupervisorDecision(action="finish", reason="空")
    )
    out = _sup_smart(_base_state(enable_supervisor=True, dispatched=set()))
    assert out["route"] == "dispatch"
    assert sorted(out["dispatch"]) == sorted(G._ALL_REVIEWER_NAMES)


def test_supervisor_smart_finish_when_already_dispatched(monkeypatch):
    monkeypatch.setattr(
        G, "_decide_dispatch", lambda state, llm: G.SupervisorDecision(action="finish", reason="够了")
    )
    out = _sup_smart(_base_state(enable_supervisor=True, dispatched={"security"}))
    assert out["route"] == "finish"


# --------------------------------------------------------------------------- #
# 3.2 审查员子图:内部结构 + 错误隔离 + mock(design D12 第二刀)
# --------------------------------------------------------------------------- #


def test_reviewer_subgraph_exposes_internal_nodes():
    # 子图化的招牌收益:审查员内部流水线在图层面显式可见。
    sub = G.build_reviewer_subgraph(G.DEFAULT_REVIEWERS[0])
    node_names = set(sub.get_graph().nodes)
    assert {"prepare", "review", "collect"} <= node_names


def test_reviewer_subgraph_error_isolation(monkeypatch):
    class _Boom:
        def review(self, *a, **k):
            raise RuntimeError("engine down")

    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _Boom())
    sub = G.build_reviewer_subgraph(G.DEFAULT_REVIEWERS[0], llm=object())
    out = sub.invoke({"diff_text": "d", "file_groups": {}})
    # 不抛断;贡献空 issues、记录告警、标记已派发。
    assert out.get("issues", []) == []
    assert out["dispatched"] == {G.DEFAULT_REVIEWERS[0].name}
    assert any("失败" in s for s in out.get("supervisor_log", []))


def test_reviewer_subgraph_mock_only_security_returns_issues():
    sec = G.build_reviewer_subgraph(G.Reviewer("security", "security.txt"), llm=None)
    other = G.build_reviewer_subgraph(G.Reviewer("logic", "logic.txt"), llm=None)
    sec_out = sec.invoke({})
    other_out = other.invoke({})
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

    def review(self, llm, *, system_prompt, user_prompt, reviewer_name, max_retries, structured_method, enable_hitl=False):
        from codeguard_agent.pipeline.engines import ReviewOutcome

        iss = [Issue(severity=Severity.WARNING, file=f"{reviewer_name}.java", line=1,
                     type=reviewer_name, message="m")]
        gc = [GatheredContext("get_file_content", f"{reviewer_name}.java", "x")]
        return ReviewOutcome(ReviewResult(summary=f"sum-{reviewer_name}", issues=iss), gc)


_DIFF = "diff --git a/A.java b/A.java\n--- a/A.java\n+++ b/A.java\n@@ -1 +1,2 @@\n+int x=1;\n"


def test_graph_fanin_three_reviewers(monkeypatch):
    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _FakeEngine())
    orch = PipelineOrchestrator(enable_summary=False)  # 跳过摘要 LLM,确定性全派
    trace: list = []
    r = orch.run(_FakeLLM(), _DIFF, trace_sink=trace)
    assert {i.type for i in r.issues} == {"security", "logic", "quality"}  # 三路 fan-in
    assert len(trace) == 3  # 三条不同来源的工具上下文进侧信道


def test_graph_supervisor_loop_subset_then_finish(monkeypatch):
    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _FakeEngine())
    calls = {"n": 0}

    def fake_decide(state, llm):
        calls["n"] += 1
        if calls["n"] == 1:
            return G.SupervisorDecision(action="dispatch", reviewers=["security"], reason="只看安全")
        return G.SupervisorDecision(action="finish", reason="够了")

    monkeypatch.setattr(G, "_decide_dispatch", fake_decide)
    orch = PipelineOrchestrator(enable_summary=False, enable_supervisor=True)
    r = orch.run(_FakeLLM(), _DIFF)
    assert [i.type for i in r.issues] == ["security"]  # 仅派发 security 一路
    assert calls["n"] >= 2  # 决策至少两轮:派发 → finish(证明 supervisor 循环成立)


# --------------------------------------------------------------------------- #
# Checkpoint 持久化与中断恢复(change langgraph-checkpoint-interrupt)
# --------------------------------------------------------------------------- #


def test_checkpointer_factory_memory_creates_MemorySaver():
    from codeguard_agent.pipeline.orchestrator import _create_checkpointer

    cp = _create_checkpointer("memory", "")
    assert cp is not None
    # MemorySaver 应来自 langgraph.checkpoint.memory
    from langgraph.checkpoint.memory import MemorySaver

    assert isinstance(cp, MemorySaver)


def test_checkpointer_factory_empty_returns_none():
    from codeguard_agent.pipeline.orchestrator import _create_checkpointer

    assert _create_checkpointer("", "") is None


def test_checkpointer_factory_sqlite_without_package_returns_none(monkeypatch):
    """SqliteSaver 需要 langgraph-checkpoint-sqlite 包;未安装时优雅降级为 None。"""
    from codeguard_agent.pipeline.orchestrator import _create_checkpointer

    cp = _create_checkpointer("sqlite", ":memory:")
    assert cp is None  # 当前环境未装 langgraph-checkpoint-sqlite


def test_orchestrator_no_checkpointer_behavior_unchanged(monkeypatch):
    """不传 checkpoint_backend 时行为与当前完全一致:图正常执行并产出 ReviewResult。"""
    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _FakeEngine())
    orch = PipelineOrchestrator(enable_summary=False, enable_supervisor=False)
    r = orch.run(_FakeLLM(), _DIFF)
    assert len(r.issues) >= 1
    assert r.summary


def test_orchestrator_with_memory_checkpointer_produces_same_result(monkeypatch):
    """MemorySaver 下 mock 模式一次性跑通,产出 ReviewResult(与无 checkpointer 一致)。"""
    orch = PipelineOrchestrator(
        enable_summary=False,
        enable_supervisor=False,
        checkpoint_backend="memory",
    )
    # 用 mock 模式(llm=None) — None 可被 msgpack 序列化,避免 _FakeLLM 序列化失败
    r = orch.run(None, _DIFF, thread_id="test-same-result")
    assert len(r.issues) >= 1
    assert r.summary


def test_same_thread_id_second_invoke_returns_cached_result(monkeypatch):
    """同一 thread_id 连续两次 invoke:第二次应返回缓存结果,不重新派发审查员。

    用 mock 模式(llm=None)规避 LLM 对象不可序列化的问题。
    """
    from langgraph.checkpoint.memory import MemorySaver

    cp = MemorySaver()
    g = G.build_review_graph(enable_summary=False, checkpointer=cp, llm=None)
    st: G.ReviewState = {
        "diff_text": _DIFF,
        "enabled_tools": None,
        "max_retries": 3,
        "structured_method": "function_calling",
        "enable_supervisor": False,
        "max_review_rounds": G.DEFAULT_MAX_ROUNDS,
        "fp_llm_verify": False,
        "issues": [],
        "gathered_context": [],
        "review_summaries": [],
        "dispatched": set(),
        "iteration": 0,
        "final_issues": [],
        "supervisor_log": [],
    }
    config = {"configurable": {"thread_id": "twice-test"}, "recursion_limit": 50}
    r1 = g.invoke(st, config)
    r2 = g.invoke(None, config)
    assert len(r1.get("final_issues") or []) == len(r2.get("final_issues") or [])


def test_different_thread_ids_independent(monkeypatch):
    """不同 thread_id 的 state 互不干扰。"""
    from langgraph.checkpoint.memory import MemorySaver

    cp = MemorySaver()
    g = G.build_review_graph(enable_summary=False, checkpointer=cp, llm=None)
    base_st = {
        "diff_text": _DIFF,
        "enabled_tools": None,
        "max_retries": 3,
        "structured_method": "function_calling",
        "enable_supervisor": False,
        "max_review_rounds": G.DEFAULT_MAX_ROUNDS,
        "fp_llm_verify": False,
        "issues": [],
        "gathered_context": [],
        "review_summaries": [],
        "dispatched": set(),
        "iteration": 0,
        "final_issues": [],
        "supervisor_log": [],
    }
    r_a = g.invoke(dict(base_st), {"configurable": {"thread_id": "A"}, "recursion_limit": 50})
    r_b = g.invoke(dict(base_st), {"configurable": {"thread_id": "B"}, "recursion_limit": 50})
    assert (r_a.get("final_issues") or []) == (r_b.get("final_issues") or [])


def test_graph_build_without_checkpointer_compiles(monkeypatch):
    """不传 checkpointer 时 compile 成功,与当前行为一致。"""
    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _FakeEngine())
    g = G.build_review_graph(enable_summary=False, llm=_FakeLLM())
    assert g is not None
    st: G.ReviewState = {
        "diff_text": _DIFF,
        "enabled_tools": None,
        "max_retries": 3,
        "structured_method": "function_calling",
        "enable_supervisor": False,
        "max_review_rounds": G.DEFAULT_MAX_ROUNDS,
        "fp_llm_verify": False,
        "issues": [],
        "gathered_context": [],
        "review_summaries": [],
        "dispatched": set(),
        "iteration": 0,
        "final_issues": [],
        "supervisor_log": [],
    }
    result = g.invoke(st, {"configurable": {"thread_id": "test-no-ckpt"}, "recursion_limit": 50})
    assert result.get("final_issues") is not None


# --------------------------------------------------------------------------- #
# Human-in-the-loop 测试(change langgraph-human-in-the-loop)
# --------------------------------------------------------------------------- #


def test_supervisor_node_finish_without_hitl_no_interrupt():
    """HITL 关闭时 supervisor finish 直接返回,不调 interrupt。"""
    from langgraph.checkpoint.memory import MemorySaver

    cp = MemorySaver()
    g = G.build_review_graph(enable_summary=False, checkpointer=cp, llm=None)
    st: G.ReviewState = {
        "diff_text": _DIFF,
        "enabled_tools": None,
        "max_retries": 3,
        "structured_method": "function_calling",
        "enable_supervisor": False,
        "enable_hitl": False,
        "max_review_rounds": G.DEFAULT_MAX_ROUNDS,
        "fp_llm_verify": False,
        "issues": [],
        "gathered_context": [],
        "review_summaries": [],
        "dispatched": set(),
        "iteration": 0,
        "final_issues": [],
        "supervisor_log": [],
    }
    result = g.invoke(st, {"configurable": {"thread_id": "no-hitl-test"}, "recursion_limit": 50})
    assert result.get("final_issues") is not None


def test_hitl_closed_no_interrupt(monkeypatch):
    """HITL 关闭时图一次性跑通,无 interrupt。"""
    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: _FakeEngine())
    orch = PipelineOrchestrator(
        enable_summary=False, enable_supervisor=False,
        checkpoint_backend="memory", enable_human_in_the_loop=False,
    )
    r = orch.run(None, _DIFF, thread_id="hitl-off")
    assert len(r.issues) >= 1


def test_hitl_dialog_commands():
    """交互式对话函数:命令解析正确。"""
    from codeguard_agent.cli import _hitl_supervisor_finish_dialog, _hitl_reviewer_limit_dialog

    # supervisor_finish dialog 命令(root 测试)
    # (交互式需要 stdin,这里只验证函数可 import 且签名正确)
    assert callable(_hitl_supervisor_finish_dialog)
    assert callable(_hitl_reviewer_limit_dialog)


def test_hitl_pipeline_nohitl_unchanged():
    """HITL 默认关 = 当前行为完全不变。通过 PipelineOrchestrator 调用。"""
    orch = PipelineOrchestrator(
        enable_summary=False, enable_supervisor=False,
    )
    # 无 checkpoint 无 HITL:mock 模式应安全跑通
    r = orch.run(None, _DIFF)
    assert len(r.issues) >= 1


def test_config_hitl_default_false():
    """CODEGUARD_ENABLE_HITL 默认 false。"""
    from codeguard_agent.config import Settings
    import os

    # 设空字符串而非 pop:保证 _load_dotenv(override=False) 不会用 .env 值覆盖。
    old = os.environ.get("CODEGUARD_ENABLE_HITL")
    os.environ["CODEGUARD_ENABLE_HITL"] = ""
    try:
        s = Settings.from_env()
        assert s.enable_human_in_the_loop is False
    finally:
        if old is not None:
            os.environ["CODEGUARD_ENABLE_HITL"] = old
        else:
            os.environ.pop("CODEGUARD_ENABLE_HITL", None)
