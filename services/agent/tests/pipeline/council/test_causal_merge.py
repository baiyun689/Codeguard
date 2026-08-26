"""Cause/Effect 语义合并的保守合并规则测试。"""

from __future__ import annotations

from codeguard_agent.models.council import (
    CandidateIssue,
    CausalAnalysisBatch,
    CausalComparison,
    CausalProfile,
)
from codeguard_agent.models.evidence import (
    CandidateVerification,
    EvidenceSourceKind,
    EvidenceValidationStatus,
    VerifiedEvidence,
)
from codeguard_agent.models.schemas import Issue, Severity
from codeguard_agent.pipeline.council.causal_merge import merge_survivors


def _candidate(
    cid: str,
    claim: str,
    *,
    task_id: str = "task-1",
    file: str = "OrderService.java",
) -> CandidateIssue:
    return CandidateIssue(
        id=cid,
        task_id=task_id,
        source_agent="behavior",
        file=file,
        line=10,
        type="logic",
        claim=claim,
        suggestion="修复问题",
    )


def _issue(candidate: CandidateIssue) -> Issue:
    return candidate.to_issue(Severity.WARNING)


def _verification(candidate: CandidateIssue) -> CandidateVerification:
    return CandidateVerification(
        candidate_id=candidate.id,
        source_kinds={EvidenceSourceKind.TASK_PATCH},
        valid_evidence=[
            VerifiedEvidence(
                artifact_id=f"ev-{candidate.id}",
                source_kind=EvidenceSourceKind.TASK_PATCH,
                content="verified patch fact",
                validation_status=EvidenceValidationStatus.VALID,
            )
        ],
        grounding_status="grounded",
        eligible_for_judge=True,
    )


def _profile(candidate_id: str) -> CausalProfile:
    return CausalProfile(
        candidate_id=candidate_id,
        defect_mechanism="same mechanism",
        location=["OrderService.java:10"],
        trigger="same trigger",
        runtime_consequence="same consequence",
        affected_scope="same scope",
        observable_behavior="same behavior",
        cause_evidence_ids=[f"ev-{candidate_id}"],
        effect_evidence_ids=[f"ev-{candidate_id}"],
    )


class _FakeLLM:
    def __init__(self, result: CausalAnalysisBatch | None):
        self.result = result
        self.calls = 0

    def with_structured_output(self, _schema, method=None):
        return self

    def invoke(self, _messages):
        self.calls += 1
        return self.result


def _run(first: CandidateIssue, second: CandidateIssue, result: CausalAnalysisBatch | None):
    llm = _FakeLLM(result)
    output = merge_survivors(
        [first, second],
        [first.id, second.id],
        [_issue(first), _issue(second)],
        {first.id: _verification(first), second.id: _verification(second)},
        llm=llm,
        structured_method="function_calling",
    )
    return output, llm


def test_only_same_cause_and_same_effect_are_merged():
    first, second = _candidate("a", "same bug"), _candidate("b", "same bug")
    output, _ = _run(
        first,
        second,
        CausalAnalysisBatch(
            profiles=[_profile("a"), _profile("b")],
            comparisons=[CausalComparison(
                left_candidate_id="a",
                right_candidate_id="b",
                same_cause=True,
                same_effect=True,
            )],
        ),
    )

    assert len(output.final_issues) == 1
    assert output.stats["merged_candidate_count"] == 2


def test_same_cause_but_different_effect_is_preserved():
    first, second = _candidate("a", "different impact one"), _candidate("b", "different impact two")
    result = CausalAnalysisBatch(
        profiles=[_profile("a"), _profile("b")],
        comparisons=[CausalComparison(
            left_candidate_id="a", right_candidate_id="b", same_cause=True, same_effect=False
        )],
    )

    output, _ = _run(first, second, result)

    assert len(output.final_issues) == 2
    assert output.groups == []


def test_unknown_comparison_is_not_treated_as_same():
    first, second = _candidate("a", "first"), _candidate("b", "second")
    result = CausalAnalysisBatch(
        profiles=[_profile("a"), _profile("b")],
        comparisons=[CausalComparison(
            left_candidate_id="a",
            right_candidate_id="b",
            same_cause=None,
            same_effect=None,
        )],
    )

    output, _ = _run(first, second, result)

    assert len(output.final_issues) == 2
    assert output.groups == []


def test_uncertain_or_failed_analysis_is_fail_closed():
    first, second = _candidate("a", "first"), _candidate("b", "second")
    output, llm = _run(first, second, None)

    assert len(output.final_issues) == 2
    assert output.stats["failed_batch_count"] == 1
    assert llm.calls == 2


def test_candidates_from_different_tasks_are_not_compared_or_merged():
    first = _candidate("a", "same bug", task_id="task-1")
    second = _candidate("b", "same bug", task_id="task-2")
    llm = _FakeLLM(
        CausalAnalysisBatch(
            profiles=[_profile("a"), _profile("b")],
            comparisons=[CausalComparison(
                left_candidate_id="a",
                right_candidate_id="b",
                same_cause=True,
                same_effect=True,
            )],
        )
    )

    output = merge_survivors(
        [first, second],
        [first.id, second.id],
        [_issue(first), _issue(second)],
        {first.id: _verification(first), second.id: _verification(second)},
        llm=llm,
        structured_method="function_calling",
    )

    assert len(output.final_issues) == 2
    assert llm.calls == 0
    assert output.stats["batch_count"] == 0
