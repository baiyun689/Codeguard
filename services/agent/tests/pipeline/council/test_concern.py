"""ConcernAnalyzer 单元测试。"""
from __future__ import annotations

import pytest
from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.council.concern import (
    analyze_candidate_groups,
    build_singleton_concerns,
)
from codeguard_agent.pipeline.council.dedup import CandidateGroup


def _make_candidate(
    cid: str = "c1",
    claim: str = "测试问题",
    source_agent: str = "threat_model",
    task_id: str = "t1",
    file: str = "src/main/Foo.java",
    line: int = 10,
    ctype: str = "AUTHORIZATION",
    suggestion: str = "添加校验",
) -> CandidateIssue:
    return CandidateIssue(
        id=cid,
        task_id=task_id,
        source_agent=source_agent,
        file=file,
        line=line,
        type=ctype,
        severity_proposal=Severity.WARNING,
        claim=claim,
        suggestion=suggestion,
        confidence=0.8,
    )


def _make_group(
    gid: str = "g1",
    members: tuple = (),
    primary_tag: RiskTag = RiskTag.AUTHORIZATION,
) -> CandidateGroup:
    if not members:
        c = _make_candidate()
        members = (c,)
    return CandidateGroup(
        id=gid,
        members=members,
        primary_risk_tag=primary_tag,
        severity_proposal=Severity.WARNING,
        confidence=0.8,
        shared_root_cause="test root cause",
        shared_behavior="test behavior",
        shared_fix="test fix",
    )


class TestConcernAnalyzer:
    def test_singleton_candidate_conversion(self):
        """singleton candidate → 完整 concern 转换。"""
        c = _make_candidate()
        group = _make_group(members=(c,))
        analysis = analyze_candidate_groups([group])
        assert len(analysis.concerns) == 1
        concern = analysis.concerns[0]
        assert concern.member_candidate_ids == (c.id,)
        assert len(concern.claims) == 1
        assert concern.claims[0].root_cause == c.claim
        assert c.id in analysis.candidate_to_concern

    def test_group_members_preserved(self):
        """组内全部成员被保留。"""
        c1 = _make_candidate("c1", "事务不原子")
        c2 = _make_candidate("c2", "事务不原子")
        group = _make_group(members=(c1, c2))
        analysis = analyze_candidate_groups([group])
        assert len(analysis.concerns) == 1
        assert set(analysis.concerns[0].member_candidate_ids) == {"c1", "c2"}

    def test_tags_extracted_from_member_types(self):
        """从成员 type 和 group primary_risk_tag 聚合标签。"""
        c1 = _make_candidate("c1", "事务问题", ctype="TRANSACTION_ATOMICITY")
        c2 = _make_candidate("c2", "事务问题", ctype="TRANSACTION_ATOMICITY")
        group = _make_group(members=(c1, c2), primary_tag=RiskTag.TRANSACTION_ATOMICITY)
        analysis = analyze_candidate_groups([group])
        concern = analysis.concerns[0]
        assert concern.tags.primary_tag == RiskTag.TRANSACTION_ATOMICITY

    def test_multi_tag_from_different_member_types(self):
        """不同成员 type 产生 primary + secondary。"""
        c1 = _make_candidate("c1", "事务+消息问题", ctype="TRANSACTION_ATOMICITY")
        c2 = _make_candidate("c2", "事务+消息问题", ctype="MESSAGE_DELIVERY")
        group = _make_group(members=(c1, c2), primary_tag=RiskTag.TRANSACTION_ATOMICITY)
        analysis = analyze_candidate_groups([group])
        concern = analysis.concerns[0]
        assert concern.tags.primary_tag == RiskTag.TRANSACTION_ATOMICITY
        assert RiskTag.MESSAGE_DELIVERY in concern.tags.secondary_tags

    def test_different_root_cause_splits_group(self):
        """不同 root cause → 拆成独立 concern。"""
        c1 = _make_candidate("c1", "事务不原子导致数据不一致")
        c2 = _make_candidate("c2", "消息丢失导致下游状态错误")
        group = _make_group(members=(c1, c2))
        analysis = analyze_candidate_groups([group])
        assert len(analysis.concerns) == 2

    def test_empty_group_no_crash(self):
        """空 members 不崩溃。"""
        group = CandidateGroup(
            id="empty", members=(), primary_risk_tag=RiskTag.GENERAL_REVIEW,
            severity_proposal=Severity.WARNING, confidence=0.5,
            shared_root_cause="", shared_behavior="", shared_fix="",
        )
        analysis = analyze_candidate_groups([group])
        assert len(analysis.concerns) == 0

    def test_candidate_id_coverage_100_percent(self):
        """candidate ID 覆盖率 100%。"""
        c1 = _make_candidate("c1")
        c2 = _make_candidate("c2")
        group = _make_group(members=(c1, c2))
        analysis = analyze_candidate_groups([group])
        all_ids = {m.id for g in [group] for m in g.members}
        covered = set(analysis.candidate_to_concern.keys())
        assert all_ids == covered

    def test_build_singleton_concerns_no_group(self):
        """无 CandidateGroup 时 singleton 兼容。"""
        c = _make_candidate()
        analysis = build_singleton_concerns([c])
        assert len(analysis.concerns) == 1
        assert analysis.concerns[0].member_candidate_ids == (c.id,)

    def test_concern_auto_id_generated(self):
        """concern_id 自动生成。"""
        c = _make_candidate()
        group = _make_group(members=(c,))
        analysis = analyze_candidate_groups([group])
        assert analysis.concerns[0].concern_id.startswith("concern-")

    def test_claim_auto_id_generated(self):
        """claim_id 自动生成。"""
        c = _make_candidate()
        group = _make_group(members=(c,))
        analysis = analyze_candidate_groups([group])
        assert analysis.concerns[0].claims[0].claim_id.startswith("claim-")
