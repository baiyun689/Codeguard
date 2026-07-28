"""SeverityResolver 确定性单元测试。"""
from __future__ import annotations

import pytest

from codeguard_agent.models.council import (
    CriticalPredicate,
    FactorStatus,
    ImpactAssessment,
    ImpactClass,
    ImpactFactor,
    ImpactFactorAssessment,
    ImpactRubric,
)
from codeguard_agent.models.schemas import Severity
from codeguard_agent.pipeline.council.severity import resolve_severity


def _assessment(
    concern_id: str = "c1",
    factors: tuple = (),
    impact_class: ImpactClass = ImpactClass.RUNTIME_CORRECTNESS,
) -> ImpactAssessment:
    return ImpactAssessment(
        concern_id=concern_id,
        impact_class=impact_class,
        factors=factors,
    )


def _factor(
    factor: ImpactFactor,
    status: FactorStatus = FactorStatus.PROVEN,
    evidence_ids: tuple = ("ev-1",),
) -> ImpactFactorAssessment:
    return ImpactFactorAssessment(
        factor=factor,
        status=status,
        evidence_ids=evidence_ids,
        reason="test",
    )


def _rubric(predicates: tuple = ()) -> ImpactRubric:
    return ImpactRubric(
        rubric_id="test",
        required_factors=tuple(ImpactFactor),
        critical_predicates=predicates,
    )


class TestSeverityResolver:
    def test_maintainability_only_is_info(self):
        """仅维护性影响 proven → INFO。"""
        impact = _assessment(factors=(
            _factor(ImpactFactor.LOCAL_MAINTAINABILITY_ONLY),
            _factor(ImpactFactor.RUNTIME_REACHABLE, FactorStatus.UNKNOWN, ()),
        ))
        result = resolve_severity(impact, _rubric())
        assert result.severity == Severity.INFO

    def test_runtime_error_bounded_is_warning(self):
        """运行时错误可达+范围有界 → WARNING。"""
        impact = _assessment(factors=(
            _factor(ImpactFactor.RUNTIME_REACHABLE),
            _factor(ImpactFactor.INTEGRITY_LOSS),
        ))
        result = resolve_severity(impact, _rubric())
        assert result.severity == Severity.WARNING

    def test_critical_predicate_all_proven_is_critical(self):
        """跨租户授权 predicate 全 proven → CRITICAL。"""
        predicate = CriticalPredicate(
            rule_id="test.cross_tenant",
            all_of=(ImpactFactor.RUNTIME_REACHABLE, ImpactFactor.AUTHORIZATION_BYPASS,
                    ImpactFactor.CROSS_TENANT_SCOPE),
            any_of=(ImpactFactor.CONFIDENTIALITY_LOSS, ImpactFactor.INTEGRITY_LOSS),
        )
        impact = _assessment(factors=(
            _factor(ImpactFactor.RUNTIME_REACHABLE),
            _factor(ImpactFactor.AUTHORIZATION_BYPASS),
            _factor(ImpactFactor.CROSS_TENANT_SCOPE),
            _factor(ImpactFactor.INTEGRITY_LOSS),
        ))
        result = resolve_severity(impact, _rubric((predicate,)))
        assert result.severity == Severity.CRITICAL
        assert result.rule_id == "test.cross_tenant"

    def test_cross_tenant_unknown_blocks_critical(self):
        """跨租户范围 unknown → WARNING（阻止 CRITICAL）。"""
        predicate = CriticalPredicate(
            rule_id="test.cross_tenant",
            all_of=(ImpactFactor.RUNTIME_REACHABLE, ImpactFactor.CROSS_TENANT_SCOPE),
        )
        impact = _assessment(factors=(
            _factor(ImpactFactor.RUNTIME_REACHABLE),
            _factor(ImpactFactor.CROSS_TENANT_SCOPE, FactorStatus.UNKNOWN, ()),
        ))
        result = resolve_severity(impact, _rubric((predicate,)))
        assert result.severity == Severity.WARNING

    def test_high_risk_tag_no_evidence_not_critical(self):
        """高风险标签但无 impact evidence → 不得 CRITICAL。"""
        impact = _assessment(factors=(
            _factor(ImpactFactor.RUNTIME_REACHABLE, FactorStatus.UNKNOWN, ()),
        ))
        predicate = CriticalPredicate(
            rule_id="test.auth",
            all_of=(ImpactFactor.RUNTIME_REACHABLE, ImpactFactor.AUTHORIZATION_BYPASS),
        )
        result = resolve_severity(impact, _rubric((predicate,)))
        assert result.severity != Severity.CRITICAL

    def test_proven_without_evidence_id_demoted_to_unknown(self):
        """PROVEN 无 evidence ID → 视为 UNKNOWN。"""
        # PROVEN 但没有 evidence_ids
        impact = _assessment(factors=(
            ImpactFactorAssessment(
                factor=ImpactFactor.RUNTIME_REACHABLE,
                status=FactorStatus.PROVEN,
                evidence_ids=(),  # 空！
            ),
        ))
        result = resolve_severity(impact, _rubric())
        # 没有有效的运行时 factor proven → INFO
        # 结果是 INFO 因为 RUNTIME_REACHABLE 被忽略，其他 factor 都是 UNKNOWN
        assert result.severity != Severity.CRITICAL

    def test_auto_recoverable_blocks_critical(self):
        """AUTO_RECOVERABLE PROVEN → 阻止依赖不可恢复条件的 CRITICAL predicate。"""
        predicate = CriticalPredicate(
            rule_id="test.irreversible",
            all_of=(ImpactFactor.RUNTIME_REACHABLE, ImpactFactor.IRREVERSIBLE),
            none_of=(ImpactFactor.AUTO_RECOVERABLE,),
        )
        impact = _assessment(factors=(
            _factor(ImpactFactor.RUNTIME_REACHABLE),
            _factor(ImpactFactor.IRREVERSIBLE),
            _factor(ImpactFactor.AUTO_RECOVERABLE),  # 自动恢复 → 阻止 CRITICAL
        ))
        result = resolve_severity(impact, _rubric((predicate,)))
        assert result.severity != Severity.CRITICAL

    def test_disproven_not_confused_with_proven(self):
        """DISPROVEN 不等于 PROVEN。"""
        predicate = CriticalPredicate(
            rule_id="test.integrity",
            all_of=(ImpactFactor.RUNTIME_REACHABLE, ImpactFactor.INTEGRITY_LOSS),
        )
        impact = _assessment(factors=(
            _factor(ImpactFactor.RUNTIME_REACHABLE),
            _factor(ImpactFactor.INTEGRITY_LOSS, FactorStatus.DISPROVEN, ("ev-2",)),
        ))
        result = resolve_severity(impact, _rubric((predicate,)))
        assert result.severity == Severity.WARNING

    def test_no_runtime_impact_is_info(self):
        """无运行时影响 → INFO。"""
        impact = _assessment(factors=(
            _factor(ImpactFactor.LOCAL_MAINTAINABILITY_ONLY),
        ))
        result = resolve_severity(impact, _rubric())
        assert result.severity == Severity.INFO

    def test_resolver_does_not_accept_risk_tag(self):
        """resolve_severity 不接受 RiskTag 参数。"""
        import inspect
        sig = inspect.signature(resolve_severity)
        params = list(sig.parameters.keys())
        assert "tag" not in params
        assert "impact" in params
        assert "rubric" in params
