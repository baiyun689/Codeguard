"""Evidence-gated deterministic severity policy registry and resolver.

Each RiskTag has a ``SeverityPolicy`` with a stable default, a hard ceiling,
and (for CRITICAL-eligible tags) required factor IDs.  ``resolve_severity``
returns the final severity after validating factor assessments against the
candidate's evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from dataclasses import dataclass

from codeguard_agent.models.council import EvidenceFinding, SeverityFactorAssessment
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import RiskTag

FindingIndex = Mapping[str, EvidenceFinding | Sequence[EvidenceFinding]]


# ── value objects ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SeverityFactorDefinition:
    """A single factor that a tag's CRITICAL policy requires."""

    id: str
    description: str


@dataclass(frozen=True)
class SeverityPolicy:
    """Immutable severity policy for one primary RiskTag."""

    tag: RiskTag
    default_severity: Severity
    maximum_severity: Severity
    factors: tuple[SeverityFactorDefinition, ...]
    critical_requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class SeverityResolution:
    """Deterministic result of applying a SeverityPolicy to evidence."""

    severity: Severity
    matched_rule: str
    proven_factors: tuple[str, ...]
    missing_critical_factors: tuple[str, ...]
    evidence_ids: tuple[str, ...]


# ── registry ─────────────────────────────────────────────────────────────────

CRITICAL_FACTORS: dict[RiskTag, tuple[str, ...]] = {
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

FACTOR_DESCRIPTIONS: dict[str, str] = {
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

LEVELS: dict[RiskTag, tuple[Severity, Severity]] = {
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

POLICIES: dict[RiskTag, SeverityPolicy] = {
    tag: SeverityPolicy(
        tag=tag,
        default_severity=levels[0],
        maximum_severity=levels[1],
        factors=tuple(
            SeverityFactorDefinition(
                id=factor_id,
                description=FACTOR_DESCRIPTIONS[factor_id],
            )
            for factor_id in CRITICAL_FACTORS.get(tag, ())
        ),
        critical_requires=CRITICAL_FACTORS.get(tag, ()),
    )
    for tag, levels in LEVELS.items()
}


# ── public API ───────────────────────────────────────────────────────────────


def policy_for(tag: RiskTag) -> SeverityPolicy:
    """Return the immutable severity policy for *tag*."""
    return POLICIES[tag]


def factor_is_proven(
    assessment: SeverityFactorAssessment,
    findings_by_id: FindingIndex,
) -> bool:
    """Return True when *assessment* is proven with sufficient evidence strength.

    A factor is proven when:
    - its status is ``"proven"``, AND
    - at least one cited finding has ``relation="supports"`` with ``strength="direct"``, OR
    - at least two cited findings from distinct sources have ``relation="supports"`` with ``strength="contextual"``.
    """
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
    contextual_sources = {
        f.source for f in supporting if f.strength == "contextual"
    }
    return len(contextual_sources) >= 2


def resolve_severity(
    tag: RiskTag,
    assessments: Sequence[SeverityFactorAssessment],
    findings_by_id: FindingIndex,
) -> SeverityResolution:
    """Apply the tag's policy to factor assessments and return a deterministic result.

    CRITICAL is returned only when the policy allows it AND every
    ``critical_requires`` factor is proven.
    """
    policy = policy_for(tag)
    by_id = {
        a.factor_id: a
        for a in assessments
        if a.factor_id in {f.id for f in policy.factors}
    }
    proven = tuple(
        factor_id
        for factor_id in policy.critical_requires
        if factor_id in by_id and factor_is_proven(by_id[factor_id], findings_by_id)
    )
    missing = tuple(fid for fid in policy.critical_requires if fid not in proven)
    severity = (
        Severity.CRITICAL
        if (
            policy.maximum_severity is Severity.CRITICAL
            and policy.critical_requires
            and not missing
        )
        else policy.default_severity
    )
    evidence_ids = tuple(
        dict.fromkeys(
            eid
            for factor_id in proven
            for eid in by_id[factor_id].evidence_ids
            if eid in findings_by_id
        )
    )
    return SeverityResolution(
        severity=severity,
        matched_rule=(
            f"{tag.value.lower()}.critical"
            if severity is Severity.CRITICAL
            else f"{tag.value.lower()}.default"
        ),
        proven_factors=proven,
        missing_critical_factors=missing,
        evidence_ids=evidence_ids,
    )
