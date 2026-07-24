"""RiskTag 到证据策略的统一注册表。"""

from __future__ import annotations

from codeguard_agent.models.council import EvidencePurpose
from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence.rules.behavior import BEHAVIOR_STRATEGIES
from codeguard_agent.pipeline.evidence.rules.classify import (
    CandidateTagResolution,
    resolve_candidate_evidence_tag,
    resolve_candidate_tags,
)
from codeguard_agent.pipeline.evidence.rules.general import GENERAL_STRATEGIES
from codeguard_agent.pipeline.evidence.rules.maintainability import (
    MAINTAINABILITY_STRATEGIES,
)
from codeguard_agent.pipeline.evidence.rules.security import SECURITY_STRATEGIES
from codeguard_agent.pipeline.evidence.rules.types import EvidenceStrategy, ToolCallSpec


def _build_registry(
    strategies: list[EvidenceStrategy],
) -> tuple[dict[RiskTag, tuple[EvidenceStrategy, ...]], dict[str, EvidenceStrategy]]:
    by_id: dict[str, EvidenceStrategy] = {}
    mutable_by_tag: dict[RiskTag, list[EvidenceStrategy]] = {}
    for strategy in strategies:
        if strategy.id in by_id:
            raise ValueError(f"duplicate evidence strategy id: {strategy.id}")
        by_id[strategy.id] = strategy
        for tag in strategy.tags:
            mutable_by_tag.setdefault(tag, []).append(strategy)

    by_tag = {
        tag: tuple(sorted(items, key=lambda item: item.priority))
        for tag, items in mutable_by_tag.items()
    }
    return by_tag, by_id


_ALL_STRATEGIES = [
    *SECURITY_STRATEGIES,
    *BEHAVIOR_STRATEGIES,
    *MAINTAINABILITY_STRATEGIES,
    *GENERAL_STRATEGIES,
]
STRATEGIES_BY_TAG, STRATEGIES_BY_ID = _build_registry(_ALL_STRATEGIES)


def strategies_for(
    tag: RiskTag, purpose: EvidencePurpose | None = None
) -> tuple[EvidenceStrategy, ...]:
    strategies = STRATEGIES_BY_TAG.get(tag, ())
    if purpose is None:
        return strategies
    return tuple(strategy for strategy in strategies if strategy.purpose == purpose)


__all__ = [
    "CandidateTagResolution",
    "EvidenceStrategy",
    "ToolCallSpec",
    "STRATEGIES_BY_TAG",
    "STRATEGIES_BY_ID",
    "resolve_candidate_evidence_tag",
    "resolve_candidate_tags",
    "strategies_for",
]
