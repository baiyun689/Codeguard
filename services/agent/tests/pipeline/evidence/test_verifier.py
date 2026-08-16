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
    _located_match,
    _RelationBatch,
    _relation_payload,
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
        lambda items, fn: [None] * len(items),
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
