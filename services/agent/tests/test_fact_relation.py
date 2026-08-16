"""FactRelation/CandidateFact/统一裁决模型契约测试(ADR-046)。"""
import pytest
from pydantic import ValidationError

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


def test_fact_relation_requires_observation_for_supporting_relations():
    with pytest.raises(ValidationError):
        FactRelation(fact_id="f1", relation="supports")
    with pytest.raises(ValidationError):
        FactRelation(fact_id="f1", relation="contradicts", observation="   ")


def test_insufficient_relation_requires_contextual_strength_and_limitation():
    with pytest.raises(ValidationError):
        FactRelation(
            fact_id="f1", relation="insufficient", strength="direct", limitation="x"
        )
    with pytest.raises(ValidationError):
        FactRelation(fact_id="f1", relation="insufficient", limitation="   ")
    ok = FactRelation(fact_id="f1", relation="insufficient", limitation="diff 无法判定")
    assert ok.strength == "contextual"


def test_literal_enums_reject_invalid_values():
    with pytest.raises(ValidationError):
        FactRelation(fact_id="f1", relation="validates", observation="x")
    with pytest.raises(ValidationError):
        FactRelation(
            fact_id="f1", relation="insufficient", strength="absolute", limitation="x"
        )
    with pytest.raises(ValidationError):
        CandidateFact(fact_id="f1", source="tool:x", replay_status="confirmed")


def test_fact_models_defaults():
    relation = FactRelation(fact_id="f1", relation="supports", observation="有证据")
    assert relation.strength == "contextual"
    assert relation.limitation == ""
    fact = CandidateFact(fact_id="f1", source="tool:x")
    assert fact.replay_status == "unverified"
    assert fact.raw == ""
    assert fact.limitation == ""
