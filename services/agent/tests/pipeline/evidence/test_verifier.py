"""evidence_verifier 链校验与固定配方测试(ADR-046)。"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, Mock

import pytest

from codeguard_agent.models.council import CandidateFact, CandidateIssue
from codeguard_agent.models.schemas import EvidenceTraceStep, Severity
from codeguard_agent.models.tasks import (
    ContextFact,
    ReviewTask,
    RiskTag,
    TaskContextBundle,
)
from codeguard_agent.pipeline.evidence.planner import CandidateDossier
from codeguard_agent.pipeline.evidence.verifier import (
    _call_tool,
    _collect_facts,
    _graph_assertions_match,
    _graph_summary,
    _located_match,
    _parse_assertions,
    _RelationBatch,
    _relation_payload,
    _side_match,
    _symbol_id,
    analyze_relations,
    recipe_calls,
    replay_calls,
    validate_chain,
    verify_evidence,
)


def _dossier(line=10, symbol="S1", task_file="src/A.java",
             facts=None, chain=()) -> CandidateDossier:
    candidate = CandidateIssue(
        id="c1", task_id="t1", source_agent="threat_model",
        file=task_file, line=line, type="t",
        severity_proposal=Severity.WARNING, claim="claim", confidence=0.8,
        evidence_chain=list(chain),
    )
    task = ReviewTask(id="t1", file=task_file, patch="+x", changed_lines=[line])
    if facts is None:
        facts = [
            ContextFact(
                source="tool:resolve_change_context",
                kind="symbol_context",
                content='{"symbol_id": "%s", "start_line": 5, "end_line": 20}' % symbol,
            )
        ]
    bundle = TaskContextBundle(task_id="t1", facts=facts)
    return CandidateDossier(
        candidate=candidate, task=task, context_bundle=bundle
    )


def _batch_metrics_payload(batch) -> dict:
    for event, detail in batch.trace:
        if event == "evidence_batch_metrics":
            return json.loads(detail)
    pytest.fail("evidence_batch_metrics 事件缺失")


def test_validate_chain_drops_unknown_tool_and_missing_located():
    steps = [
        # tool 是 Literal,非法工具名需 model_construct 绕过校验构造
        EvidenceTraceStep.model_construct(tool="rm_rf", args={}, located="x"),
        EvidenceTraceStep(tool="get_file_content", args={"file_path": "A.java"}, located=""),
        EvidenceTraceStep(tool="get_file_content", args={"file_path": "A.java"}, located="int x;"),
    ]
    assert validate_chain(steps) == (steps[2],)


def test_validate_chain_requires_valid_args_and_truncates():
    steps = [
        EvidenceTraceStep(tool="inspect_change_impact", args={"file_path": "A.java"}, located="x"),  # 参数键错
    ]
    assert validate_chain(steps) == ()
    many = [
        EvidenceTraceStep(tool="get_file_content", args={"file_path": "A.java"}, located="x%d" % i)
        for i in range(5)
    ]
    assert len(validate_chain(many)) == 3


def test_recipe_calls_security_tag_adds_security_path():
    calls = recipe_calls(_dossier(), RiskTag.INJECTION)
    tools = [c[0] for c in calls]
    assert tools == ["get_file_content", "inspect_change_impact", "inspect_security_path"]


def test_recipe_calls_maintainability_tag_adds_structure():
    calls = recipe_calls(_dossier(), RiskTag.COMPLEXITY_CONTROL_FLOW)
    assert [c[0] for c in calls] == ["get_file_content", "inspect_change_impact", "inspect_structure"]


def test_recipe_calls_no_symbol_file_only():
    dossier = _dossier()
    dossier.context_bundle.facts = []  # type: ignore[attr-defined]
    assert recipe_calls(dossier, RiskTag.GENERAL_REVIEW) == [("get_file_content", {"file_path": "src/A.java"})]


def test_symbol_id_matches_line_range_and_falls_back():
    assert _symbol_id(_dossier(line=7)) == "S1"
    assert _symbol_id(_dossier(line=999)) == "S1"  # 未命中回退首个 symbol


def test_replay_calls_maps_steps_to_call_tuples():
    steps = (
        EvidenceTraceStep(tool="get_file_content", args={"file_path": "A.java"}, located="x"),
        EvidenceTraceStep(tool="inspect_change_impact", args={"symbol_id": "S1"}, located="y"),
    )
    assert replay_calls(steps) == [
        ("get_file_content", {"file_path": "A.java"}),
        ("inspect_change_impact", {"symbol_id": "S1"}),
    ]


def test_symbol_id_none_bundle_returns_empty():
    base = _dossier()
    dossier = CandidateDossier(
        candidate=base.candidate, task=base.task,
        context_bundle=None,
    )
    assert _symbol_id(dossier) == ""


def test_symbol_id_skips_truncated_fact_and_falls_back():
    dossier = _dossier(line=10, facts=[
        ContextFact(
            source="tool:resolve_change_context", kind="symbol_context",
            content='{"symbol_id": "S1", "start_line": 5, "end_line": 20}',
            truncated=True,
        ),
        ContextFact(
            source="tool:resolve_change_context", kind="symbol_context",
            content='{"symbol_id": "S2", "start_line": 1, "end_line": 2}',
        ),
    ])
    assert _symbol_id(dossier) == "S2"  # 截断事实跳过,回退首个完整 symbol


def test_symbol_id_skips_bad_json_and_falls_back():
    dossier = _dossier(line=10, facts=[
        ContextFact(
            source="tool:resolve_change_context", kind="symbol_context",
            content="{not valid json",
        ),
        ContextFact(
            source="tool:resolve_change_context", kind="symbol_context",
            content='{"symbol_id": "S2", "start_line": 1, "end_line": 2}',
        ),
    ])
    assert _symbol_id(dossier) == "S2"  # 坏 JSON 跳过,回退首个可解析 symbol


def test_located_match_ignores_whitespace():
    assert _located_match("int x = 1;\nint y = 2;", "int x=1;")
    assert not _located_match("int x = 1;", "int z = 1;")


def _tool_client(**outputs) -> MagicMock:
    client = MagicMock()
    for name, value in outputs.items():
        # result 显式置 None,确保 _call_tool 走 as_tool_output 分支(MagicMock
        # 自动创建的 .result 为 truthy 会遮蔽 as_tool_output 返回值)
        getattr(client, name).return_value = Mock(
            success=True, result=None, as_tool_output=Mock(return_value=value),
        )
    return client


def test_collect_facts_verified_and_recipe_statuses():
    dossier = _dossier(chain=[
        EvidenceTraceStep(
            tool="get_file_content",
            args={"file_path": "src/A.java"},
            located="int x = 1;",
        )
    ])
    tool_client = _tool_client(
        get_file_content="int x = 1;",
        inspect_change_impact=(
            '{"status": "confirmed", "subject_symbol_id": "S1", "relationships": []}'
        ),
    )
    facts, trace, gathered = _collect_facts(
        [dossier], tool_client=tool_client,
        tag_by_candidate={"c1": RiskTag.GENERAL_REVIEW},
    )
    statuses = {f.source: f.replay_status for f in facts["c1"]}
    assert statuses["tool:get_file_content"] == "verified"
    assert [event for event, _ in trace] == [
        "candidate_evidence_path",
        "evidence_tool_called",
    ]
    assert json.loads(trace[0][1]) == {"candidate_id": "c1", "path": "chain"}
    assert len(gathered) == 1


def test_collect_facts_recipe_fallback_when_chain_invalid():
    dossier = _dossier(chain=[
        EvidenceTraceStep.model_construct(tool="rm_rf", args={}, located="x")
    ])
    tool_client = _tool_client(
        get_file_content="body",
        inspect_change_impact=(
            '{"status": "confirmed", "subject_symbol_id": "S1", "relationships": []}'
        ),
    )
    facts, trace, _ = _collect_facts(
        [dossier], tool_client=tool_client,
        tag_by_candidate={"c1": RiskTag.GENERAL_REVIEW},
    )
    assert all(f.replay_status == "recipe" for f in facts["c1"])
    assert trace[0][0] == "candidate_evidence_path"
    assert json.loads(trace[0][1]) == {"candidate_id": "c1", "path": "recipe"}


def test_collect_facts_chain_tool_error_marks_failed():
    """回归:调用失败必须标 failed,不能被 located 分支抢先标 unverified。"""
    dossier = _dossier(chain=[
        EvidenceTraceStep(
            tool="get_file_content",
            args={"file_path": "src/A.java"},
            located="int x = 1;",
        )
    ])
    tool_client = MagicMock()
    tool_client.get_file_content.side_effect = RuntimeError("sandbox denied")
    facts, _, _ = _collect_facts(
        [dossier], tool_client=tool_client,
        tag_by_candidate={"c1": RiskTag.GENERAL_REVIEW},
    )
    fact = facts["c1"][0]
    assert fact.replay_status == "failed"
    assert fact.limitation.startswith("tool_error:")


def test_collect_facts_chain_located_mismatch_marks_unverified():
    dossier = _dossier(chain=[
        EvidenceTraceStep(
            tool="get_file_content",
            args={"file_path": "src/A.java"},
            located="int z = 1;",
        )
    ])
    tool_client = _tool_client(get_file_content="int x = 1;")
    facts, _, _ = _collect_facts(
        [dossier], tool_client=tool_client,
        tag_by_candidate={"c1": RiskTag.GENERAL_REVIEW},
    )
    assert facts["c1"][0].replay_status == "unverified"


def _graph_response(payload: str) -> Mock:
    return Mock(success=True, result=payload, as_tool_output=Mock())


def test_call_tool_graph_subject_mismatch():
    client = MagicMock()
    client.inspect_change_impact.return_value = _graph_response(
        '{"status": "confirmed", "subject_symbol_id": "S9", "relationships": []}'
    )
    raw, limitation = _call_tool(
        client, "inspect_change_impact", {"symbol_id": "S1"}
    )
    assert limitation == "graph_subject_mismatch"
    assert raw  # 原文保留,供分析层判断


def test_call_tool_graph_unknown():
    client = MagicMock()
    client.inspect_change_impact.return_value = _graph_response(
        '{"status": "unknown"}'
    )
    _, limitation = _call_tool(client, "inspect_change_impact", {"symbol_id": "S1"})
    assert limitation == "graph_unknown"


def test_call_tool_graph_partial_coverage_confirmed_passes():
    client = MagicMock()
    client.inspect_change_impact.return_value = _graph_response(
        '{"status": "confirmed", "subject_symbol_id": "S1",'
        ' "coverage": "partial", "relationships": []}'
    )
    raw, limitation = _call_tool(
        client, "inspect_change_impact", {"symbol_id": "S1"}
    )
    assert limitation == ""
    assert raw


@pytest.mark.parametrize("source_scope", ["MAIN", "GENERATED"])
def test_call_tool_graph_test_only_relationships_rejected_in_production_scope(
    source_scope: str,
):
    """生产 scope 下仅有 test 关系的图数据不能支撑语义(自旧 evidence.agent 迁移)。"""
    client = MagicMock()
    client.inspect_security_path.return_value = _graph_response(
        '{"status": "confirmed", "subject_symbol_id": "S1",'
        f' "source_scope": "{source_scope}", "relationships": [],'
        ' "test_relationships": [{"source_set": "TEST"}]}'
    )
    raw, limitation = _call_tool(
        client, "inspect_security_path", {"symbol_id": "S1"}
    )
    assert limitation == "graph_test_only_confirmation"
    assert raw  # 原文保留,供分析层降级为 insufficient


def test_call_tool_graph_test_scoped_relationships_pass_for_test_scope():
    """测试候选的 TEST scope 图数据原样透传,不做 test_only 降级。"""
    client = MagicMock()
    client.inspect_security_path.return_value = _graph_response(
        '{"status": "confirmed", "subject_symbol_id": "S1",'
        ' "source_scope": "TEST", "relationships": [],'
        ' "test_relationships": [{"source_set": "TEST"}]}'
    )
    raw, limitation = _call_tool(
        client, "inspect_security_path", {"symbol_id": "S1"}
    )
    assert limitation == ""
    assert raw


def test_verify_evidence_not_found_with_test_only_relations_is_analyzed():
    """回归锁:not_found 图响应不触发 confirmed-only 的 test_only 守卫,事实照常进关系分析。

    call 层守卫(graph_test_only_confirmation)只拦 confirmed + 空 relationships + 仅 test
    关系;not_found 表示 MAIN 中未命中,原始响应保留给分析层,mock 路径按有内容事实处理。
    """
    dossier = _dossier(chain=[
        EvidenceTraceStep(
            tool="inspect_change_impact",
            args={"symbol_id": "S1"},
            located="x",
        )
    ])
    tool_client = _tool_client(
        inspect_change_impact=(
            '{"status": "not_found", "subject_symbol_id": "S1",'
            ' "source_scope": "MAIN", "relationships": [],'
            ' "test_relationships": [{"source_set": "TEST"}]}'
        )
    )
    batch = verify_evidence(
        [dossier], tool_client=tool_client, analyst_llm=None,
        structured_method="function_calling", enabled_tools=None,
        tag_by_candidate={"c1": RiskTag.GENERAL_REVIEW},
    )
    (fact,) = batch.facts["c1"]
    assert fact.limitation == ""  # 守卫未命中,不是 graph_test_only_confirmation
    assert "not_found" in fact.raw
    (relation,) = batch.relations["c1"]
    assert relation.relation == "supports"  # mock 分析:not_found 事实仍进入分析
    assert relation.strength == "contextual"
    assert relation.limitation == "mock_mode_synthetic_relation"


def test_analyze_relations_mock_path_without_llm():
    facts = [
        CandidateFact(
            fact_id="f1", source="tool:get_file_content",
            raw="int x = 1;", replay_status="verified",
        ),
        CandidateFact(
            fact_id="f2", source="tool:x", raw="",
            replay_status="failed", limitation="tool_empty",
        ),
    ]
    relations = analyze_relations(
        dossier=_dossier(), facts=facts, tag=RiskTag.GENERAL_REVIEW,
        analyst_llm=None, structured_method="function_calling",
    )
    by_id = {r.fact_id: r for r in relations}
    assert by_id["f1"].relation == "supports"      # mock: 有内容的事实按支持处理
    assert by_id["f2"].relation == "insufficient"  # 带 limitation 的事实恒为不足


def test_verify_evidence_routes_chain_and_recipe():
    dossier = _dossier()
    tool_client = Mock()
    tool_client.get_file_content.return_value = Mock(
        success=True, result=None, as_tool_output=lambda: "body",
    )
    batch = verify_evidence(
        [dossier], tool_client=tool_client, analyst_llm=None,
        structured_method="function_calling", enabled_tools=None,
        tag_by_candidate={"c1": RiskTag.GENERAL_REVIEW},
    )
    assert set(batch.facts) == {"c1"}
    assert set(batch.relations) == {"c1"}


def test_verify_evidence_batch_metrics_payload_carries_replay_and_path_stats():
    """回归:evidence_batch_metrics 携带重放四态/链配方路径/工具调用计数,供 trace 仪表盘渲染。"""
    chain = EvidenceTraceStep(
        tool="get_file_content", args={"file_path": "src/A.java"}, located="int x = 1;",
    )
    dossier_chain = _dossier(chain=[chain])
    dossier_recipe = _dossier(line=30)
    dossier_recipe.candidate.id = "c2"
    tool_client = Mock()
    tool_client.get_file_content.return_value = Mock(
        success=True, result=None, as_tool_output=lambda: "int x = 1;\nvoid f() {}",
    )
    tool_client.inspect_change_impact.return_value = Mock(
        success=True,
        result=(
            '{"status": "confirmed", "subject_symbol_id": "S1",'
            ' "source_scope": "MAIN", "coverage": "complete",'
            ' "relationships": [], "test_relationships": []}'
        ),
    )
    batch = verify_evidence(
        [dossier_chain, dossier_recipe], tool_client=tool_client, analyst_llm=None,
        structured_method="function_calling", enabled_tools=None,
        tag_by_candidate={"c1": RiskTag.GENERAL_REVIEW, "c2": RiskTag.GENERAL_REVIEW},
    )
    payload = _batch_metrics_payload(batch)
    assert payload["candidates"] == 2
    assert payload["request_count"] == 2  # 跨候选去重后仅 2 次真实工具调用
    assert payload["fact_count"] == 3
    assert payload["replay_verified_count"] == 1   # 链重放引用命中
    assert payload["replay_unverified_count"] == 0
    assert payload["replay_failed_count"] == 0
    assert payload["recipe_fact_count"] == 2       # 配方兜底两条事实
    assert payload["chain_used"] == 1
    assert payload["recipe_fallback"] == 1
    assert payload["llm_analysis_calls"] == 0      # mock 档不调 LLM
    assert payload["fact_analysis_ms"] >= 0


def test_verify_evidence_batch_metrics_counts_llm_analysis_calls():
    """回归:关系分析真实发起 LLM 调用的候选数计入 llm_analysis_calls。"""
    dossier = _dossier()
    tool_client = Mock()
    tool_client.get_file_content.return_value = Mock(
        success=True, result=None, as_tool_output=lambda: "int x = 1;",
    )
    tool_client.inspect_change_impact.return_value = Mock(
        success=True,
        result=(
            '{"status": "confirmed", "subject_symbol_id": "S1",'
            ' "source_scope": "MAIN", "coverage": "complete",'
            ' "relationships": [], "test_relationships": []}'
        ),
    )
    analyst = MagicMock()
    analyst.with_structured_output.return_value = Mock(
        invoke=Mock(return_value=_RelationBatch(findings=[])),
    )
    batch = verify_evidence(
        [dossier], tool_client=tool_client, analyst_llm=analyst,
        structured_method="function_calling", enabled_tools=None,
        tag_by_candidate={"c1": RiskTag.GENERAL_REVIEW},
    )
    assert _batch_metrics_payload(batch)["llm_analysis_calls"] == 1


def test_verify_evidence_without_tool_client_uses_patch_facts():
    dossier = _dossier()
    batch = verify_evidence(
        [dossier], tool_client=None, analyst_llm=None,
        structured_method="function_calling", enabled_tools=None,
        tag_by_candidate={"c1": RiskTag.GENERAL_REVIEW},
    )
    assert batch.facts["c1"][0].source == "diff"
    assert batch.facts["c1"][0].replay_status == "recipe"


def test_analyze_relations_guard_scan_prior_wins():
    from codeguard_agent.pipeline.evidence.guard_scan import scan_guard_fact

    # symbol_context 形状按 test_guard_scan.py 夹具:锚定 update() 方法声明块
    dossier = _dossier(
        line=2,
        facts=[
            ContextFact(
                source="tool:resolve_change_context",
                kind="symbol_context",
                content=(
                    '{"symbol_id": "java:Service#update()", "kind": "method",'
                    ' "start_line": 1, "end_line": 2}'
                ),
            )
        ],
    )
    fact = CandidateFact(
        fact_id="f1", source="tool:get_file_content",
        raw='@PreAuthorize("hasRole(\'ADMIN\')")\npublic void update() {}',
        replay_status="verified",
    )
    prior = scan_guard_fact(dossier, fact, RiskTag.AUTHORIZATION)
    assert prior is not None  # 前置:scanner 应命中
    relations = analyze_relations(
        dossier=dossier, facts=[fact], tag=RiskTag.AUTHORIZATION,
        analyst_llm=None, structured_method="function_calling",
    )
    assert relations[0].relation == "contradicts"
    assert relations[0].strength == "direct"


def test_relation_batch_accepts_stringified_findings():
    """兼容部分 OpenAI 端点把数组参数序列化为 JSON 字符串(parse_stringified_findings 分支)。"""
    wrapped = _RelationBatch.model_validate(
        {"findings": json.dumps({"findings": [{"fact_id": "f1", "relation": "supports"}]})}
    )
    assert wrapped.findings == [{"fact_id": "f1", "relation": "supports"}]

    bare = _RelationBatch.model_validate(
        {"findings": json.dumps([{"fact_id": "f2", "relation": "insufficient"}])}
    )
    assert bare.findings == [{"fact_id": "f2", "relation": "insufficient"}]


def test_analyze_relations_llm_findings_sanitized():
    """回归:LLM 输出非法 relation/strength/空 observation 时逐项降级,不拖累整批。"""
    analyst = MagicMock()
    analyst.with_structured_output.return_value = Mock(
        invoke=Mock(return_value=_RelationBatch(findings=[
            {
                "fact_id": "f1",
                "relation": "weird",
                "strength": "absolute",
                "observation": "",
            },
            {
                "fact_id": "f2",
                "relation": "supports",
                "strength": "direct",
                "observation": "",
            },
            {
                "fact_id": "f3",
                "relation": ["unhashable"],  # 非 str:isinstance 守卫,不得拖垮整批
                "strength": "direct",
                "observation": "y",
            },
        ])),
    )
    facts = [
        CandidateFact(fact_id="f1", source="tool:x", raw="a"),
        CandidateFact(fact_id="f2", source="tool:x", raw="b"),
        CandidateFact(fact_id="f3", source="tool:x", raw="c"),
    ]
    relations = analyze_relations(
        dossier=_dossier(), facts=facts, tag=RiskTag.GENERAL_REVIEW,
        analyst_llm=analyst, structured_method="function_calling",
    )
    by_id = {r.fact_id: r for r in relations}
    assert by_id["f1"].relation == "insufficient"
    assert by_id["f1"].strength == "contextual"
    assert by_id["f1"].limitation == "analysis_unclear"
    assert by_id["f2"].relation == "insufficient"
    assert by_id["f2"].limitation == "observation_missing"
    assert by_id["f3"].relation == "insufficient"
    assert by_id["f3"].limitation == "analysis_unclear"


def test_analyze_relations_llm_failure_degrades_all_analyzable():
    """回归:LLM 结构化解码异常时,可分析事实全部降级 insufficient,不误杀。"""
    analyst = MagicMock()
    analyst.with_structured_output.side_effect = RuntimeError("structured output down")
    facts = [
        CandidateFact(fact_id="f1", source="tool:x", raw="a"),
        CandidateFact(fact_id="f2", source="tool:x", raw="", limitation="tool_empty"),
    ]
    relations = analyze_relations(
        dossier=_dossier(), facts=facts, tag=RiskTag.GENERAL_REVIEW,
        analyst_llm=analyst, structured_method="function_calling",
    )
    by_id = {r.fact_id: r for r in relations}
    assert by_id["f1"].relation == "insufficient"
    assert by_id["f1"].limitation == "analysis_failed_or_missing"
    assert by_id["f2"].limitation == "tool_empty"  # 带 limitation 不交 LLM


def test_verify_evidence_parallel_failure_marks_insufficient(monkeypatch):
    """回归:并行分析单项返回 None 时按 insufficient 兜底,不炸管线。"""
    import codeguard_agent.pipeline.evidence.verifier as verifier_module

    monkeypatch.setattr(
        verifier_module, "run_bounded_parallel",
        lambda items, fn, **kwargs: [None] * len(items),
    )
    dossier = _dossier()
    batch = verify_evidence(
        [dossier], tool_client=None, analyst_llm=None,
        structured_method="function_calling", enabled_tools=None,
        tag_by_candidate={"c1": RiskTag.GENERAL_REVIEW},
    )
    (relation,) = batch.relations["c1"]
    assert relation.relation == "insufficient"
    assert relation.limitation == "parallel_analysis_failed"


def test_relation_payload_truncates_long_raw():
    fact = CandidateFact(
        fact_id="f1", source="tool:x", raw="x" * 3000, replay_status="verified",
    )
    payload = json.loads(_relation_payload(_dossier(), [fact]))
    assert payload["facts"][0]["raw"] == "x" * 2000
    assert payload["facts"][0]["raw_truncated"] is True


# ────────────────────────────────────────────────────────────────
# 图工具关系断言匹配与图响应压缩(2026-08-17 单 case 评测 TP=0 修复)
# ────────────────────────────────────────────────────────────────

_GRAPH_SUBJECT = (
    "java:org.codehaus.plexus.util.cli.shell.Shell#getRawCommandLine"
    "(java.lang.String, java.lang.String[])"
)
_GRAPH_GET_CMD_ID = (
    "java:org.codehaus.plexus.util.cli.shell.Shell#getCommandLine"
    "(java.lang.String, java.lang.String[])"
)
_GRAPH_CMDSHELL_GET_CMD_ID = (
    "java:org.codehaus.plexus.util.cli.shell.CmdShell#getCommandLine"
    "(java.lang.String, java.lang.String[])"
)
_GRAPH_GET_SHELL_CMD_ID = (
    "java:org.codehaus.plexus.util.cli.shell.Shell#getShellCommandLine"
    "(java.lang.String[])"
)
_GRAPH_CMDLINE_GET_SHELL_ID = (
    "java:org.codehaus.plexus.util.cli.Commandline#getShellCommandline()"
)

_GRAPH_RAW = json.dumps(
    {
        "status": "confirmed",
        "coverage": "partial",
        "source_scope": "MAIN",
        "subject_symbol_id": _GRAPH_SUBJECT,
        "symbols": [
            {
                "id": _GRAPH_SUBJECT, "kind": "METHOD",
                "file": "src/main/java/org/codehaus/plexus/util/cli/shell/Shell.java",
                "startLine": 132, "endLine": 178,
                "signature": "List<String> getRawCommandLine(String executable, String[] arguments)",
                "ownerId": "java:org.codehaus.plexus.util.cli.shell.Shell",
                "annotations": [], "source_set": "MAIN",
            },
            {
                "id": _GRAPH_GET_CMD_ID, "kind": "METHOD",
                "file": "src/main/java/org/codehaus/plexus/util/cli/shell/Shell.java",
                "startLine": 127, "endLine": 130,
                "signature": "List<String> getCommandLine(String executable, String[] arguments)",
                "ownerId": "java:org.codehaus.plexus.util.cli.shell.Shell",
                "annotations": [], "source_set": "MAIN",
            },
            {
                "id": _GRAPH_CMDSHELL_GET_CMD_ID, "kind": "METHOD",
                "file": "src/main/java/org/codehaus/plexus/util/cli/shell/CmdShell.java",
                "startLine": 80, "endLine": 88,
                "signature": "List<String> getCommandLine(String executable, String[] arguments)",
                "ownerId": "java:org.codehaus.plexus.util.cli.shell.CmdShell",
                "annotations": [], "source_set": "MAIN",
            },
            {
                "id": _GRAPH_GET_SHELL_CMD_ID, "kind": "METHOD",
                "file": "src/main/java/org/codehaus/plexus/util/cli/shell/Shell.java",
                "startLine": 266, "endLine": 285,
                "signature": "List<String> getShellCommandLine(String[] arguments)",
                "ownerId": "java:org.codehaus.plexus.util.cli.shell.Shell",
                "annotations": [], "source_set": "MAIN",
            },
            {
                "id": _GRAPH_CMDLINE_GET_SHELL_ID, "kind": "METHOD",
                "file": "src/main/java/org/codehaus/plexus/util/cli/Commandline.java",
                "startLine": 501, "endLine": 520,
                "signature": "String getShellCommandline()",
                "ownerId": "java:org.codehaus.plexus.util.cli.Commandline",
                "annotations": [], "source_set": "MAIN",
            },
        ],
        "relationships": [
            {
                "sourceId": _GRAPH_GET_CMD_ID, "targetId": _GRAPH_SUBJECT,
                "kind": "CALLS",
                "file": "src/main/java/org/codehaus/plexus/util/cli/shell/Shell.java",
                "line": 129, "resolution": "RESOLVED",
                "extractor": "javaparser", "source_set": "MAIN",
            },
            {
                "sourceId": _GRAPH_CMDSHELL_GET_CMD_ID, "targetId": _GRAPH_GET_CMD_ID,
                "kind": "CALLS",
                "file": "src/main/java/org/codehaus/plexus/util/cli/shell/CmdShell.java",
                "line": 84, "resolution": "RESOLVED",
                "extractor": "javaparser", "source_set": "MAIN",
            },
            {
                "sourceId": _GRAPH_GET_SHELL_CMD_ID, "targetId": _GRAPH_GET_CMD_ID,
                "kind": "CALLS",
                "file": "src/main/java/org/codehaus/plexus/util/cli/shell/Shell.java",
                "line": 281, "resolution": "RESOLVED",
                "extractor": "javaparser", "source_set": "MAIN",
            },
            {
                "sourceId": _GRAPH_CMDLINE_GET_SHELL_ID, "targetId": _GRAPH_GET_SHELL_CMD_ID,
                "kind": "CALLS",
                "file": "src/main/java/org/codehaus/plexus/util/cli/Commandline.java",
                "line": 506, "resolution": "RESOLVED",
                "extractor": "javaparser", "source_set": "MAIN",
            },
        ],
        "main_relationships": [],
        "test_relationships": [
            {
                "sourceId": "java:org.codehaus.plexus.util.cli.TestRunner#run()",
                "targetId": _GRAPH_GET_CMD_ID,
                "kind": "CALLS",
                "file": "src/test/java/org/codehaus/plexus/util/cli/TestRunner.java",
                "line": 10, "resolution": "RESOLVED",
                "extractor": "javaparser", "source_set": "TEST",
            },
        ],
        "generated_relationships": [],
        "limitations": [],
    }
)

# 实测 trace 中审查员输出的 located 原文(人话转述,内容 100% 真实)
_GRAPH_PARAPHRASE = (
    "relationships: getCommandLine -> getRawCommandLine (line 129), "
    "CmdShell.getCommandLine -> Shell.getCommandLine (CmdShell.java:84), "
    "getShellCommandLine -> getCommandLine (Shell.java:281), "
    "Commandline.getShellCommandline -> getShellCommandLine (Commandline.java:506)"
)


class TestGraphAssertionParsing:
    def test_parse_assertions_extracts_pairs_line_and_file(self):
        assertions = _parse_assertions(_GRAPH_PARAPHRASE)
        assert len(assertions) == 4
        a0, a1, a2, a3 = assertions
        assert (a0.source, a0.target, a0.line) == (
            "getCommandLine", "getRawCommandLine", 129,
        )
        assert a0.file == ""
        assert (a1.source, a1.target) == (
            "CmdShell.getCommandLine", "Shell.getCommandLine",
        )
        assert (a1.file, a1.line) == ("CmdShell.java", 84)
        assert (a2.file, a2.line) == ("Shell.java", 281)
        assert (a3.file, a3.line) == ("Commandline.java", 506)

    def test_parse_assertions_accepts_hash_separator(self):
        located = "Shell#getCommandLine -> Shell#getRawCommandLine (line 129)"
        assertions = _parse_assertions(located)
        assert len(assertions) == 1
        assert (assertions[0].source, assertions[0].target) == (
            "Shell.getCommandLine", "Shell.getRawCommandLine",
        )

    def test_parse_assertions_empty_for_verbatim(self):
        assert _parse_assertions('"targetId": "java:org.codehaus"') == []


class TestGraphAssertionsMatch:
    def test_real_human_paraphrase_hits(self):
        """核心回归:实测 trace 的人话转述必须命中(TP=0 的直接修复)。"""
        assert _graph_assertions_match(_GRAPH_PARAPHRASE, _GRAPH_RAW) is True

    def test_line_tolerance_within_and_beyond(self):
        assert _graph_assertions_match(
            "getCommandLine -> getRawCommandLine (line 130)", _GRAPH_RAW,
        ) is True
        assert _graph_assertions_match(
            "getCommandLine -> getRawCommandLine (line 131)", _GRAPH_RAW,
        ) is True
        assert _graph_assertions_match(
            "getCommandLine -> getRawCommandLine (line 133)", _GRAPH_RAW,
        ) is False

    def test_fabricated_edge_unverified(self):
        """幻觉内核:编造的调用边核对不上。"""
        assert _graph_assertions_match(
            "FakeFactory.build -> getRawCommandLine (line 129)", _GRAPH_RAW,
        ) is False

    def test_assertion_without_line_hits(self):
        assert _graph_assertions_match(
            "CmdShell.getCommandLine -> Shell.getCommandLine", _GRAPH_RAW,
        ) is True

    def test_partial_hit_is_verified(self):
        """裁决决策:至少一条断言命中即 verified。"""
        located = (
            "getCommandLine -> getRawCommandLine (line 129), "
            "FakeFoo.run -> Shell.getCommandLine, "
            "getShellCommandLine -> NoSuchMethod (line 999)"
        )
        assert _graph_assertions_match(located, _GRAPH_RAW) is True

    def test_reversed_direction_unverified(self):
        assert _graph_assertions_match(
            "getRawCommandLine -> getCommandLine", _GRAPH_RAW,
        ) is False

    def test_file_qualifier_hit_and_mismatch(self):
        assert _graph_assertions_match(
            "CmdShell.getCommandLine -> Shell.getCommandLine (CmdShell.java:84)",
            _GRAPH_RAW,
        ) is True
        assert _graph_assertions_match(
            "CmdShell.getCommandLine -> Shell.getCommandLine (Other.java:84)",
            _GRAPH_RAW,
        ) is False

    def test_verbatim_fragment_falls_back_to_substring(self):
        located = '"targetId": "java:org.codehaus.plexus.util.cli.shell.Shell#getRawCommandLine(java.lang.String, java.lang.String[])"'
        assert _graph_assertions_match(located, _GRAPH_RAW) is True
        assert _graph_assertions_match(
            '"targetId": "java:com.example.NotInGraph#x()"', _GRAPH_RAW,
        ) is False

    def test_mixed_verbatim_fragment_falls_back(self):
        located = (
            "FakeFoo.run -> NoSuch.method (line 999), "
            '"targetId": "java:org.codehaus.plexus.util.cli.shell.Shell#getRawCommandLine(java.lang.String, java.lang.String[])"'
        )
        assert _graph_assertions_match(located, _GRAPH_RAW) is True

    def test_non_json_raw_unverified(self):
        assert _graph_assertions_match(
            "getCommandLine -> getRawCommandLine (line 129)", "not a json",
        ) is False

    def test_empty_located_unverified(self):
        assert _graph_assertions_match("   ", _GRAPH_RAW) is False

    def test_edge_in_test_relationships_hits(self):
        assert _graph_assertions_match(
            "TestRunner.run -> Shell.getCommandLine", _GRAPH_RAW,
        ) is True


class TestSideMatch:
    def test_nested_class_and_constructor(self):
        nested_id = (
            "java:org.codehaus.plexus.util.cli.Commandline.Argument"
            "#setLine(java.lang.String)"
        )
        assert _side_match("Commandline.Argument.setLine", nested_id) is True
        assert _side_match("Argument.setLine", nested_id) is True
        assert _side_match("OtherClass.setLine", nested_id) is False
        constructor_id = (
            "java:org.codehaus.plexus.util.cli.Commandline"
            "#<init>Commandline(java.lang.String)"
        )
        assert _side_match("Commandline", constructor_id) is True
        type_node_id = "java:org.codehaus.plexus.util.cli.Commandline"
        assert _side_match("Commandline", type_node_id) is True

    def test_method_only_token(self):
        assert _side_match("getCommandLine", _GRAPH_GET_CMD_ID) is True
        assert _side_match("getCommandline", _GRAPH_GET_CMD_ID) is False


class TestGraphAssertionsIntegration:
    def test_collect_facts_graph_assertion_marks_verified(self):
        dossier = _dossier(chain=[
            EvidenceTraceStep(
                tool="inspect_change_impact",
                args={"symbol_id": _GRAPH_SUBJECT},
                located=_GRAPH_PARAPHRASE,
            )
        ])
        tool_client = Mock()
        tool_client.inspect_change_impact.return_value = Mock(
            success=True, result=_GRAPH_RAW,
        )
        facts, _, _ = _collect_facts(
            [dossier], tool_client=tool_client,
            tag_by_candidate={"c1": RiskTag.GENERAL_REVIEW},
        )
        (fact,) = facts["c1"]
        assert fact.replay_status == "verified"

    def test_collect_facts_graph_fabricated_assertion_marks_unverified(self):
        dossier = _dossier(chain=[
            EvidenceTraceStep(
                tool="inspect_change_impact",
                args={"symbol_id": _GRAPH_SUBJECT},
                located="FakeFactory.build -> getRawCommandLine (line 129)",
            )
        ])
        tool_client = Mock()
        tool_client.inspect_change_impact.return_value = Mock(
            success=True, result=_GRAPH_RAW,
        )
        facts, _, _ = _collect_facts(
            [dossier], tool_client=tool_client,
            tag_by_candidate={"c1": RiskTag.GENERAL_REVIEW},
        )
        (fact,) = facts["c1"]
        assert fact.replay_status == "unverified"


class TestGraphSummary:
    def test_keeps_header_and_filters_fields(self):
        summary = json.loads(_graph_summary(_GRAPH_RAW))
        for key in (
            "status", "coverage", "source_scope",
            "subject_symbol_id", "limitations",
        ):
            assert key in summary
        assert set(summary["symbols"][0].keys()) == {
            "id", "kind", "file", "startLine", "endLine",
        }
        assert set(summary["relationships"][0].keys()) == {
            "sourceId", "targetId", "kind", "file", "line",
        }
        for dropped in (
            '"signature"', '"annotations"', '"ownerId"',
            '"source_set"', '"resolution"', '"extractor"',
            '"main_relationships"', '"test_relationships"',
            '"generated_relationships"',
        ):
            assert dropped not in _graph_summary(_GRAPH_RAW)

    def test_caps_length_and_deterministic(self):
        big = json.loads(_GRAPH_RAW)
        big["symbols"] = [dict(big["symbols"][0]) for _ in range(60)]
        big["relationships"] = [dict(big["relationships"][0]) for _ in range(200)]
        big_raw = json.dumps(big)
        summary = _graph_summary(big_raw)
        assert len(summary) <= 8000
        assert _graph_summary(big_raw) == summary  # 确定性
        parsed = json.loads(summary)
        assert "symbols" not in parsed          # 超限时符号先让位
        assert parsed["relationships"]          # 关系是本次修复的核心,保留

    def test_non_json_falls_back_to_slice(self):
        assert _graph_summary("not a json") == "not a json"
        assert len(_graph_summary("x" * 9000)) == 8000

    def test_empty_primary_includes_fallback_arrays(self):
        bare = json.loads(_GRAPH_RAW)
        bare["relationships"] = []
        summary = json.loads(_graph_summary(json.dumps(bare)))
        assert summary["relationships"] == []
        assert summary["test_relationships"]  # not_found 透传路径保护

    def test_relation_payload_graph_fact_gets_summary(self):
        fact = CandidateFact(
            fact_id="f1", source="tool:inspect_change_impact",
            raw=_GRAPH_RAW, replay_status="unverified",
        )
        payload = json.loads(_relation_payload(_dossier(), [fact]))
        entry = payload["facts"][0]
        summary = json.loads(entry["raw"])
        assert "signature" not in json.dumps(summary)
        assert entry["raw_truncated"] is True
        assert entry["replay_status"] == "unverified"
