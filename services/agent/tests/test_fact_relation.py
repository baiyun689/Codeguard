"""FactRelation/CandidateFact/统一裁决模型契约测试(ADR-046)。"""
from codeguard_agent.models.council import (
    CandidateDirectAssessment,
    CandidateFact,
    FactRelation,
)
from codeguard_agent.models.schemas import Severity


def test_fact_relation_triple_and_strength():
    relation = FactRelation(
        fact_id="f1",
        relation="supports",
        strength="direct",
        observation="上游调用方确认可达",
    )
    assert relation.relation == "supports"
    assert relation.strength == "direct"


def test_candidate_fact_replay_statuses():
    assert CandidateFact(fact_id="f1", source="tool:x", replay_status="recipe").replay_status == "recipe"
    assert CandidateFact(fact_id="f2", source="tool:y", replay_status="verified").replay_status == "verified"


def test_direct_assessment_carries_cited_fact_ids():
    assessment = CandidateDirectAssessment(
        candidate_id="c1",
        action="keep",
        severity=Severity.WARNING,
        cited_fact_ids=("f1",),
        reason="有支持证据",
    )
    assert assessment.cited_fact_ids == ("f1",)
    # 消融档默认无引用
    assert CandidateDirectAssessment(
        candidate_id="c1", action="drop", severity=Severity.WARNING
    ).cited_fact_ids == ()
