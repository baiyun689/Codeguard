"""EvidenceCapabilityRegistry：fact_type → capabilities 的深 Module。

在现有 EvidenceStrategy 注册表之上提供按 fact_type 查询的能力。
RiskTag 用于排序 capability 而非唯一 lookup key。

同时提供 claim-driven 证据策略注册表——每条策略按 (fact_type, polarity) 绑定
确定性的工具配方，不再依赖运行时动态构造。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from codeguard_agent.models.council import EvidenceFactType, EvidencePolarity
from codeguard_agent.pipeline.evidence.rules.types import EvidenceCapability

if TYPE_CHECKING:
    from codeguard_agent.pipeline.evidence.planner import CandidateDossier
    from codeguard_agent.pipeline.evidence.rules.types import ToolCallSpec


# ── fact_type → 推荐的 capability 优先级列表 ──────────────────────────

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
        EvidenceCapability.CURRENT_IMPLEMENTATION,
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
        EvidenceCapability.CURRENT_IMPLEMENTATION,
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


# ── claim-driven 策略的工具配方（纯函数，不捕获 request） ────────────

def _claim_file_only(dossier: "CandidateDossier") -> "list[ToolCallSpec]":
    """仅当前文件内容。适用 CHANGED_CONDITION。"""
    from codeguard_agent.pipeline.evidence.rules.recipes import file_only
    return file_only(dossier)


def _claim_file_and_upstream(dossier: "CandidateDossier") -> "list[ToolCallSpec]":
    """当前文件 + 上游调用方。适用大部分 fact_type。"""
    from codeguard_agent.pipeline.evidence.rules.recipes import (
        callers_upstream,
        file_only,
    )
    return [*file_only(dossier), *callers_upstream(dossier)]


def _claim_upstream_only(dossier: "CandidateDossier") -> "list[ToolCallSpec]":
    """仅上游调用方。适用 CALL_PATH。"""
    from codeguard_agent.pipeline.evidence.rules.recipes import callers_upstream
    return callers_upstream(dossier)


def _claim_security(dossier: "CandidateDossier") -> "list[ToolCallSpec]":
    """文件 + 安全路径。适用 REACHABILITY。"""
    from codeguard_agent.pipeline.evidence.rules.recipes import file_sensitive
    return file_sensitive(dossier)


def _claim_upstream_security(dossier: "CandidateDossier") -> "list[ToolCallSpec]":
    """上游调用方 + 安全路径（不含文件内容）。保留供将来使用。"""
    from codeguard_agent.pipeline.evidence.rules.recipes import callers_upstream
    from codeguard_agent.pipeline.evidence.rules.types import (
        EvidenceCapability,
        ToolCallSpec,
    )

    calls: "list[ToolCallSpec]" = [*callers_upstream(dossier)]
    if dossier.context_bundle is not None:
        import json
        for fact in dossier.context_bundle.facts:
            if fact.kind != "symbol_context" or fact.truncated:
                continue
            try:
                value = json.loads(fact.content)
                symbol = str(value.get("symbol_id", ""))
                if symbol:
                    calls.append(
                        ToolCallSpec(
                            capability=EvidenceCapability.SECURITY_PATH,
                            arguments=(("symbol_id", symbol),),
                        )
                    )
                    break
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
    return calls


def _claim_file_and_metrics(dossier: "CandidateDossier") -> "list[ToolCallSpec]":
    """文件 + 上游 + 结构指标。适用 FIX_SCOPE。"""
    from codeguard_agent.pipeline.evidence.rules.recipes import (
        callers_upstream,
        file_metrics,
    )
    return [*file_metrics(dossier), *callers_upstream(dossier)]


# ── fact_type → 工具配方映射 ──────────────────────────────────────

_FACT_TYPE_RECIPE: dict[EvidenceFactType, object] = {
    EvidenceFactType.CHANGED_CONDITION: _claim_file_only,
    EvidenceFactType.VALUE_IDENTITY: _claim_file_and_upstream,
    EvidenceFactType.CALL_PATH: _claim_upstream_only,
    EvidenceFactType.DATA_FLOW: _claim_file_and_upstream,
    EvidenceFactType.STATE_TRANSITION: _claim_file_and_upstream,
    EvidenceFactType.TRANSACTION_BOUNDARY: _claim_file_and_upstream,
    EvidenceFactType.ORDERING: _claim_file_and_upstream,
    EvidenceFactType.GUARD_PRESENCE: _claim_file_and_upstream,
    EvidenceFactType.REACHABILITY: _claim_security,
    EvidenceFactType.SIDE_EFFECT: _claim_file_and_upstream,
    EvidenceFactType.OBSERVABLE_CONSEQUENCE: _claim_file_and_upstream,
    EvidenceFactType.FIX_SCOPE: _claim_file_and_metrics,
    EvidenceFactType.IMPACT_FACTOR: _claim_file_and_upstream,
}

# polarity → purpose 映射（IMPACT → "severity" 兼容 EvidenceRequest）
_POLARITY_PURPOSE: dict[EvidencePolarity, str] = {
    EvidencePolarity.SUPPORT: "support",
    EvidencePolarity.COUNTER: "counter",
    EvidencePolarity.IMPACT: "severity",
}


def _build_claim_strategies() -> "tuple[object, ...]":
    """构建全部 claim.* 策略注册表：13 fact_types × 3 polarities = 39 条。

    question_template 留空——运行时由 goal.proposition 注入。
    tags 为空 frozenset——claim 策略不绑定 RiskTag。
    """
    from codeguard_agent.pipeline.evidence.rules.types import EvidenceStrategy

    strategies: list[EvidenceStrategy] = []
    for fact_type in EvidenceFactType:
        recipe = _FACT_TYPE_RECIPE.get(fact_type, _claim_file_only)
        capabilities = capabilities_for_fact_type(fact_type)
        for polarity in EvidencePolarity:
            purpose = _POLARITY_PURPOSE[polarity]
            strategy_id = f"claim.{fact_type.value}.{polarity.value}"
            strategies.append(
                EvidenceStrategy(
                    id=strategy_id,
                    tags=frozenset(),
                    purpose=purpose,
                    priority=0,
                    question_template="",  # 运行时注入
                    context_kinds=("task_patch", "symbol_context"),
                    allowed_capabilities=capabilities,
                    build_tool_calls=recipe,
                )
            )
    return tuple(strategies)


CLAIM_STRATEGIES: "tuple[object, ...]" = _build_claim_strategies()
