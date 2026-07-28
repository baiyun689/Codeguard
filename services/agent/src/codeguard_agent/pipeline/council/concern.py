"""ConcernAnalyzer：候选组 → 结构化 CandidateConcern。

读取 CandidateGroup 全部成员，确定性提取已有字段。
始终保持 singleton fallback——任何失败都不丢候选。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from codeguard_agent.models.council import (
    CandidateClaim,
    CandidateConcern,
    CandidateIssue,
    ConcernAnalysis,
    ConcernTagResolution,
)
from codeguard_agent.models.tasks import RiskTag, TaskRiskPrior
from codeguard_agent.pipeline.council.dedup import CandidateGroup

# Rebuild models that reference RiskTag as a forward reference
# (RiskTag is imported under TYPE_CHECKING in models/council to avoid circular imports)
import codeguard_agent.models.council as _council_mod
_CouncilNS = {'RiskTag': RiskTag}
_CouncilNS.update({k: v for k, v in _council_mod.__dict__.items() if not k.startswith('_')})
ConcernTagResolution.model_rebuild(_types_namespace=_CouncilNS)
CandidateConcern.model_rebuild(_types_namespace=_CouncilNS)

logger = logging.getLogger("codeguard")


def _extract_claim_from_candidate(candidate: CandidateIssue) -> CandidateClaim:
    """从单个候选确定性提取已有结构化字段。"""
    unresolved: list[str] = []
    if not candidate.suggestion:
        unresolved.append("fix_action")
    # trigger 和 observable_consequence 需要 LLM 解析（当前阶段确定性不可得）
    unresolved.extend(["trigger", "observable_consequence"])

    return CandidateClaim(
        root_cause=candidate.claim,
        trigger="",
        observable_consequence="",
        fix_location=f"{candidate.file}:{candidate.line}" if candidate.line else candidate.file,
        fix_action=candidate.suggestion,
        unresolved_fields=tuple(unresolved),
    )


def _extract_tags_from_members(
    members: Sequence[CandidateIssue],
    group: CandidateGroup | None = None,
) -> ConcernTagResolution:
    """从成员和 group 聚合候选标签。"""
    all_tags: list[RiskTag] = []

    # 从 member type 字段提取
    for member in members:
        try:
            tag = RiskTag(member.type)
            if tag is not RiskTag.GENERAL_REVIEW:
                all_tags.append(tag)
        except ValueError:
            pass

    # 从 group 的 primary_risk_tag 获取
    if group is not None and group.primary_risk_tag is not RiskTag.GENERAL_REVIEW:
        if group.primary_risk_tag not in all_tags:
            all_tags.insert(0, group.primary_risk_tag)

    # 去重保持顺序
    seen: set[RiskTag] = set()
    unique: list[RiskTag] = []
    for tag in all_tags:
        if tag not in seen:
            seen.add(tag)
            unique.append(tag)

    concrete = [t for t in unique if t is not RiskTag.GENERAL_REVIEW]
    if not concrete:
        return ConcernTagResolution(
            source="unclassified",
            reasons=("no concrete tags from members or group",),
        )

    primary = concrete[0]
    secondary = tuple(concrete[1:3])  # max 2
    return ConcernTagResolution(
        primary_tag=primary,
        secondary_tags=secondary,
        confidence=0.85,
        source="deterministic",
        reasons=(f"aggregated from {len(members)} members and group",),
    )


def _members_share_core(
    claims: Sequence[CandidateClaim],
) -> bool:
    """判定成员是否可以共享一个 concern。"""
    if len(claims) <= 1:
        return True
    first_rc = claims[0].root_cause[:80].strip().lower()
    if not first_rc:
        return False
    return all(
        c.root_cause[:80].strip().lower() == first_rc
        for c in claims[1:]
    )


def _build_singleton_concern(
    candidate: CandidateIssue,
) -> CandidateConcern:
    """为单个候选构造最小 CandidateConcern（singleton fallback）。"""
    claim = _extract_claim_from_candidate(candidate)
    return CandidateConcern(
        member_candidate_ids=(candidate.id,),
        claims=(claim,),
        tags=_extract_tags_from_members((candidate,)),
        source_agents=(candidate.source_agent,),
        task_ids=(candidate.task_id,),
        files=(candidate.file,),
        confidence=candidate.confidence,
    )


def analyze_candidate_groups(
    groups: Sequence[CandidateGroup],
    *,
    task_priors: Mapping[str, TaskRiskPrior] | None = None,
    llm: Any = None,
) -> ConcernAnalysis:
    """从候选组无损构造 CandidateConcern 列表。

    不修改候选去重判定。无法形成共享 concern 时自动拆组为 singleton。
    保证：输入 candidate IDs 的集合 == concerns 覆盖的 member_candidate_ids 集合。
    """
    concerns: list[CandidateConcern] = []
    candidate_to_concern: dict[str, str] = {}
    diagnostics: list[str] = []

    for group in groups:
        members = list(group.members)
        if not members:
            continue

        claims = [_extract_claim_from_candidate(m) for m in members]

        if _members_share_core(claims):
            concern = CandidateConcern(
                group_id=group.id,
                member_candidate_ids=tuple(m.id for m in members),
                claims=tuple(claims),
                tags=_extract_tags_from_members(members, group),
                member_risk_tags={
                    m.id: (group.primary_risk_tag,)
                    for m in members
                },
                source_agents=tuple(dict.fromkeys(m.source_agent for m in members)),
                task_ids=tuple(dict.fromkeys(m.task_id for m in members)),
                files=tuple(dict.fromkeys(m.file for m in members)),
                confidence=group.confidence,
            )
            concerns.append(concern)
            for m in members:
                candidate_to_concern[m.id] = concern.concern_id
        else:
            diagnostics.append(
                f"group {group.id} split: members do not share core claim"
            )
            for m in members:
                singleton = _build_singleton_concern(m)
                concerns.append(singleton)
                candidate_to_concern[m.id] = singleton.concern_id

    # 验证覆盖率
    all_candidate_ids = {m.id for g in groups for m in g.members}
    covered = set(candidate_to_concern.keys())
    missing = all_candidate_ids - covered
    if missing:
        diagnostics.append(
            f"candidate coverage gap: {sorted(missing)}"
        )

    return ConcernAnalysis(
        concerns=tuple(concerns),
        candidate_to_concern=candidate_to_concern,
        diagnostics=tuple(diagnostics),
    )


def build_singleton_concerns(
    candidates: Sequence[CandidateIssue],
) -> ConcernAnalysis:
    """为无 CandidateGroup 的候选构造 singleton concerns（兼容旧路径）。"""
    concerns: list[CandidateConcern] = []
    candidate_to_concern: dict[str, str] = {}
    for c in candidates:
        concern = _build_singleton_concern(c)
        concerns.append(concern)
        candidate_to_concern[c.id] = concern.concern_id
    return ConcernAnalysis(
        concerns=tuple(concerns),
        candidate_to_concern=candidate_to_concern,
    )
