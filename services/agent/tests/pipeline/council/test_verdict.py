"""verdict 门控测试(ADR-046)。"""
from codeguard_agent.models.council import FactRelation
from codeguard_agent.pipeline.council.verdict import gate_candidate


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


from codeguard_agent.models.council import (  # noqa: E402
    CandidateDirectAssessment,
    CandidateIssue,
    FactRelation,
)
from codeguard_agent.models.schemas import Severity  # noqa: E402
from codeguard_agent.models.tasks import ReviewTask  # noqa: E402
from codeguard_agent.pipeline.council.verdict import synthesize_verdict  # noqa: E402
from codeguard_agent.pipeline.evidence.planner import CandidateDossier  # noqa: E402


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
    expected = CandidateDirectAssessment(
        candidate_id="C001", action="keep", severity=Severity.WARNING,
        cited_fact_ids=("f1",), reason="有支持证据",
    )
    result = synthesize_verdict(
        _verdict_dossier(),
        [FactRelation(fact_id="f1", relation="supports", observation="上游可达")],
        judge_llm=_FakeLLM(expected), structured_method="function_calling", max_retries=1,
    )
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
