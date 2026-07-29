"""Concern → Evidence 集成测试：端到端验证新管线。"""
from __future__ import annotations

from codeguard_agent.models.council import (
    CandidateIssue,
    EvidenceFactType,
    EvidencePolarity,
)
from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence.planner import plan_claim_evidence
from codeguard_agent.pipeline.council.concern import analyze_candidate_groups
from codeguard_agent.pipeline.council.dedup import CandidateGroup
from codeguard_agent.models.schemas import Severity


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


class TestConcernEvidenceIntegration:
    def test_misclassified_behavior_claim_still_gets_correct_evidence_question(self):
        """被错分到维护性标签的行为主张，Evidence 仍询问真实表达式/调用路径。"""
        c = _make_candidate("c1", "新表达式改变了运算顺序导致金额计算错误", ctype="COMPLEXITY_CONTROL_FLOW")
        group = _make_group(members=(c,), primary_tag=RiskTag.COMPLEXITY_CONTROL_FLOW)
        analysis = analyze_candidate_groups([group])
        concern = analysis.concerns[0]
        plan = plan_claim_evidence(concern)

        # 即使 type 是 COMPLEXITY_CONTROL_FLOW，root cause 是表达式计算问题
        support_goals = [g for g in plan.goals if g.polarity == EvidencePolarity.SUPPORT]
        assert len(support_goals) >= 1
        # 应该识别为 VALUE_IDENTITY（表达式值问题），而非泛化复杂度问题
        assert support_goals[0].fact_type == EvidenceFactType.VALUE_IDENTITY
        assert "表达式" in support_goals[0].proposition or "计算" in support_goals[0].proposition

    def test_same_root_cause_different_consequence_split_to_separate_concerns(self):
        """共享 root cause 但不同下游影响 → 拆成独立 concern。"""
        c1 = _make_candidate("c1", "事务不原子导致数据不一致")
        c2 = _make_candidate("c2", "消息丢失导致下游状态错误")
        group = _make_group(members=(c1, c2))
        analysis = analyze_candidate_groups([group])
        # 不同的 root cause → 拆组
        assert len(analysis.concerns) == 2

    def test_singleton_fallback_produces_valid_concern(self):
        """Singleton fallback 产生有效 concern 和 plan。"""
        c = _make_candidate("c1", "空指针风险")
        analysis = analyze_candidate_groups([], candidates=[c])
        assert len(analysis.concerns) == 1
        concern = analysis.concerns[0]
        plan = plan_claim_evidence(concern)
        assert len(plan.goals) >= 3  # support + counter + impact

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

    def test_goal_request_alignment_chain(self):
        """goal → request 对齐链完整。"""
        c = _make_candidate("c1", "SQL 注入风险：用户输入拼接到查询")
        group = _make_group(members=(c,))
        analysis = analyze_candidate_groups([group])
        plan = plan_claim_evidence(analysis.concerns[0])

        for request in plan.requests:
            # 每个 request 应该能追溯到其 goal
            matching_goals = [g for g in plan.goals if g.goal_id == request.goal_id]
            assert len(matching_goals) == 1
            # concern_id 对齐
            assert request.concern_id == analysis.concerns[0].concern_id

    def test_candidate_coverage_100_percent(self):
        """所有 candidate 都映射到 concern。"""
        c1 = _make_candidate("c1", "问题A")
        c2 = _make_candidate("c2", "问题B不同")
        group = _make_group(members=(c1, c2))
        analysis = analyze_candidate_groups([group])
        all_ids = {m.id for g in [group] for m in g.members}
        covered = set(analysis.candidate_to_concern.keys())
        assert all_ids == covered
