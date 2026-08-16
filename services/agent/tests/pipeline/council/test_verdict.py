"""verdict 门控、终审与批量裁决测试(ADR-046)。"""
from codeguard_agent.models.council import (
    CandidateDirectAssessment,
    CandidateIssue,
    FactRelation,
)
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import ReviewTask, RiskTag
from codeguard_agent.pipeline.council.dedup import CandidateGroup
from codeguard_agent.pipeline.council.verdict import (
    VerdictBatch,
    consolidate_groups,
    gate_candidate,
    judge_direct,
    judge_with_evidence,
    synthesize_verdict,
)
from codeguard_agent.pipeline.evidence.planner import (
    CandidateBindingFailure,
    CandidateDossier,
    DossierAssembly,
)


def _relation(relation, strength="contextual"):
    return FactRelation(
        fact_id="f1", relation=relation, strength=strength,
        observation="obs" if relation in {"supports", "contradicts"} else "",
        limitation="" if relation in {"supports", "contradicts"} else "lim",
    )


def test_gate_direct_counter_drops():
    code, _ = gate_candidate([
        _relation("supports"), _relation("contradicts", strength="direct"),
    ])
    assert code == "direct_counter_evidence"


def test_gate_no_facts_drops():
    code, _ = gate_candidate([])
    assert code == "evidence_insufficient"


def test_gate_all_insufficient_drops():
    code, _ = gate_candidate([_relation("insufficient")])
    assert code == "evidence_insufficient"


def test_gate_no_support_drops():
    code, _ = gate_candidate([_relation("contradicts")])  # contextual 反证不构成直接反证
    assert code == "no_supporting_evidence"


def test_gate_supported_passes():
    assert gate_candidate([_relation("supports")]) is None


def test_gate_supported_with_contextual_counter_passes():
    # supports + contextual contradicts 放行给终审,不触发 no_supporting_evidence
    assert gate_candidate([
        _relation("supports"), _relation("contradicts"),
    ]) is None


class _FakeStructured:
    def __init__(self, result):
        self._result = result

    def invoke(self, _messages):
        return self._result


class _FakeLLM:
    def __init__(self, result):
        self._result = result

    def with_structured_output(self, _schema, method=None):
        return _FakeStructured(self._result)


class _RaisingLLM:
    def with_structured_output(self, _schema, method=None):
        raise RuntimeError("structured output unavailable")


def _verdict_dossier() -> CandidateDossier:
    candidate = CandidateIssue(
        id="c1", task_id="t1", source_agent="threat_model",
        file="A.java", line=10, type="t", severity_proposal=Severity.WARNING,
        claim="claim", confidence=0.8,
    )
    task = ReviewTask(id="t1", file="A.java", patch="+x")
    return CandidateDossier(candidate=candidate, task=task, context_bundle=None,
                            requests=(), notes=())


def test_synthesize_returns_unified_assessment():
    raw = CandidateDirectAssessment(
        candidate_id="C001", action="keep", severity=Severity.WARNING,
        cited_fact_ids=("f1",), reason="有支持证据",
    )
    result = synthesize_verdict(
        _verdict_dossier(),
        [FactRelation(fact_id="f1", relation="supports", observation="上游可达")],
        judge_llm=_FakeLLM(raw), structured_method="function_calling", max_retries=1,
    )
    expected = raw.model_copy(update={"candidate_id": "c1"})
    assert result == expected
    assert result.cited_fact_ids == ("f1",)


def test_synthesize_none_falls_back_to_none():
    assert synthesize_verdict(
        _verdict_dossier(), [], judge_llm=_FakeLLM(None),
        structured_method="function_calling", max_retries=1,
    ) is None


def test_synthesize_wrong_candidate_id_returns_none():
    wrong = CandidateDirectAssessment(
        candidate_id="C002", action="keep", severity=Severity.WARNING, reason="x",
    )
    assert synthesize_verdict(
        _verdict_dossier(), [], judge_llm=_FakeLLM(wrong),
        structured_method="function_calling", max_retries=1,
    ) is None


def test_synthesize_no_llm_returns_none():
    assert synthesize_verdict(
        _verdict_dossier(), [], judge_llm=None,
        structured_method="function_calling", max_retries=1,
    ) is None


def test_synthesize_structured_failure_returns_none():
    assert synthesize_verdict(
        _verdict_dossier(), [], judge_llm=_RaisingLLM(),
        structured_method="function_calling", max_retries=1,
    ) is None


def _group_with(candidates) -> CandidateGroup:
    first = candidates[0]
    return CandidateGroup(
        id="g1", members=tuple(candidates), primary_risk_tag=RiskTag.GENERAL_REVIEW,
        severity_proposal=first.severity_proposal, confidence=0.8,
        shared_root_cause="shared", shared_behavior="shared", shared_fix="shared",
    )


def _assembly(*dossiers) -> DossierAssembly:
    return DossierAssembly(dossiers=tuple(dossiers), failures=(), trace=())


def test_judge_with_evidence_invalid_binding_drops():
    candidate = _verdict_dossier().candidate
    assembly = DossierAssembly(
        dossiers=(), failures=(CandidateBindingFailure(candidate, "no task match"),),
        trace=(),
    )
    batch = judge_with_evidence(
        assembly, {}, judge_llm=None,
        structured_method="function_calling", max_retries=1,
    )
    assert [v.action for v in batch.verdicts] == ["drop"]
    assert batch.verdicts[0].reason_code == "invalid_candidate_binding"
    assert batch.final_issues == []


def test_judge_direct_invalid_binding_drops():
    candidate = _verdict_dossier().candidate
    assembly = DossierAssembly(
        dossiers=(), failures=(CandidateBindingFailure(candidate, "no task match"),),
        trace=(),
    )
    batch = judge_direct(
        assembly, judge_llm=None,
        structured_method="function_calling", max_retries=1,
    )
    assert [v.action for v in batch.verdicts] == ["drop"]
    assert batch.verdicts[0].reason_code == "invalid_candidate_binding"


def test_judge_with_evidence_gate_drops_without_llm():
    dossier = _verdict_dossier()
    batch = judge_with_evidence(
        _assembly(dossier),
        {"c1": []},  # 无任何关系 → gate ②
        judge_llm=_FakeLLM(None), structured_method="function_calling", max_retries=1,
    )
    assert [v.action for v in batch.verdicts] == ["drop"]
    assert batch.verdicts[0].reason_code == "evidence_insufficient"


def test_judge_with_evidence_llm_failure_keeps_proposal_severity():
    dossier = _verdict_dossier()
    batch = judge_with_evidence(
        _assembly(dossier),
        {"c1": [FactRelation(fact_id="f1", relation="supports", observation="可达")]},
        judge_llm=_FakeLLM(None), structured_method="function_calling", max_retries=1,
    )
    assert batch.verdicts[0].action == "keep"
    assert batch.verdicts[0].resolved_severity == Severity.WARNING
    assert batch.final_issues[0].severity == Severity.WARNING


def test_judge_direct_keeps_and_uses_assessment_severity():
    dossier = _verdict_dossier()
    assessment = CandidateDirectAssessment(
        candidate_id="C001", action="keep", severity=Severity.CRITICAL,
        reason="直接可见", cited_fact_ids=(),
    )
    batch = judge_direct(
        _assembly(dossier),
        judge_llm=_FakeLLM(assessment), structured_method="function_calling", max_retries=1,
    )
    assert batch.verdicts[0].action == "keep"
    assert batch.final_issues[0].severity == Severity.CRITICAL


def test_judge_direct_llm_unavailable_keeps_proposal():
    dossier = _verdict_dossier()
    batch = judge_direct(
        _assembly(dossier),
        judge_llm=_FakeLLM(None), structured_method="function_calling", max_retries=1,
    )
    assert batch.verdicts[0].action == "keep"
    assert batch.final_issues[0].severity == Severity.WARNING


def test_consolidate_ungrouped_passes_through():
    candidate = _verdict_dossier().candidate
    issue = candidate.to_issue()
    batch = VerdictBatch()
    consolidate_groups(batch, [(candidate.id, issue)], ())
    assert batch.final_candidate_ids == [candidate.id]
    assert batch.final_issues == [issue]


def test_consolidate_shape_mismatch_splits_back():
    candidate = _verdict_dossier().candidate
    other = candidate.model_copy(update={"id": "c2", "line": 12})
    group = _group_with([candidate, other])
    batch = VerdictBatch()
    consolidate_groups(
        batch,
        [
            (candidate.id, candidate.to_issue()),
            (other.id, other.to_issue().model_copy(update={"severity": Severity.CRITICAL})),
        ],
        [group],
    )
    assert batch.final_candidate_ids == [candidate.id, other.id]
    assert len(batch.final_issues) == 2
    assert any(event == "candidate_group_split" for event, _ in batch.trace)


def test_consolidate_shape_match_merges_semantics():
    candidate = _verdict_dossier().candidate  # line=10, type="t", claim="claim", conf=0.8
    other = candidate.model_copy(update={
        "id": "c2", "line": 12, "claim": "claim2", "confidence": 0.6,
        "suggestion": "建议",
    })
    group = _group_with([candidate, other])
    batch = VerdictBatch()
    consolidate_groups(
        batch,
        [(candidate.id, candidate.to_issue()), (other.id, other.to_issue())],
        [group],
    )
    assert batch.final_candidate_ids == ["c1"]
    assert len(batch.final_issues) == 1
    merged = batch.final_issues[0]
    assert merged.line == 10  # 最小正行号
    assert merged.type == "t"  # 相同类型去重
    assert merged.message == "claim；claim2"  # 消息去重拼接
    assert merged.suggestion == "建议"  # 空建议被过滤
    assert merged.confidence == 0.6  # 置信度取 min
    assert any(event == "candidate_group_consolidated" for event, _ in batch.trace)


def test_consolidate_partial_support_keeps_only_kept_member():
    candidate = _verdict_dossier().candidate
    other = candidate.model_copy(update={"id": "c2", "line": 12})
    group = _group_with([candidate, other])
    batch = VerdictBatch()
    consolidate_groups(batch, [(other.id, other.to_issue())], [group])
    assert batch.final_candidate_ids == [other.id]
    assert len(batch.final_issues) == 1
    assert batch.final_issues[0].line == 12
