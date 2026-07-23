"""ReviewCouncil 候选归并模块。

在 Coordinator fan-in 后执行一次：规范化排序、局部性分块、保守校验、稳定应用。
LLM 语义归并在 deduplicate_candidates() 中通过可注入的 _invoke_block seam 实现。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.tasks import ReviewTask, RiskTag
from codeguard_agent.pipeline import context_rules
from codeguard_agent.pipeline.evidence_rules.classify import CandidateTagResolution

logger = logging.getLogger("codeguard")

MIN_DEDUP_CONFIDENCE = 0.90
CANDIDATE_LINE_WINDOW = 5
MAX_DEDUP_WORKERS = 8

_SOURCE_ORDER = {
    "threat_model": 0,
    "behavior": 1,
    "maintainability": 2,
}


# ── LLM 结构化输出模型 ──


class DuplicateGroup(BaseModel):
    """LLM 归并建议：一组描述同一底层问题的候选。"""

    member_ids: list[str]
    representative_id: str
    same_root_cause: bool
    same_affected_behavior: bool
    single_fix_resolves_all: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


class CandidateDedupDecision(BaseModel):
    """LLM 对单个 block 的归并决策。"""

    groups: list[DuplicateGroup] = Field(default_factory=list)


# ── 结果模型 ──


@dataclass(frozen=True)
class AcceptedCandidateGroup:
    member_ids: tuple[str, ...]
    representative_id: str
    confidence: float
    reason: str


@dataclass(frozen=True)
class RejectedCandidateGroup:
    member_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class _CandidateBlock:
    id: str
    candidates: tuple[CandidateIssue, ...]


@dataclass(frozen=True)
class _BlockApplyResult:
    candidates: tuple[CandidateIssue, ...]
    accepted_groups: tuple[AcceptedCandidateGroup, ...]
    rejected_groups: tuple[RejectedCandidateGroup, ...]


@dataclass(frozen=True)
class _BlockDecisionOutcome:
    decision: CandidateDedupDecision | None
    failure: str = ""


@dataclass(frozen=True)
class CandidateDedupResult:
    candidates: tuple[CandidateIssue, ...]
    raw_candidate_count: int
    block_count: int
    multi_member_block_count: int
    llm_call_count: int
    accepted_groups: tuple[AcceptedCandidateGroup, ...]
    rejected_groups: tuple[RejectedCandidateGroup, ...]
    block_failures: tuple[str, ...]


# ── 排序 ──


def _candidate_sort_key(candidate: CandidateIssue) -> tuple[object, ...]:
    path = context_rules.normalize_path(candidate.file)
    line_key = (0, candidate.line) if candidate.line > 0 else (1, 0)
    return (
        path,
        line_key,
        candidate.task_id,
        _SOURCE_ORDER.get(candidate.source_agent, 99),
        candidate.source_agent,
        candidate.id,
    )


def _canonical_candidates(
    candidates: Sequence[CandidateIssue],
) -> tuple[CandidateIssue, ...]:
    return tuple(sorted(candidates, key=_candidate_sort_key))


# ── 局部性判断 ──


def _adjacent(left: CandidateIssue, right: CandidateIssue) -> bool:
    if context_rules.normalize_path(left.file) != context_rules.normalize_path(
        right.file
    ):
        return False
    if left.task_id == right.task_id:
        return True
    return (
        left.line > 0
        and right.line > 0
        and abs(left.line - right.line) <= CANDIDATE_LINE_WINDOW
    )


# ── 连通分量分块 ──


def _build_candidate_blocks(
    ordered: tuple[CandidateIssue, ...],
) -> tuple[_CandidateBlock, ...]:
    """按规范化顺序扫描，相邻连通构建块。"""
    if not ordered:
        return ()
    blocks: list[_CandidateBlock] = []
    current: list[CandidateIssue] = [ordered[0]]
    for candidate in ordered[1:]:
        if _adjacent(current[-1], candidate):
            current.append(candidate)
        else:
            blocks.append(
                _CandidateBlock(
                    id=f"block-{len(blocks)}",
                    candidates=tuple(current),
                )
            )
            current = [candidate]
    blocks.append(
        _CandidateBlock(
            id=f"block-{len(blocks)}",
            candidates=tuple(current),
        )
    )
    return tuple(blocks)


# ── 校验 ──


def _group_rejection_reason(
    block: _CandidateBlock,
    group: DuplicateGroup,
    overlapping_ids: set[str],
) -> str | None:
    member_ids = tuple(dict.fromkeys(group.member_ids))
    known = {candidate.id: candidate for candidate in block.candidates}
    if len(member_ids) < 2:
        return "too_few_members"
    if any(member_id not in known for member_id in member_ids):
        return "unknown_member"
    if group.representative_id not in set(member_ids):
        return "invalid_representative"
    if any(member_id in overlapping_ids for member_id in member_ids):
        return "overlapping_group"
    if not group.reason.strip():
        return "empty_reason"
    if group.confidence < MIN_DEDUP_CONFIDENCE:
        return "low_confidence"
    if not (
        group.same_root_cause
        and group.same_affected_behavior
        and group.single_fix_resolves_all
    ):
        return "semantic_criteria_not_met"
    members = [known[member_id] for member_id in member_ids]
    if any(
        not _adjacent(left, right)
        for index, left in enumerate(members)
        for right in members[index + 1 :]
    ):
        return "members_outside_locality"
    return None


def _apply_decision(
    block: _CandidateBlock,
    decision: CandidateDedupDecision,
) -> _BlockApplyResult:
    """保守校验每个 group，通过的保留代表，拒绝的保留全部原候选。"""
    accepted: list[AcceptedCandidateGroup] = []
    rejected: list[RejectedCandidateGroup] = []

    # 先检测重叠
    overlapping_ids: set[str] = set()
    id_to_groups: dict[str, list[int]] = {}
    for index, group in enumerate(decision.groups):
        for member_id in dict.fromkeys(group.member_ids):
            id_to_groups.setdefault(member_id, []).append(index)
    for member_id, group_indices in id_to_groups.items():
        if len(group_indices) > 1:
            overlapping_ids.add(member_id)

    kept_ids: set[str] = set()
    removed_ids: set[str] = set()
    for group in decision.groups:
        reason = _group_rejection_reason(block, group, overlapping_ids)
        member_ids = tuple(dict.fromkeys(group.member_ids))
        if reason is not None:
            rejected.append(RejectedCandidateGroup(member_ids, reason))
            continue
        kept_ids.add(group.representative_id)
        removed_ids.update(mid for mid in member_ids if mid != group.representative_id)
        accepted.append(
            AcceptedCandidateGroup(
                member_ids=member_ids,
                representative_id=group.representative_id,
                confidence=group.confidence,
                reason=group.reason,
            )
        )

    survivors: list[CandidateIssue] = []
    for candidate in block.candidates:
        if candidate.id in removed_ids:
            continue
        survivors.append(candidate)

    return _BlockApplyResult(
        candidates=tuple(survivors),
        accepted_groups=tuple(accepted),
        rejected_groups=tuple(rejected),
    )


# ── 公开接口 ──


def deduplicate_candidates(
    candidates: Sequence[CandidateIssue],
    *,
    tasks_by_id: Mapping[str, ReviewTask],
    tag_resolutions: Mapping[str, CandidateTagResolution],
    llm: Any,
    structured_method: str,
    max_workers: int = MAX_DEDUP_WORKERS,
) -> CandidateDedupResult:
    """执行一次候选归并：排序 → 分块 → (可选 LLM) → 校验 → 应用。

    llm=None 时跳过语义归并，仅做规范化排序（用于无工具/mock 路径）。
    """
    # 1. ID 去重
    seen: set[str] = set()
    unique: list[CandidateIssue] = []
    for candidate in candidates:
        if candidate.id in seen:
            continue
        seen.add(candidate.id)
        unique.append(candidate)

    raw_count = len(unique)

    # 2. 规范化排序
    ordered = _canonical_candidates(unique)

    # 3. 局部性分块
    blocks = _build_candidate_blocks(ordered)

    # 4. 对多成员块调 LLM（单成员直接保留）
    multi = [b for b in blocks if len(b.candidates) >= 2]
    singles = [b for b in blocks if len(b.candidates) == 1]

    block_decisions: dict[str, _BlockDecisionOutcome] = {}
    llm_call_count = 0
    block_failures: list[str] = []

    if llm is not None and multi:
        from codeguard_agent.pipeline.concurrency import run_bounded_parallel

        outcomes = run_bounded_parallel(
            multi,
            lambda block: _invoke_block(
                block,
                tasks_by_id=tasks_by_id,
                tag_resolutions=tag_resolutions,
                llm=llm,
                structured_method=structured_method,
            ),
            max_workers=max_workers,
        )
        for block, outcome in zip(multi, outcomes, strict=True):
            if outcome is None:
                block_failures.append(block.id)
                continue
            block_decisions[block.id] = outcome
            if outcome.decision is not None:
                llm_call_count += 1
            if outcome.failure:
                block_failures.append(block.id)

    # 5. 组装结果
    all_candidates: list[CandidateIssue] = []
    all_accepted: list[AcceptedCandidateGroup] = []
    all_rejected: list[RejectedCandidateGroup] = []

    for block in blocks:
        if len(block.candidates) == 1:
            all_candidates.extend(block.candidates)
            continue
        outcome = block_decisions.get(block.id)
        if outcome is None or outcome.decision is None:
            all_candidates.extend(block.candidates)
            continue
        result = _apply_decision(block, outcome.decision)
        all_candidates.extend(result.candidates)
        all_accepted.extend(result.accepted_groups)
        all_rejected.extend(result.rejected_groups)

    return CandidateDedupResult(
        candidates=tuple(all_candidates),
        raw_candidate_count=raw_count,
        block_count=len(blocks),
        multi_member_block_count=len(multi),
        llm_call_count=llm_call_count,
        accepted_groups=tuple(all_accepted),
        rejected_groups=tuple(all_rejected),
        block_failures=tuple(block_failures),
    )


# ── LLM 适配器（Task 3 实现） ──


def _invoke_block(
    block: _CandidateBlock,
    *,
    tasks_by_id: Mapping[str, ReviewTask],
    tag_resolutions: Mapping[str, CandidateTagResolution],
    llm: Any,
    structured_method: str,
) -> _BlockDecisionOutcome:
    """调用结构化 LLM 对单个 block 做语义归并。

    Task 2 阶段为 no-LLM 占位；Task 3 接入真实的 prompt + LLM 调用。
    """
    return _BlockDecisionOutcome(decision=None, failure="llm_not_configured")


__all__ = [
    "CANDIDATE_LINE_WINDOW",
    "MIN_DEDUP_CONFIDENCE",
    "MAX_DEDUP_WORKERS",
    "AcceptedCandidateGroup",
    "CandidateDedupDecision",
    "CandidateDedupResult",
    "DuplicateGroup",
    "RejectedCandidateGroup",
    "_CandidateBlock",
    "_BlockApplyResult",
    "_BlockDecisionOutcome",
    "_apply_decision",
    "_build_candidate_blocks",
    "_canonical_candidates",
    "_invoke_block",
    "deduplicate_candidates",
]
