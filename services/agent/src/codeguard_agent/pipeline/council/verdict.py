"""裁决模块:确定性门控 + LLM 终审 + 组内合并(ADR-046)。

门控依赖关系分析产出;门控本身零 LLM。终审与组内合并由后续 Task 补入本模块。
"""

from __future__ import annotations

import logging
from typing import Sequence

from codeguard_agent.models.council import FactRelation

logger = logging.getLogger("codeguard")


def gate_candidate(relations: Sequence[FactRelation]) -> tuple[str, str] | None:
    """三条确定性证据门控(零 LLM 成本淘汰)。返回 (reason_code, reason) 表示应 drop。"""
    if any(
        item.relation == "contradicts" and item.strength == "direct"
        for item in relations
    ):
        return "direct_counter_evidence", "直接反证足以排除候选"
    if not relations or all(
        item.relation == "insufficient" for item in relations
    ):
        return "evidence_insufficient", "候选没有可用证据"
    if not any(item.relation == "supports" for item in relations):
        return "no_supporting_evidence", "没有 support 证据支持候选主张"
    return None
