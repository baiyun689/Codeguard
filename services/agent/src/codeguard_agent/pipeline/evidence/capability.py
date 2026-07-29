"""EvidenceCapabilityRegistry：fact_type → capabilities 的深 Module。

在现有 EvidenceStrategy 注册表之上提供按 fact_type 查询的能力。
RiskTag 用于排序 capability 而非唯一 lookup key。
"""

from __future__ import annotations

from codeguard_agent.models.council import EvidenceFactType
from codeguard_agent.pipeline.evidence.rules.types import EvidenceCapability


# fact_type → 推荐的 capability 优先级列表
FACT_TYPE_CAPABILITIES: dict[EvidenceFactType, tuple[EvidenceCapability, ...]] = {
    EvidenceFactType.CHANGED_CONDITION: (
        EvidenceCapability.CURRENT_IMPLEMENTATION,
    ),
    EvidenceFactType.VALUE_IDENTITY: (
        EvidenceCapability.CURRENT_IMPLEMENTATION,
        EvidenceCapability.UPSTREAM_REACHABILITY,
    ),
    EvidenceFactType.CALL_PATH: (
        EvidenceCapability.UPSTREAM_REACHABILITY,
        EvidenceCapability.FRAMEWORK_ENTRY_REACHABILITY,
    ),
    EvidenceFactType.DATA_FLOW: (
        EvidenceCapability.UPSTREAM_REACHABILITY,
        EvidenceCapability.CURRENT_IMPLEMENTATION,
    ),
    EvidenceFactType.STATE_TRANSITION: (
        EvidenceCapability.CURRENT_IMPLEMENTATION,
        EvidenceCapability.UPSTREAM_REACHABILITY,
    ),
    EvidenceFactType.TRANSACTION_BOUNDARY: (
        EvidenceCapability.CURRENT_IMPLEMENTATION,
        EvidenceCapability.UPSTREAM_REACHABILITY,
    ),
    EvidenceFactType.ORDERING: (
        EvidenceCapability.CURRENT_IMPLEMENTATION,
        EvidenceCapability.UPSTREAM_REACHABILITY,
    ),
    EvidenceFactType.GUARD_PRESENCE: (
        EvidenceCapability.UPSTREAM_REACHABILITY,
        EvidenceCapability.CURRENT_IMPLEMENTATION,
    ),
    EvidenceFactType.REACHABILITY: (
        EvidenceCapability.UPSTREAM_REACHABILITY,
        EvidenceCapability.FRAMEWORK_ENTRY_REACHABILITY,
        EvidenceCapability.SECURITY_PATH,
    ),
    EvidenceFactType.SIDE_EFFECT: (
        EvidenceCapability.UPSTREAM_REACHABILITY,
        EvidenceCapability.CURRENT_IMPLEMENTATION,
    ),
    EvidenceFactType.OBSERVABLE_CONSEQUENCE: (
        EvidenceCapability.UPSTREAM_REACHABILITY,
        EvidenceCapability.CURRENT_IMPLEMENTATION,
    ),
    EvidenceFactType.FIX_SCOPE: (
        EvidenceCapability.UPSTREAM_REACHABILITY,
        EvidenceCapability.INHERITANCE_IMPACT,
    ),
    EvidenceFactType.IMPACT_FACTOR: (
        EvidenceCapability.UPSTREAM_REACHABILITY,
        EvidenceCapability.CURRENT_IMPLEMENTATION,
    ),
}


def capabilities_for_fact_type(
    fact_type: EvidenceFactType,
) -> tuple[EvidenceCapability, ...]:
    """返回 fact_type 的推荐 capability 优先级列表。"""
    return FACT_TYPE_CAPABILITIES.get(
        fact_type, (EvidenceCapability.CURRENT_IMPLEMENTATION,)
    )
