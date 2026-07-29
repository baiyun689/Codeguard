"""Evidence-factor deterministic severity resolver.

Severity is determined by proven impact factors, not by RiskTag defaults.
CRITICAL must hit a registered CriticalPredicate with all factors proven.
"""

from __future__ import annotations

from collections.abc import Sequence

from codeguard_agent.models.council import (
    CriticalPredicate,
    FactorStatus,
    ImpactAssessment,
    ImpactClass,
    ImpactFactor,
    ImpactFactorAssessment,
    ImpactRubric,
    SeverityResolution,
)
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import RiskTag


# ── Factor definitions ─────────────────────────────────────────────────────────

FACTOR_INFO: dict[ImpactFactor, str] = {
    ImpactFactor.RUNTIME_REACHABLE: "变更代码在运行时可达",
    ImpactFactor.EXTERNAL_ACTOR_CONTROLLED: "攻击者或未授权调用者能够控制输入或触发条件",
    ImpactFactor.AUTHORIZATION_BYPASS: "缺少有效且不可绕过的授权校验",
    ImpactFactor.CROSS_TENANT_SCOPE: "影响跨租户边界",
    ImpactFactor.PRIVILEGED_OPERATION: "涉及高权限操作",
    ImpactFactor.CONFIDENTIALITY_LOSS: "可导致敏感信息泄露",
    ImpactFactor.INTEGRITY_LOSS: "可破坏数据或状态完整性",
    ImpactFactor.AVAILABILITY_LOSS: "可导致服务不可用",
    ImpactFactor.FINANCIAL_IMPACT: "涉及资金或财务影响",
    ImpactFactor.PERSISTENT_STATE_CORRUPTION: "可造成持久化状态损坏",
    ImpactFactor.EXTERNAL_SIDE_EFFECT: "产生外部副作用（消息、事件、回调）",
    ImpactFactor.MULTI_ENTITY_BLAST_RADIUS: "影响范围涉及多个实体/订单/用户",
    ImpactFactor.REPEATED_OR_AUTOMATIC_TRIGGER: "可被重复或自动触发",
    ImpactFactor.IRREVERSIBLE: "后果不可逆",
    ImpactFactor.AUTO_RECOVERABLE: "可自动恢复",
    ImpactFactor.OPERATOR_RECOVERABLE: "可由操作员恢复",
    ImpactFactor.LOCAL_MAINTAINABILITY_ONLY: "仅影响局部可维护性",
}


# ── CRITICAL predicates ────────────────────────────────────────────────────────

CRITICAL_PREDICATES: tuple[CriticalPredicate, ...] = (
    CriticalPredicate(
        rule_id="critical.cross_tenant_authorization",
        all_of=(
            ImpactFactor.RUNTIME_REACHABLE,
            ImpactFactor.EXTERNAL_ACTOR_CONTROLLED,
            ImpactFactor.AUTHORIZATION_BYPASS,
            ImpactFactor.CROSS_TENANT_SCOPE,
        ),
        any_of=(
            ImpactFactor.CONFIDENTIALITY_LOSS,
            ImpactFactor.INTEGRITY_LOSS,
        ),
        none_of=(ImpactFactor.AUTO_RECOVERABLE,),
    ),
    CriticalPredicate(
        rule_id="critical.financial_integrity",
        all_of=(
            ImpactFactor.RUNTIME_REACHABLE,
            ImpactFactor.FINANCIAL_IMPACT,
            ImpactFactor.INTEGRITY_LOSS,
        ),
        any_of=(
            ImpactFactor.PERSISTENT_STATE_CORRUPTION,
            ImpactFactor.EXTERNAL_SIDE_EFFECT,
        ),
    ),
    CriticalPredicate(
        rule_id="critical.persistent_blast_radius",
        all_of=(
            ImpactFactor.RUNTIME_REACHABLE,
            ImpactFactor.PERSISTENT_STATE_CORRUPTION,
            ImpactFactor.MULTI_ENTITY_BLAST_RADIUS,
        ),
        none_of=(ImpactFactor.AUTO_RECOVERABLE,),
    ),
    CriticalPredicate(
        rule_id="critical.privileged_external_action",
        all_of=(
            ImpactFactor.RUNTIME_REACHABLE,
            ImpactFactor.EXTERNAL_ACTOR_CONTROLLED,
            ImpactFactor.PRIVILEGED_OPERATION,
            ImpactFactor.EXTERNAL_SIDE_EFFECT,
        ),
    ),
    CriticalPredicate(
        rule_id="critical.irreversible_integrity_loss",
        all_of=(
            ImpactFactor.RUNTIME_REACHABLE,
            ImpactFactor.INTEGRITY_LOSS,
            ImpactFactor.IRREVERSIBLE,
        ),
    ),
    CriticalPredicate(
        rule_id="critical.broad_availability_loss",
        all_of=(
            ImpactFactor.RUNTIME_REACHABLE,
            ImpactFactor.AVAILABILITY_LOSS,
            ImpactFactor.MULTI_ENTITY_BLAST_RADIUS,
        ),
        any_of=(ImpactFactor.REPEATED_OR_AUTOMATIC_TRIGGER,),
    ),
)

# Tag → relevant predicates (subset)
TAG_PREDICATES: dict[RiskTag, tuple[CriticalPredicate, ...]] = {
    RiskTag.AUTHORIZATION: (CRITICAL_PREDICATES[0],),  # cross_tenant_authorization
    RiskTag.AUTHENTICATION_SESSION: (CRITICAL_PREDICATES[0],),
    RiskTag.INJECTION: (CRITICAL_PREDICATES[3], CRITICAL_PREDICATES[4]),
    RiskTag.SQL_DATA_ACCESS: (CRITICAL_PREDICATES[1], CRITICAL_PREDICATES[2]),
    RiskTag.FILE_PATH_IO: (CRITICAL_PREDICATES[3],),
    RiskTag.SSRF_OUTBOUND: (CRITICAL_PREDICATES[3],),
    RiskTag.CONFIG_SECURITY: (CRITICAL_PREDICATES[3],),
    RiskTag.DATA_EXPOSURE: (CRITICAL_PREDICATES[0],),
    RiskTag.DESERIALIZATION: (CRITICAL_PREDICATES[3],),
    RiskTag.TRANSACTION_ATOMICITY: (CRITICAL_PREDICATES[1], CRITICAL_PREDICATES[2]),
    RiskTag.CONCURRENCY_CONSISTENCY: (CRITICAL_PREDICATES[1], CRITICAL_PREDICATES[2]),
    RiskTag.IDEMPOTENCY_RETRY: (CRITICAL_PREDICATES[1],),
    RiskTag.MESSAGE_DELIVERY: (CRITICAL_PREDICATES[1], CRITICAL_PREDICATES[5]),
}


# ── Rubric registry ────────────────────────────────────────────────────────────

def _relevant_factors(tags: Sequence[RiskTag]) -> tuple[ImpactFactor, ...]:
    """从 tags 聚合应评估的 factors。"""
    if not tags:
        return tuple(ImpactFactor)
    factors: set[ImpactFactor] = {ImpactFactor.RUNTIME_REACHABLE}
    for tag in tags:
        if tag in (RiskTag.AUTHORIZATION, RiskTag.AUTHENTICATION_SESSION):
            factors.update({ImpactFactor.EXTERNAL_ACTOR_CONTROLLED,
                           ImpactFactor.AUTHORIZATION_BYPASS,
                           ImpactFactor.CROSS_TENANT_SCOPE})
        elif tag in (RiskTag.INJECTION, RiskTag.DESERIALIZATION):
            factors.update({ImpactFactor.EXTERNAL_ACTOR_CONTROLLED,
                           ImpactFactor.PRIVILEGED_OPERATION})
        elif tag in (RiskTag.SQL_DATA_ACCESS, RiskTag.TRANSACTION_ATOMICITY):
            factors.update({ImpactFactor.INTEGRITY_LOSS,
                           ImpactFactor.PERSISTENT_STATE_CORRUPTION})
        elif tag == RiskTag.MESSAGE_DELIVERY:
            factors.update({ImpactFactor.EXTERNAL_SIDE_EFFECT,
                           ImpactFactor.REPEATED_OR_AUTOMATIC_TRIGGER})
        elif tag in (RiskTag.CONCURRENCY_CONSISTENCY, RiskTag.IDEMPOTENCY_RETRY):
            factors.update({ImpactFactor.INTEGRITY_LOSS})
        elif tag == RiskTag.DATA_EXPOSURE:
            factors.update({ImpactFactor.CONFIDENTIALITY_LOSS})
        elif tag == RiskTag.CACHE_CONSISTENCY:
            factors.update({ImpactFactor.INTEGRITY_LOSS})
    # Always include maintainability + recoverability
    factors.update({ImpactFactor.LOCAL_MAINTAINABILITY_ONLY,
                    ImpactFactor.AUTO_RECOVERABLE,
                    ImpactFactor.OPERATOR_RECOVERABLE})
    return tuple(factors)


def rubric_for(
    impact_class: ImpactClass | None = None,
    tags: Sequence[RiskTag] = (),
) -> ImpactRubric:
    """根据 impact_class 和 tags 返回应评估的 rubric。"""
    factors = _relevant_factors(tags)
    # 从 tags 聚合 predicates
    preds: list[CriticalPredicate] = []
    seen: set[str] = set()
    for tag in tags:
        for p in TAG_PREDICATES.get(tag, ()):
            if p.rule_id not in seen:
                seen.add(p.rule_id)
                preds.append(p)
    # 无标签 concern 使用通用 rubric，但不允许仅凭宽泛关键词达到 CRITICAL。
    # 若证据证明了具体高影响语义，应先形成具体 concern tag 再选择 predicate。
    if preds:
        referenced = {
            factor
            for predicate in preds
            for factor in (
                *predicate.all_of,
                *predicate.any_of,
                *predicate.none_of,
            )
        }
        factors = tuple(dict.fromkeys((*factors, *sorted(
            referenced, key=lambda factor: factor.value,
        ))))

    tag_names = "+".join(sorted(t.value for t in tags)) if tags else "generic"
    return ImpactRubric(
        rubric_id=f"rubric.{impact_class.value if impact_class else 'unknown'}.{tag_names}",
        required_factors=factors,
        critical_predicates=tuple(preds),
    )


# ── Deterministic resolver ─────────────────────────────────────────────────────

def _factor_is_proven(
    assessment: ImpactFactorAssessment | None,
) -> bool:
    """PROVEN 且至少有一个 evidence_id。"""
    return (
        assessment is not None
        and assessment.status == FactorStatus.PROVEN
        and len(assessment.evidence_ids) > 0
    )


def _predicate_matched(
    predicate: CriticalPredicate,
    factors: Sequence[ImpactFactorAssessment],
) -> bool:
    """检查 predicate 的所有条件是否满足。"""
    by_factor = {a.factor: a for a in factors}

    # all_of: 每个 factor 都必须 PROVEN
    for f in predicate.all_of:
        a = by_factor.get(f)
        if a is None or not _factor_is_proven(a):
            return False

    # any_of: 至少一个 PROVEN（空 = 满足）
    if predicate.any_of:
        if not any(_factor_is_proven(by_factor.get(f)) for f in predicate.any_of if f in by_factor):
            return False

    # none_of: 没有一个 PROVEN
    for f in predicate.none_of:
        a = by_factor.get(f)
        if a is not None and _factor_is_proven(a):
            return False

    return True


def _nearest_critical_blockers(
    rubric: ImpactRubric,
    factors: Sequence[ImpactFactorAssessment],
) -> tuple[ImpactFactor, ...]:
    """返回最接近命中的 CRITICAL predicate 仍缺少/被阻止的因子。"""
    by_factor = {assessment.factor: assessment for assessment in factors}
    candidates: list[tuple[ImpactFactor, ...]] = []
    for predicate in rubric.critical_predicates:
        blockers: list[ImpactFactor] = [
            factor
            for factor in predicate.all_of
            if not _factor_is_proven(by_factor.get(factor))
        ]
        if predicate.any_of and not any(
            _factor_is_proven(by_factor.get(factor))
            for factor in predicate.any_of
        ):
            blockers.extend(predicate.any_of)
        blockers.extend(
            factor
            for factor in predicate.none_of
            if _factor_is_proven(by_factor.get(factor))
        )
        candidates.append(tuple(dict.fromkeys(blockers)))
    if not candidates:
        return ()
    return min(candidates, key=lambda item: (len(item), tuple(f.value for f in item)))


def resolve_severity(
    impact: ImpactAssessment,
    rubric: ImpactRubric,
) -> SeverityResolution:
    """纯确定性函数：ImpactAssessment → SeverityResolution。

    不接受 RiskTag、不接受 LLM、不接受 proposed severity。
    """
    proven = tuple(
        a.factor for a in impact.factors if _factor_is_proven(a)
    )
    disproven = tuple(
        a.factor for a in impact.factors if a.status == FactorStatus.DISPROVEN
    )

    # INFO: 仅维护性问题
    if (ImpactFactor.LOCAL_MAINTAINABILITY_ONLY in proven
            and not any(f in proven for f in (
                ImpactFactor.INTEGRITY_LOSS, ImpactFactor.AVAILABILITY_LOSS,
                ImpactFactor.CONFIDENTIALITY_LOSS, ImpactFactor.FINANCIAL_IMPACT,
                ImpactFactor.PERSISTENT_STATE_CORRUPTION,
            ))):
        return SeverityResolution(
            concern_id=impact.concern_id,
            severity=Severity.INFO,
            rule_id="info.maintainability_only",
            proven_factors=proven,
            limiting_factors=disproven,
            evidence_ids=tuple(dict.fromkeys(
                eid for a in impact.factors if _factor_is_proven(a)
                for eid in a.evidence_ids
            )),
            rationale="仅影响局部可维护性，无运行时影响",
        )

    # 运行时问题至少需要一个运行时 factor proven
    has_runtime_impact = any(f in proven for f in (
        ImpactFactor.RUNTIME_REACHABLE, ImpactFactor.INTEGRITY_LOSS,
        ImpactFactor.AVAILABILITY_LOSS, ImpactFactor.CONFIDENTIALITY_LOSS,
        ImpactFactor.FINANCIAL_IMPACT, ImpactFactor.PERSISTENT_STATE_CORRUPTION,
        ImpactFactor.EXTERNAL_SIDE_EFFECT,
    ))
    if not has_runtime_impact:
        return SeverityResolution(
            concern_id=impact.concern_id,
            severity=Severity.INFO,
            rule_id="info.no_runtime_impact",
            proven_factors=proven,
            limiting_factors=disproven,
            evidence_ids=(),
            rationale="未证明运行时影响",
        )

    # CRITICAL: 命中至少一个 predicate
    for predicate in rubric.critical_predicates:
        if _predicate_matched(predicate, impact.factors):
            evidence_ids = tuple(dict.fromkeys(
                eid for a in impact.factors
                if a.factor in predicate.all_of + predicate.any_of
                for eid in a.evidence_ids
            ))
            return SeverityResolution(
                concern_id=impact.concern_id,
                severity=Severity.CRITICAL,
                rule_id=predicate.rule_id,
                proven_factors=proven,
                limiting_factors=disproven,
                evidence_ids=evidence_ids,
                rationale=f"满足 CRITICAL predicate: {predicate.rule_id}",
            )

    # WARNING: 有运行时影响但未达 CRITICAL
    critical_blockers = _nearest_critical_blockers(
        rubric, impact.factors,
    )
    return SeverityResolution(
        concern_id=impact.concern_id,
        severity=Severity.WARNING,
        rule_id="warning.proven_runtime_bounded_or_unknown",
        proven_factors=proven,
        limiting_factors=critical_blockers or disproven,
        evidence_ids=tuple(dict.fromkeys(
            eid for a in impact.factors if _factor_is_proven(a)
            for eid in a.evidence_ids
        )),
        rationale="已证明运行时影响，但未满足 CRITICAL predicate 的全部条件",
    )


def resolve_severity_fallback(
    impact_class: ImpactClass | None = None,
) -> SeverityResolution:
    """Assessor/resolver 失败时的保守 fallback。绝不 CRITICAL。"""
    if impact_class == ImpactClass.MAINTAINABILITY:
        severity = Severity.INFO
    else:
        severity = Severity.WARNING
    return SeverityResolution(
        severity=severity,
        rule_id="fallback.assessor_or_resolver_failed",
        fallback_used=True,
        rationale="ImpactAssessor 或 SeverityResolver 失败，使用保守 fallback",
    )
