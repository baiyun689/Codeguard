"""ClaimEvidencePlanner 单元测试。"""
from __future__ import annotations

from codeguard_agent.models.council import (
    CandidateClaim,
    CandidateConcern,
    ConcernTagResolution,
    EvidenceFactType,
    EvidencePolarity,
)
from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence.planner import plan_claim_evidence

# Rebuild models that reference RiskTag as a forward reference
import codeguard_agent.models.council as _council_mod

_CouncilNS = {"RiskTag": RiskTag}
_CouncilNS.update({k: v for k, v in _council_mod.__dict__.items() if not k.startswith("_")})
ConcernTagResolution.model_rebuild(_types_namespace=_CouncilNS)
CandidateConcern.model_rebuild(_types_namespace=_CouncilNS)


def _make_concern(
    concern_id: str = "concern-001",
    member_ids: tuple = ("c1",),
    root_cause: str = "测试 root cause",
    trigger: str = "",
    consequence: str = "",
    files: tuple = ("src/main/Foo.java",),
) -> CandidateConcern:
    claim = CandidateClaim(
        root_cause=root_cause,
        trigger=trigger,
        observable_consequence=consequence,
        fix_location="src/main/Foo.java:10",
        fix_action="修复建议",
    )
    return CandidateConcern(
        concern_id=concern_id,
        member_candidate_ids=member_ids,
        claims=(claim,),
        files=files,
        confidence=0.8,
    )


class TestClaimEvidencePlanner:
    def test_expression_issue_gets_value_identity_goal(self):
        """表达式/计算问题 → VALUE_IDENTITY fact_type。"""
        concern = _make_concern(
            root_cause="新表达式改变了运算结合顺序，quantity>1 时值被重复放大",
        )
        plan = plan_claim_evidence(concern)
        support_goals = [g for g in plan.goals if g.polarity == EvidencePolarity.SUPPORT]
        assert len(support_goals) == 1
        assert support_goals[0].fact_type == EvidenceFactType.VALUE_IDENTITY

    def test_transaction_issue_gets_transaction_boundary_goal(self):
        """事务问题 → TRANSACTION_BOUNDARY fact_type。"""
        concern = _make_concern(
            root_cause="数据库事务提交成功但事件发布失败",
        )
        plan = plan_claim_evidence(concern)
        support_goals = [g for g in plan.goals if g.polarity == EvidencePolarity.SUPPORT]
        assert len(support_goals) == 1
        assert support_goals[0].fact_type == EvidenceFactType.TRANSACTION_BOUNDARY

    def test_has_support_counter_impact_goals(self):
        """每个 concern 至少生成 support/counter/impact 三类 goal。"""
        concern = _make_concern(root_cause="测试问题描述")
        plan = plan_claim_evidence(concern)
        polarities = {g.polarity for g in plan.goals}
        assert EvidencePolarity.SUPPORT in polarities
        assert EvidencePolarity.COUNTER in polarities
        assert EvidencePolarity.IMPACT in polarities

    def test_goals_have_propositions_not_templates(self):
        """goal proposition 是具体命题而非通用模板。"""
        concern = _make_concern(root_cause="空指针异常在用户取消订单时触发")
        plan = plan_claim_evidence(concern)
        for goal in plan.goals:
            assert "空指针" in goal.proposition or "guard" in goal.proposition.lower() or "影响" in goal.proposition or "后果" in goal.proposition

    def test_requests_have_alignment_fields(self):
        """生成的 request 携带 goal_id/concern_id/claim_ids/fact_type。"""
        concern = _make_concern()
        plan = plan_claim_evidence(concern)
        for request in plan.requests:
            assert request.goal_id is not None
            assert request.concern_id == concern.concern_id
            assert request.fact_type is not None

    def test_request_target_uses_concern_file_when_fix_location_is_natural_language(self):
        """LLM 生成的自然语言修复位置不能污染严格校验的 request target。"""
        concern = _make_concern()
        claim = concern.claims[0].model_copy(
            update={"fix_location": "src/main/Foo.java，第10行之前。"}
        )
        concern = concern.model_copy(update={"claims": (claim,)})

        plan = plan_claim_evidence(concern)

        assert {request.target for request in plan.requests} == {"src/main/Foo.java"}

    def test_request_target_does_not_match_a_file_path_substring(self):
        """多文件 concern 中路径互为子串时仍选择完整匹配的文件。"""
        concern = _make_concern(
            files=("src/main/Foo.java", "test/src/main/Foo.java"),
        )
        claim = concern.claims[0].model_copy(
            update={"fix_location": "test/src/main/Foo.java，第10行。"}
        )
        concern = concern.model_copy(update={"claims": (claim,)})

        plan = plan_claim_evidence(concern)

        assert {request.target for request in plan.requests} == {
            "test/src/main/Foo.java"
        }

    def test_no_claims_produces_empty_plan(self):
        """无 claims → 空 plan + diagnostics。"""
        concern = CandidateConcern(
            concern_id="empty-concern",
            member_candidate_ids=("c1",),
        )
        plan = plan_claim_evidence(concern)
        assert len(plan.goals) == 0
        assert len(plan.diagnostics) > 0

    def test_member_specific_consequence_gets_own_goal(self):
        """成员独有 consequence → 独立 impact goal。"""
        c1 = CandidateClaim(root_cause="共享 root cause", observable_consequence="影响1")
        c2 = CandidateClaim(root_cause="共享 root cause", observable_consequence="影响2不同")
        concern = CandidateConcern(
            concern_id="multi-concern",
            member_candidate_ids=("c1", "c2"),
            claims=(c1, c2),
            files=("src/main/Foo.java",),
        )
        plan = plan_claim_evidence(concern)
        impact_goals = [g for g in plan.goals if g.polarity == EvidencePolarity.IMPACT]
        # 至少 2 个 impact goals（共享 + 成员独有）
        assert len(impact_goals) >= 2

    def test_generic_claim_gets_changed_condition(self):
        """无法分类的 claim → CHANGED_CONDITION。"""
        concern = _make_concern(root_cause="某段代码的行为与文档描述不符")
        plan = plan_claim_evidence(concern)
        support_goals = [g for g in plan.goals if g.polarity == EvidencePolarity.SUPPORT]
        assert len(support_goals) >= 1
        assert support_goals[0].fact_type == EvidenceFactType.CHANGED_CONDITION
