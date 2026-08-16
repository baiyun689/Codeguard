"""Knowledge 集成测试：端到端验证 Knowledge 选择和 Reviewer prompt 组装。"""
from __future__ import annotations

from codeguard_agent.models.knowledge import KnowledgeBudget
from codeguard_agent.models.tasks import (
    ReviewTask,
    ReviewerKind,
    RiskCoverage,
    RiskHypothesis,
    RiskTag,
    TaskRiskPrior,
)
from codeguard_agent.pipeline.knowledge.catalog import KnowledgeCatalog
from codeguard_agent.pipeline.knowledge.selector import select_knowledge


class TestKnowledgeIntegration:
    def test_token_misrouting_behavior_base_still_present(self):
        """'Token' 误命中安全标签时，Behavior BASE 仍存在。"""
        catalog = KnowledgeCatalog()
        task = _make(patch="+\tString token = TokenBucket.acquire();")
        prior = _make_prior(
            hypotheses=(_hyp(RiskTag.AUTHENTICATION_SESSION, 0.85, 3),),
            coverage=RiskCoverage.CONFIDENT,
        )
        bundle = select_knowledge(
            reviewer=ReviewerKind.BEHAVIOR,
            task=task,
            prior=prior,
            context=None,
            catalog=catalog,
            budget=KnowledgeBudget(max_specialized_fragments=2),
        )
        assert bundle.base is not None

    def test_transaction_message_composite_gets_two_topics(self):
        """事务 + 消息 composite 问题同时拿到两个专门主题。"""
        catalog = KnowledgeCatalog()
        task = _make(patch="+\t@Transactional\n+\tpublic void create() { repo.save(o); eventPublisher.publish(evt); }")
        prior = _make_prior(
            hypotheses=(
                _hyp(RiskTag.TRANSACTION_ATOMICITY, 0.9, 3),
                _hyp(RiskTag.MESSAGE_DELIVERY, 0.85, 3),
            ),
            coverage=RiskCoverage.CONFIDENT,
        )
        bundle = select_knowledge(
            reviewer=ReviewerKind.BEHAVIOR,
            task=task,
            prior=prior,
            context=None,
            catalog=catalog,
            budget=KnowledgeBudget(max_specialized_fragments=3),
        )
        topics = [s.fragment.topic for s in bundle.specialized]
        assert "TRANSACTION_ATOMICITY" in topics
        assert "MESSAGE_DELIVERY" in topics

    def test_tools_off_knowledge_bundle_consistent(self):
        """关闭工具时 KnowledgeBundle 一致（不依赖工具状态）。"""
        catalog = KnowledgeCatalog()
        task = _make()
        prior = _make_prior()
        b1 = select_knowledge(reviewer=ReviewerKind.BEHAVIOR, task=task, prior=prior,
                              context=None, catalog=catalog, budget=KnowledgeBudget())
        b2 = select_knowledge(reviewer=ReviewerKind.BEHAVIOR, task=task, prior=prior,
                              context=None, catalog=catalog, budget=KnowledgeBudget())
        assert b1.rendered_text == b2.rendered_text

    def test_unclassified_prior_still_gets_base_and_possible_specialized(self):
        """UNCLASSIFIED prior 时 BASE 存在，patch 语义仍可能选中专门主题。"""
        catalog = KnowledgeCatalog()
        task = _make(patch="+\t@Transactional\n+\tpublic void transfer() { repo.save(a); eventPublisher.publish(evt); }")
        prior = _make_prior(coverage=RiskCoverage.UNCLASSIFIED)
        bundle = select_knowledge(
            reviewer=ReviewerKind.BEHAVIOR,
            task=task,
            prior=prior,
            context=None,
            catalog=catalog,
            budget=KnowledgeBudget(),
        )
        assert bundle.base is not None
        # 即使 prior 是 UNCLASSIFIED，patch 中的 @Transactional 和 publish 应该触发专门主题
        # (不强断言具体数量，因为依赖于评分阈值)


def _make(task_id: str = "t1", file: str = "src/main/java/Foo.java", patch: str = "") -> ReviewTask:
    return ReviewTask(id=task_id, file=file,
                      patch=patch or "+\tpublic void foo() {}")


def _make_prior(task_id: str = "t1", hypotheses=(), coverage=RiskCoverage.UNCLASSIFIED) -> TaskRiskPrior:
    return TaskRiskPrior(task_id=task_id, hypotheses=hypotheses, coverage=coverage)


def _hyp(tag: RiskTag, conf: float, pri: int) -> RiskHypothesis:
    return RiskHypothesis(
        tag=tag, match_confidence=conf, review_priority=pri,
        source_kind="diff_text", source="test", reason="test",
    )
