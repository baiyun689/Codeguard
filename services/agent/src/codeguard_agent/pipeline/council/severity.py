"""Evidence-factor deterministic severity resolver.

Phase 4: Severity is determined by proven impact factors, not by RiskTag defaults.
CRITICAL must hit a registered CriticalPredicate with all factors proven.

Compatibility: old policy_for(), factor_is_proven(), and resolve_severity(tag, ...)
are kept as deprecated adapters that delegate to the old policy data.
"""

# Phase 4: The primary severity resolution path is resolve_severity(impact, rubric).
# Old tag-based policy_for() and _resolve_severity_legacy() are deprecated
# compatibility adapters. LEVELS dict is kept only for legacy callers.

from __future__ import annotations

import warnings
from collections.abc import Mapping, Sequence

from dataclasses import dataclass

from codeguard_agent.models.council import (
    CriticalPredicate,
    EvidenceFinding,
    FactorStatus,
    ImpactAssessment,
    ImpactClass,
    ImpactFactor,
    ImpactFactorAssessment,
    ImpactRubric,
    SeverityFactorAssessment,
    SeverityResolution,
)
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import RiskTag

FindingIndex = Mapping[str, EvidenceFinding | Sequence[EvidenceFinding]]


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


# ── Deprecated compatibility ───────────────────────────────────────────────────

# Retain old registry data so deprecated wrappers still work for existing callers.
_OLD_CRITICAL_FACTORS: dict[RiskTag, tuple[str, ...]] = {
    RiskTag.AUTHORIZATION: (
        "untrusted_actor_reachable",
        "effective_authorization_absent",
        "high_value_cross_boundary_impact",
    ),
    RiskTag.AUTHENTICATION_SESSION: (
        "credential_or_session_control",
        "effective_session_validation_absent",
        "account_takeover_or_broad_scope",
    ),
    RiskTag.INJECTION: (
        "untrusted_input",
        "dangerous_interpreter_sink",
        "effective_mitigation_absent",
        "high_impact_execution_or_data",
    ),
    RiskTag.SQL_DATA_ACCESS: (
        "dangerous_data_operation",
        "scope_constraint_absent",
        "operation_reachable",
        "broad_irreversible_or_cross_tenant_impact",
    ),
    RiskTag.FILE_PATH_IO: (
        "untrusted_path",
        "filesystem_sink_reached",
        "effective_confinement_absent",
        "sensitive_read_or_arbitrary_write",
    ),
    RiskTag.SSRF_OUTBOUND: (
        "untrusted_destination",
        "outbound_sink_reached",
        "effective_network_restriction_absent",
        "credential_or_privileged_internal_impact",
    ),
    RiskTag.CONFIG_SECURITY: (
        "production_reachable",
        "security_control_disabled_or_secret_exposed",
        "broad_privileged_impact",
    ),
    RiskTag.DATA_EXPOSURE: (
        "sensitive_data_flow",
        "unauthorized_audience_reachable",
        "effective_redaction_or_access_control_absent",
        "broad_or_high_value_scope",
    ),
    RiskTag.DESERIALIZATION: (
        "untrusted_payload",
        "unsafe_deserializer_reached",
        "effective_type_restriction_absent",
        "code_execution_or_privileged_impact",
    ),
    RiskTag.TRANSACTION_ATOMICITY: (
        "critical_multi_step_state_change",
        "atomicity_gap",
        "failure_or_interleaving_reachable",
        "irreversible_financial_or_data_impact",
    ),
    RiskTag.CONCURRENCY_CONSISTENCY: (
        "shared_critical_state",
        "race_reachable",
        "effective_synchronization_absent",
        "financial_or_data_integrity_impact",
    ),
    RiskTag.IDEMPOTENCY_RETRY: (
        "duplicate_execution_reachable",
        "effective_idempotency_protection_absent",
        "irreversible_high_value_action",
    ),
    RiskTag.MESSAGE_DELIVERY: (
        "critical_event",
        "loss_duplicate_or_order_failure_reachable",
        "effective_delivery_protection_absent",
        "irreversible_high_impact",
    ),
}

_OLD_FACTOR_DESCRIPTIONS: dict[str, str] = {
    "untrusted_actor_reachable": "攻击者或未授权调用者能够到达该操作路径",
    "effective_authorization_absent": "敏感操作缺少有效且不可绕过的授权校验",
    "high_value_cross_boundary_impact": "影响高价值资源、越权边界或跨租户数据",
    "credential_or_session_control": "攻击者能够控制凭据、令牌或会话标识",
    "effective_session_validation_absent": "缺少有效的会话真实性、有效期或绑定校验",
    "account_takeover_or_broad_scope": "可导致账户接管或大范围身份权限影响",
    "untrusted_input": "攻击者可控输入能够到达受影响代码路径",
    "dangerous_interpreter_sink": "输入能够到达 SQL、命令、模板等解释执行入口",
    "effective_mitigation_absent": "不存在有效参数化、转义、白名单等缓解措施",
    "high_impact_execution_or_data": "可造成代码执行或高价值数据读写影响",
    "dangerous_data_operation": "存在删除、更新、查询或批量处理等敏感数据操作",
    "scope_constraint_absent": "数据操作缺少租户、主体或范围约束",
    "operation_reachable": "危险数据操作在现实调用路径中可达",
    "broad_irreversible_or_cross_tenant_impact": "可造成广泛、不可逆或跨租户数据影响",
    "untrusted_path": "文件路径或路径片段受攻击者控制",
    "filesystem_sink_reached": "可控路径能够到达文件读取、写入或删除操作",
    "effective_confinement_absent": "缺少规范化、根目录约束或等效路径隔离",
    "sensitive_read_or_arbitrary_write": "可读取敏感文件或写入攻击者选择的位置",
    "untrusted_destination": "出站请求目标受攻击者控制",
    "outbound_sink_reached": "可控目标能够到达实际网络请求入口",
    "effective_network_restriction_absent": "缺少协议、主机、地址段或重定向限制",
    "credential_or_privileged_internal_impact": "可访问凭据或高权限内部服务",
    "production_reachable": "相关配置在生产或生产等价环境中生效",
    "security_control_disabled_or_secret_exposed": "安全控制被禁用或敏感凭据直接暴露",
    "broad_privileged_impact": "影响范围广或涉及高权限能力",
    "sensitive_data_flow": "敏感数据确实流向受影响输出或存储位置",
    "unauthorized_audience_reachable": "未授权主体能够接触该敏感数据",
    "effective_redaction_or_access_control_absent": "缺少有效脱敏或访问控制",
    "broad_or_high_value_scope": "泄露范围广或数据价值高",
    "untrusted_payload": "反序列化负载受攻击者控制",
    "unsafe_deserializer_reached": "负载能够到达不安全反序列化入口",
    "effective_type_restriction_absent": "缺少类型白名单或等效安全限制",
    "code_execution_or_privileged_impact": "可造成代码执行或高权限影响",
    "critical_multi_step_state_change": "操作包含必须保持一致的关键多步骤状态变更",
    "atomicity_gap": "步骤之间缺少事务或等效原子性保障",
    "failure_or_interleaving_reachable": "故障或并发交错能够触发不一致状态",
    "irreversible_financial_or_data_impact": "可造成不可逆资金或数据损失",
    "shared_critical_state": "多个执行单元读写同一关键状态",
    "race_reachable": "现实执行顺序能够触发竞态",
    "effective_synchronization_absent": "缺少锁、原子操作或等效同步保障",
    "financial_or_data_integrity_impact": "可破坏资金或数据完整性",
    "duplicate_execution_reachable": "重试或重复投递能够重复执行操作",
    "effective_idempotency_protection_absent": "缺少幂等键、去重或等效保护",
    "irreversible_high_value_action": "重复操作会触发不可逆的高价值影响",
    "critical_event": "消息承载关键业务或状态变更事件",
    "loss_duplicate_or_order_failure_reachable": "消息丢失、重复或乱序在现实路径中可发生",
    "effective_delivery_protection_absent": "缺少确认、去重、顺序或补偿保障",
    "irreversible_high_impact": "消息异常可造成不可逆的高影响后果",
}

_OLD_LEVELS: dict[RiskTag, tuple[Severity, Severity]] = {
    RiskTag.AUTHORIZATION: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.AUTHENTICATION_SESSION: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.WEB_SECURITY_CONFIG: (Severity.WARNING, Severity.WARNING),
    RiskTag.INPUT_VALIDATION: (Severity.WARNING, Severity.WARNING),
    RiskTag.INJECTION: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.SQL_DATA_ACCESS: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.FILE_PATH_IO: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.SSRF_OUTBOUND: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.CONFIG_SECURITY: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.DATA_EXPOSURE: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.DESERIALIZATION: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.TRANSACTION_ATOMICITY: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.CONCURRENCY_CONSISTENCY: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.IDEMPOTENCY_RETRY: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.CACHE_CONSISTENCY: (Severity.WARNING, Severity.WARNING),
    RiskTag.MESSAGE_DELIVERY: (Severity.WARNING, Severity.CRITICAL),
    RiskTag.ERROR_HANDLING: (Severity.WARNING, Severity.WARNING),
    RiskTag.NULL_STATE_SAFETY: (Severity.WARNING, Severity.WARNING),
    RiskTag.RESOURCE_LIFECYCLE: (Severity.WARNING, Severity.WARNING),
    RiskTag.API_CONTRACT: (Severity.WARNING, Severity.WARNING),
    RiskTag.PERFORMANCE: (Severity.WARNING, Severity.WARNING),
    RiskTag.COMPLEXITY_CONTROL_FLOW: (Severity.INFO, Severity.INFO),
    RiskTag.DUPLICATION_DESIGN: (Severity.INFO, Severity.INFO),
    RiskTag.OBSERVABILITY_TESTABILITY: (Severity.INFO, Severity.INFO),
    RiskTag.GENERAL_REVIEW: (Severity.WARNING, Severity.WARNING),
}


@dataclass(frozen=True)
class _LegacySeverityFactorDefinition:
    id: str
    description: str


@dataclass(frozen=True)
class _LegacySeverityPolicy:
    tag: RiskTag
    default_severity: Severity
    maximum_severity: Severity
    factors: tuple[_LegacySeverityFactorDefinition, ...]
    critical_requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class _LegacySeverityResolution:
    severity: Severity
    matched_rule: str
    proven_factors: tuple[str, ...]
    missing_critical_factors: tuple[str, ...]
    evidence_ids: tuple[str, ...]


def policy_for(tag: RiskTag) -> _LegacySeverityPolicy:
    """Deprecated: 旧 policy_for 兼容。"""
    warnings.warn("policy_for is deprecated; use rubric_for instead", DeprecationWarning, stacklevel=2)
    levels = _OLD_LEVELS.get(tag, (Severity.WARNING, Severity.WARNING))
    default_sev, max_sev = levels
    critical_reqs = _OLD_CRITICAL_FACTORS.get(tag, ())
    return _LegacySeverityPolicy(
        tag=tag, default_severity=default_sev, maximum_severity=max_sev,
        factors=tuple(
            _LegacySeverityFactorDefinition(
                id=factor_id,
                description=_OLD_FACTOR_DESCRIPTIONS[factor_id],
            )
            for factor_id in critical_reqs
        ),
        critical_requires=critical_reqs,
    )


def factor_is_proven(
    assessment: SeverityFactorAssessment,
    findings_by_id: FindingIndex,
) -> bool:
    """Deprecated: 旧 factor_is_proven 兼容。"""
    warnings.warn("factor_is_proven is deprecated", DeprecationWarning, stacklevel=2)
    if assessment.status != "proven":
        return False
    cited: list[EvidenceFinding] = []
    for evidence_id in assessment.evidence_ids:
        value = findings_by_id.get(evidence_id)
        if value is None:
            continue
        if isinstance(value, EvidenceFinding):
            cited.append(value)
        else:
            cited.extend(value)
    supporting = [f for f in cited if f.relation == "supports"]
    if any(f.strength == "direct" for f in supporting):
        return True
    contextual_sources = {f.source for f in supporting if f.strength == "contextual"}
    return len(contextual_sources) >= 2


def _resolve_severity_legacy(
    tag: RiskTag,
    assessments: Sequence[SeverityFactorAssessment],
    findings_by_id: FindingIndex,
) -> _LegacySeverityResolution:
    """Deprecated: 委托给旧 policy 的兼容包装。"""
    warnings.warn(
        "_resolve_severity_legacy is deprecated; use resolve_severity(impact, rubric)",
        DeprecationWarning, stacklevel=2,
    )
    policy = policy_for(tag)
    by_id = {
        a.factor_id: a
        for a in assessments
        if a.factor_id in {f.id for f in policy.factors}
    }
    proven = tuple(
        factor_id for factor_id in policy.critical_requires
        if factor_id in by_id and factor_is_proven(by_id[factor_id], findings_by_id)
    )
    missing = tuple(fid for fid in policy.critical_requires if fid not in proven)
    severity = (
        Severity.CRITICAL
        if (policy.maximum_severity is Severity.CRITICAL
            and policy.critical_requires and not missing)
        else policy.default_severity
    )
    evidence_ids = tuple(dict.fromkeys(
        eid for factor_id in proven
        for eid in by_id[factor_id].evidence_ids if eid in findings_by_id
    ))
    return _LegacySeverityResolution(
        severity=severity,
        matched_rule=f"{tag.value.lower()}.critical" if severity is Severity.CRITICAL else f"{tag.value.lower()}.default",
        proven_factors=proven,
        missing_critical_factors=missing,
        evidence_ids=evidence_ids,
    )
