from __future__ import annotations

import pytest
from pydantic import ValidationError

from codeguard_agent.models.council import (
    ConcernTagResolution,
    EvidenceFinding,
    FactorStatus,
    ImpactFactor,
    ImpactFactorAssessment,
)
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import (
    ContextFact,
    ReviewerKind,
    RiskCoverage,
    RiskTag,
    ReviewTask,
    TaskContextBundle,
    TaskRiskPrior,
)
from codeguard_agent.pipeline.council.concern import analyze_candidate_groups
from codeguard_agent.pipeline.council.dedup import CandidateGroup
from codeguard_agent.pipeline.council.impact import assess_impact
from codeguard_agent.pipeline.council.severity import rubric_for
from codeguard_agent.pipeline.knowledge.catalog import KnowledgeCatalog
from codeguard_agent.pipeline.knowledge.selector import select_knowledge
from codeguard_agent.models.knowledge import KnowledgeBudget, KnowledgeSelectionSource
from codeguard_agent.models.council import CandidateIssue


def _candidate(candidate_id: str, claim: str, *, line: int = 10) -> CandidateIssue:
    return CandidateIssue(
        id=candidate_id,
        task_id=f"{candidate_id}.java#h0",
        source_agent="behavior",
        file=f"{candidate_id}.java",
        line=line,
        type="TRANSACTION_ATOMICITY",
        severity_proposal=Severity.WARNING,
        claim=claim,
        suggestion="move publication into a transactional outbox",
        confidence=0.9,
    )


def test_knowledge_uses_prefetched_context_and_reports_omitted_topics() -> None:
    task = ReviewTask(
        id="OrderService.java#h0",
        file="src/main/java/OrderService.java",
        patch="+repository.save(order);\n+publisher.publish(event);",
    )
    context = TaskContextBundle(
        task_id=task.id,
        facts=[
            ContextFact(
                source="resolve_change_context",
                kind="symbol_context",
                content='{"declaration":"KafkaTemplate publisher; @Transactional"}',
            )
        ],
    )
    bundle = select_knowledge(
        reviewer=ReviewerKind.BEHAVIOR,
        task=task,
        prior=TaskRiskPrior(task_id=task.id, coverage=RiskCoverage.UNCLASSIFIED),
        context=context,
        catalog=KnowledgeCatalog(),
        budget=KnowledgeBudget(max_chars=6000, max_specialized_fragments=1),
    )

    assert bundle.specialized
    assert bundle.omitted_topics


def test_concern_analysis_covers_grouped_and_ungrouped_candidates() -> None:
    first = _candidate("first", "commit succeeds while event publication can fail")
    duplicate = _candidate("duplicate", "commit succeeds while event publication can fail")
    singleton = _candidate("singleton", "retry repeats the external charge")
    group = CandidateGroup(
        id="group-1",
        members=(first, duplicate),
        primary_risk_tag=RiskTag.TRANSACTION_ATOMICITY,
        severity_proposal=Severity.WARNING,
        confidence=0.99,
        shared_root_cause="commit and publication are not atomic",
        shared_behavior="the event may be lost after commit",
        shared_fix="use a transactional outbox",
    )

    analysis = analyze_candidate_groups(
        (group,),
        candidates=(first, duplicate, singleton),
    )

    assert set(analysis.candidate_to_concern) == {
        "first",
        "duplicate",
        "singleton",
    }


def test_generic_rubric_cannot_produce_critical_and_tag_rubric_is_complete() -> None:
    assert rubric_for(tags=()).critical_predicates == ()

    rubric = rubric_for(tags=(RiskTag.TRANSACTION_ATOMICITY,))
    referenced = {
        factor
        for predicate in rubric.critical_predicates
        for factor in (*predicate.all_of, *predicate.any_of, *predicate.none_of)
    }
    assert referenced <= set(rubric.required_factors)


def test_impact_assessor_does_not_turn_insufficient_keywords_into_proof() -> None:
    rubric = rubric_for(tags=(RiskTag.TRANSACTION_ATOMICITY,))
    finding = EvidenceFinding(
        evidence_id="E1",
        source="tool",
        observation="payment amount is persisted to the database",
        relation="insufficient",
        strength="contextual",
        limitation="call path is truncated",
        concern_id="C1",
    )

    impact = assess_impact("C1", (finding,), rubric)
    by_factor = {assessment.factor: assessment for assessment in impact.factors}
    assert by_factor[ImpactFactor.PERSISTENT_STATE_CORRUPTION].status is FactorStatus.UNKNOWN


def test_impact_assessor_does_not_reverse_negated_factor_evidence() -> None:
    rubric = rubric_for(tags=(RiskTag.TRANSACTION_ATOMICITY,))
    finding = EvidenceFinding(
        evidence_id="E1",
        source="tool",
        observation="失败后无法自动恢复，必须人工修复",
        relation="supports",
        strength="direct",
        concern_id="C1",
    )

    impact = assess_impact("C1", (finding,), rubric)
    by_factor = {assessment.factor: assessment for assessment in impact.factors}
    assert by_factor[ImpactFactor.AUTO_RECOVERABLE].status is FactorStatus.UNKNOWN
    assert by_factor[ImpactFactor.OPERATOR_RECOVERABLE].status is FactorStatus.PROVEN


def test_factor_and_concern_tag_models_enforce_lossless_invariants() -> None:
    factor = ImpactFactorAssessment(
        factor=ImpactFactor.RUNTIME_REACHABLE,
        status=FactorStatus.PROVEN,
    )
    assert factor.status is FactorStatus.UNKNOWN
    assert factor.evidence_ids == ()
    with pytest.raises(ValidationError):
        ConcernTagResolution(
            primary_tag=RiskTag.TRANSACTION_ATOMICITY,
            secondary_tags=(RiskTag.TRANSACTION_ATOMICITY,),
            source="deterministic",
        )
