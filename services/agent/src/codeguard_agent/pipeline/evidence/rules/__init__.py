"""Claim-driven 证据策略与候选语义分类入口。"""

from __future__ import annotations

from codeguard_agent.pipeline.evidence.rules.classify import resolve_candidate_tag
from codeguard_agent.pipeline.evidence.strategy_types import EvidenceStrategy, ToolCallSpec


def _build_registry(
    strategies: list[EvidenceStrategy],
) -> dict[str, EvidenceStrategy]:
    by_id: dict[str, EvidenceStrategy] = {}
    for strategy in strategies:
        if strategy.id in by_id:
            raise ValueError(f"duplicate evidence strategy id: {strategy.id}")
        by_id[strategy.id] = strategy
    return by_id


STRATEGIES_BY_ID: dict[str, EvidenceStrategy] = {}


# ── claim.* 策略延迟注册（避免 capability → rules.types ← rules.__init__ 循环导入）──

def _register_claim_strategies() -> None:
    from codeguard_agent.pipeline.evidence.capability import CLAIM_STRATEGIES

    STRATEGIES_BY_ID.update(_build_registry(list(CLAIM_STRATEGIES)))


_register_claim_strategies()


__all__ = [
    "EvidenceStrategy",
    "ToolCallSpec",
    "STRATEGIES_BY_ID",
    "resolve_candidate_tag",
]
