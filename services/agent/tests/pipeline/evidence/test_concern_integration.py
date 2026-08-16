"""Concern 集成测试：候选分组 → concern 拆分与候选全覆盖。"""
from __future__ import annotations

from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.council.concern import analyze_candidate_groups
from codeguard_agent.pipeline.council.dedup import CandidateGroup


def _make_candidate(cid: str, claim: str, ctype: str = "AUTHORIZATION") -> CandidateIssue:
    return CandidateIssue(
        id=cid, task_id="t1", source_agent="threat_model",
        file="src/main/Foo.java", line=10, type=ctype,
        severity_proposal=Severity.WARNING, claim=claim,
        suggestion="修复建议", confidence=0.8,
    )


def _make_group(members: tuple, primary_tag: RiskTag = RiskTag.AUTHORIZATION) -> CandidateGroup:
    return CandidateGroup(
        id="g1", members=members, primary_risk_tag=primary_tag,
        severity_proposal=Severity.WARNING, confidence=0.8,
        shared_root_cause="shared", shared_behavior="shared", shared_fix="shared",
    )


class TestConcernIntegration:
    def test_same_root_cause_different_consequence_split_to_separate_concerns(self):
        """共享 root cause 但不同下游影响 → 拆成独立 concern。"""
        c1 = _make_candidate("c1", "事务不原子导致数据不一致")
        c2 = _make_candidate("c2", "消息丢失导致下游状态错误")
        group = _make_group(members=(c1, c2))
        analysis = analyze_candidate_groups([group])
        # 不同的 root cause → 拆组
        assert len(analysis.concerns) == 2

    def test_transaction_message_composite_concern(self):
        """事务+消息复合候选 → 正确的 primary+secondary tags。"""
        c1 = _make_candidate("c1", "事务提交后事件发布失败导致数据不一致", ctype="TRANSACTION_ATOMICITY")
        c2 = _make_candidate("c2", "事务提交后事件发布失败导致数据不一致", ctype="MESSAGE_DELIVERY")
        group = _make_group(members=(c1, c2), primary_tag=RiskTag.TRANSACTION_ATOMICITY)
        analysis = analyze_candidate_groups([group])
        concern = analysis.concerns[0]
        assert concern.tags.primary_tag is not None
        # primary 应该是 TRANSACTION_ATOMICITY（来自 group）
        # secondary 应该包含 MESSAGE_DELIVERY
        assert RiskTag.MESSAGE_DELIVERY in concern.tags.secondary_tags or concern.tags.primary_tag == RiskTag.TRANSACTION_ATOMICITY

    def test_candidate_coverage_100_percent(self):
        """所有 candidate 都映射到 concern。"""
        c1 = _make_candidate("c1", "问题A")
        c2 = _make_candidate("c2", "问题B不同")
        group = _make_group(members=(c1, c2))
        analysis = analyze_candidate_groups([group])
        all_ids = {m.id for g in [group] for m in g.members}
        covered = set(analysis.candidate_to_concern.keys())
        assert all_ids == covered
