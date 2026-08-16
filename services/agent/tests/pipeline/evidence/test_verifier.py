"""evidence_verifier 链校验与固定配方测试(ADR-046)。"""
from __future__ import annotations

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
    _symbol_id,
    recipe_calls,
    validate_chain,
)


def _dossier(line=10, symbol="S1", task_file="src/A.java") -> CandidateDossier:
    candidate = CandidateIssue(
        id="c1", task_id="t1", source_agent="threat_model",
        file=task_file, line=line, type="t",
        severity_proposal=Severity.WARNING, claim="claim", confidence=0.8,
    )
    task = ReviewTask(id="t1", file=task_file, patch="+x", changed_lines=[line])
    bundle = TaskContextBundle(
        task_id="t1",
        facts=[
            ContextFact(
                source="tool:resolve_change_context",
                kind="symbol_context",
                content='{"symbol_id": "%s", "start_line": 5, "end_line": 20}' % symbol,
            )
        ],
    )
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
