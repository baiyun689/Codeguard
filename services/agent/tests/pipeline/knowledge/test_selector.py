"""KnowledgeSelector 单元测试。"""
from __future__ import annotations

import pytest
from codeguard_agent.models.knowledge import (
    KnowledgeBudget,
    KnowledgeFragment,
    KnowledgeKind,
    SelectedKnowledge,
)
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


def _make_task(task_id: str = "task-1", file: str = "src/main/java/Foo.java", patch: str = "") -> ReviewTask:
    return ReviewTask(id=task_id, file=file, patch=patch or "+\tpublic void foo() {}")


def _make_prior(task_id: str = "task-1", hypotheses: tuple = (), coverage: RiskCoverage = RiskCoverage.UNCLASSIFIED) -> TaskRiskPrior:
    return TaskRiskPrior(task_id=task_id, hypotheses=hypotheses, coverage=coverage)


def _hyp(tag: RiskTag, conf: float = 0.85, pri: int = 3) -> RiskHypothesis:
    return RiskHypothesis(
        tag=tag, match_confidence=conf, review_priority=pri,
        source_kind="diff_text", source="test", reason="test",
    )


class TestKnowledgeSelector:
    def test_base_always_present(self):
        catalog = KnowledgeCatalog()
        bundle = select_knowledge(
            reviewer=ReviewerKind.BEHAVIOR,
            task=_make_task(),
            prior=_make_prior(),
            context=None,
            catalog=catalog,
            budget=KnowledgeBudget(),
        )
        assert bundle.base is not None
        assert bundle.base.fragment.kind == KnowledgeKind.BASE

    def test_correct_prior_selects_matching_topic(self):
        catalog = KnowledgeCatalog()
        prior = _make_prior(
            hypotheses=(_hyp(RiskTag.TRANSACTION_ATOMICITY),),
            coverage=RiskCoverage.CONFIDENT,
        )
        task = _make_task(patch="+\t@Transactional\n+\tpublic void transfer() {\n+\t  repo.save(a); repo.save(b);\n+\t}")
        bundle = select_knowledge(
            reviewer=ReviewerKind.BEHAVIOR,
            task=task,
            prior=prior,
            context=None,
            catalog=catalog,
            budget=KnowledgeBudget(),
        )
        topics = [s.fragment.topic for s in bundle.specialized]
        assert "TRANSACTION_ATOMICITY" in topics

    def test_wrong_prior_patch_semantics_can_recover(self):
        catalog = KnowledgeCatalog()
        prior = _make_prior(
            hypotheses=(_hyp(RiskTag.PERFORMANCE),),
            coverage=RiskCoverage.CONFIDENT,
        )
        task = _make_task(patch="+\t@Transactional\n+\tpublic void process() { repo.save(x); eventPublisher.publish(evt); }")
        bundle = select_knowledge(
            reviewer=ReviewerKind.BEHAVIOR,
            task=task,
            prior=prior,
            context=None,
            catalog=catalog,
            budget=KnowledgeBudget(),
        )
        topics = [s.fragment.topic for s in bundle.specialized]
        assert len(topics) > 0

    def test_max_specialized_enforced(self):
        catalog = KnowledgeCatalog()
        budget = KnowledgeBudget(max_specialized_fragments=1, max_chars=100000)
        prior = _make_prior(
            hypotheses=(
                _hyp(RiskTag.TRANSACTION_ATOMICITY),
                _hyp(RiskTag.MESSAGE_DELIVERY),
            ),
            coverage=RiskCoverage.CONFIDENT,
        )
        bundle = select_knowledge(
            reviewer=ReviewerKind.BEHAVIOR,
            task=_make_task(),
            prior=prior,
            context=None,
            catalog=catalog,
            budget=budget,
        )
        assert len(bundle.specialized) <= 1

    def test_ambiguous_coverage_still_has_base(self):
        catalog = KnowledgeCatalog()
        prior = _make_prior(coverage=RiskCoverage.AMBIGUOUS)
        bundle = select_knowledge(
            reviewer=ReviewerKind.BEHAVIOR,
            task=_make_task(),
            prior=prior,
            context=None,
            catalog=catalog,
            budget=KnowledgeBudget(),
        )
        assert bundle.base is not None

    def test_same_input_stable_output(self):
        catalog = KnowledgeCatalog()
        prior = _make_prior(
            hypotheses=(_hyp(RiskTag.TRANSACTION_ATOMICITY, 0.9, 3),),
            coverage=RiskCoverage.CONFIDENT,
        )
        task = _make_task()
        b1 = select_knowledge(reviewer=ReviewerKind.BEHAVIOR, task=task, prior=prior, context=None, catalog=catalog, budget=KnowledgeBudget())
        b2 = select_knowledge(reviewer=ReviewerKind.BEHAVIOR, task=task, prior=prior, context=None, catalog=catalog, budget=KnowledgeBudget())
        assert b1.rendered_text == b2.rendered_text

    def test_char_budget_truncates(self):
        catalog = KnowledgeCatalog()
        budget = KnowledgeBudget(max_chars=200, reserved_base_chars=100, max_specialized_fragments=5)
        bundle = select_knowledge(
            reviewer=ReviewerKind.BEHAVIOR,
            task=_make_task(),
            prior=_make_prior(),
            context=None,
            catalog=catalog,
            budget=budget,
        )
        assert len(bundle.rendered_text) <= 250

    def test_context_none_still_selectable(self):
        catalog = KnowledgeCatalog()
        bundle = select_knowledge(
            reviewer=ReviewerKind.BEHAVIOR,
            task=_make_task(),
            prior=_make_prior(),
            context=None,
            catalog=catalog,
            budget=KnowledgeBudget(),
        )
        assert bundle.base is not None

    def test_prior_missing_treated_as_unclassified(self):
        catalog = KnowledgeCatalog()
        bundle = select_knowledge(
            reviewer=ReviewerKind.BEHAVIOR,
            task=_make_task(),
            prior=_make_prior(),
            context=None,
            catalog=catalog,
            budget=KnowledgeBudget(),
        )
        assert bundle.base is not None

    def test_rendered_text_has_usage_constraints(self):
        catalog = KnowledgeCatalog()
        bundle = select_knowledge(
            reviewer=ReviewerKind.BEHAVIOR,
            task=_make_task(),
            prior=_make_prior(),
            context=None,
            catalog=catalog,
            budget=KnowledgeBudget(),
        )
        assert "Knowledge usage constraints" in bundle.rendered_text

    def test_file_role_influences_score(self):
        catalog = KnowledgeCatalog()
        task = _make_task(file="src/main/java/com/example/OrderController.java",
                          patch="+\t@GetMapping\n+\tpublic Order get(@PathVariable Long id) {}")
        bundle = select_knowledge(
            reviewer=ReviewerKind.THREAT_MODEL,
            task=task,
            prior=_make_prior(),
            context=None,
            catalog=catalog,
            budget=KnowledgeBudget(),
        )
        topics = [s.fragment.topic for s in bundle.specialized]
        assert "AUTHORIZATION" in topics
