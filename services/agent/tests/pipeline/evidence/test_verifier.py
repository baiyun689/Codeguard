"""evidence_verifier 链校验与固定配方测试(ADR-046)。"""
from __future__ import annotations

from unittest.mock import MagicMock, Mock

from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.schemas import EvidenceTraceStep, Severity
from codeguard_agent.models.tasks import (
    ContextFact,
    ReviewTask,
    RiskTag,
    TaskContextBundle,
)
from codeguard_agent.pipeline.evidence.planner import CandidateDossier
from codeguard_agent.pipeline.evidence.verifier import (
    _collect_facts,
    _located_match,
    _symbol_id,
    recipe_calls,
    replay_calls,
    validate_chain,
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
    return CandidateDossier(candidate=candidate, task=task, context_bundle=bundle,
                            requests=(), notes=())


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
        context_bundle=None, requests=(), notes=(),
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
    assert len(trace) == 1 and trace[0][0] == "evidence_tool_called"
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
    facts, _, _ = _collect_facts(
        [dossier], tool_client=tool_client,
        tag_by_candidate={"c1": RiskTag.GENERAL_REVIEW},
    )
    assert all(f.replay_status == "recipe" for f in facts["c1"])
