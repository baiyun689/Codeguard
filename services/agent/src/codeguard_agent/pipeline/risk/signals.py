"""Risk rule signal construction.

Rules express a small, deterministic priority (1..3).  This module owns the
single mapping from that priority to match confidence so detectors cannot
silently recreate the removed monolithic risk-score semantics.
"""

from __future__ import annotations

from typing import Literal

from codeguard_agent.models.tasks import RiskSignal, RiskTag

_CONFIDENCE_BY_PRIORITY = {1: 0.45, 2: 0.70, 3: 0.85}


def make_risk_signal(
    *,
    tag: RiskTag,
    priority: int,
    source: str,
    reason: str,
    line: int | None = None,
    source_kind: Literal["diff_text", "path", "symbol", "ast"] = "diff_text",
) -> RiskSignal:
    """Build one normalized signal from a deterministic rule hit."""
    normalized_priority = min(max(priority, 1), 3)
    confidence = _CONFIDENCE_BY_PRIORITY[normalized_priority]
    if source_kind == "path":
        confidence = min(confidence, 0.60)
    return RiskSignal(
        tag=tag,
        match_confidence=confidence,
        review_priority=normalized_priority,
        source_kind=source_kind,
        source=source,
        reason=reason,
        line=line,
    )


__all__ = ["make_risk_signal"]
