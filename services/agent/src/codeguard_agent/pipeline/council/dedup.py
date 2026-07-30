"""ReviewCouncil 候选归并模块。

在 Coordinator fan-in 后执行一次：规范化排序、局部性分块、保守校验、稳定应用。
LLM 语义归并在 deduplicate_candidates() 中通过可注入的 _invoke_block seam 实现。
"""

from __future__ import annotations

import html
import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from pydantic import BaseModel, Field

from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import ReviewTask, RiskTag
from codeguard_agent.pipeline.evidence.rules.classify import CandidateTagResolution

logger = logging.getLogger("codeguard")

MIN_DEDUP_CONFIDENCE = 0.98
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
    same_root_cause: bool
    same_trigger: bool
    same_affected_behavior: bool
    same_observable_consequence: bool
    same_fix_location: bool
    single_fix_resolves_all: bool
    information_lossless: bool
    confidence: float = Field(ge=0.0, le=1.0)
    shared_root_cause: str
    shared_behavior: str
    shared_fix: str


class CandidateDedupDecision(BaseModel):
    """LLM 对单个 block 的归并决策。"""

    groups: list[DuplicateGroup] = Field(default_factory=list)


# ── 结果模型 ──


@dataclass(frozen=True)
class CandidateGroup:
    """严格等价候选的非破坏性逻辑组。"""

    id: str
    members: tuple[CandidateIssue, ...]
    primary_risk_tag: RiskTag
    severity_proposal: Severity
    confidence: float
    shared_root_cause: str
    shared_behavior: str
    shared_fix: str

    @property
    def member_ids(self) -> tuple[str, ...]:
        return tuple(member.id for member in self.members)


@dataclass(frozen=True)
class RejectedCandidateGroup:
    member_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class CandidateBlockFailure:
    block_id: str
    reason: str


class CandidateDedupStats(TypedDict):
    raw_candidate_count: int
    logical_candidate_count: int
    grouped_member_count: int
    removed_count: int
    llm_call_count: int
    block_failure_count: int


@dataclass(frozen=True)
class _CandidateBlock:
    id: str
    candidates: tuple[CandidateIssue, ...]


@dataclass(frozen=True)
class _BlockApplyResult:
    candidates: tuple[CandidateIssue, ...]
    accepted_groups: tuple[CandidateGroup, ...]
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
    accepted_groups: tuple[CandidateGroup, ...]
    rejected_groups: tuple[RejectedCandidateGroup, ...]
    block_failures: tuple[CandidateBlockFailure, ...]

    @property
    def grouped_member_count(self) -> int:
        return sum(max(0, len(group.members) - 1) for group in self.accepted_groups)

    @property
    def logical_candidate_count(self) -> int:
        return self.raw_candidate_count - self.grouped_member_count


# ── 排序 ──


def _normalize_candidate_path(path: str) -> str:
    """规范化 repo-relative Git 路径，保留路径大小写。"""
    return "/".join(
        segment
        for segment in (path or "").replace("\\", "/").split("/")
        if segment not in {"", "."}
    )


def _candidate_sort_key(candidate: CandidateIssue) -> tuple[object, ...]:
    path = _normalize_candidate_path(candidate.file)
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
    if _normalize_candidate_path(left.file) != _normalize_candidate_path(
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
    """按邻接图的连通分量构建稳定候选块。"""
    if not ordered:
        return ()
    blocks: list[_CandidateBlock] = []
    visited: set[int] = set()
    for seed in range(len(ordered)):
        if seed in visited:
            continue
        component: set[int] = {seed}
        pending = [seed]
        visited.add(seed)
        while pending:
            current = pending.pop()
            for other in range(len(ordered)):
                if other in visited:
                    continue
                if _adjacent(ordered[current], ordered[other]):
                    visited.add(other)
                    component.add(other)
                    pending.append(other)
        blocks.append(
            _CandidateBlock(
                id=f"block-{len(blocks)}",
                candidates=tuple(ordered[index] for index in sorted(component)),
            )
        )
    return tuple(blocks)


# ── 校验 ──


def _group_rejection_reason(
    block: _CandidateBlock,
    group: DuplicateGroup,
    overlapping_ids: set[str],
    tag_resolutions: Mapping[str, CandidateTagResolution] | None,
) -> str | None:
    if len(group.member_ids) != len(set(group.member_ids)):
        return "duplicate_member_id"
    member_ids = tuple(group.member_ids)
    known = {candidate.id: candidate for candidate in block.candidates}
    if len(member_ids) < 2:
        return "too_few_members"
    if any(member_id not in known for member_id in member_ids):
        return "unknown_member"
    if any(member_id in overlapping_ids for member_id in member_ids):
        return "overlapping_group"
    if not (
        group.shared_root_cause.strip()
        and group.shared_behavior.strip()
        and group.shared_fix.strip()
    ):
        return "missing_shared_equivalence"
    if group.confidence < MIN_DEDUP_CONFIDENCE:
        return "low_confidence"
    if not (
        group.same_root_cause
        and group.same_trigger
        and group.same_affected_behavior
        and group.same_observable_consequence
        and group.same_fix_location
        and group.single_fix_resolves_all
        and group.information_lossless
    ):
        return "semantic_criteria_not_met"
    members = [known[member_id] for member_id in member_ids]
    if len({_normalize_candidate_path(member.file) for member in members}) != 1:
        return "different_file"
    if len({member.task_id for member in members}) != 1:
        return "different_task"
    # 不同 reviewer 可能对同一 bug 给出不同严重级别/RiskTag——
    # LLM 已通过 same_root_cause + same_fix_location 确认等价，
    # 此处不再以 severity/risk_tag 差异阻断归并。
    if tag_resolutions is not None:
        if any(tag_resolutions.get(member.id) is None for member in members):
            return "missing_tag_resolution"
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
    tag_resolutions: Mapping[str, CandidateTagResolution] | None = None,
) -> _BlockApplyResult:
    """保守校验每个 group，并始终保留全部原候选供后续独立举证。"""
    accepted: list[CandidateGroup] = []
    rejected: list[RejectedCandidateGroup] = []
    resolutions = tag_resolutions

    # 先检测重叠
    overlapping_ids: set[str] = set()
    id_to_groups: dict[str, list[int]] = {}
    for index, group in enumerate(decision.groups):
        for member_id in dict.fromkeys(group.member_ids):
            id_to_groups.setdefault(member_id, []).append(index)
    for member_id, group_indices in id_to_groups.items():
        if len(group_indices) > 1:
            overlapping_ids.add(member_id)

    for group in decision.groups:
        reason = _group_rejection_reason(block, group, overlapping_ids, resolutions)
        member_ids = tuple(group.member_ids)
        if reason is not None:
            rejected.append(RejectedCandidateGroup(member_ids, reason))
            continue
        members = tuple(
            candidate
            for candidate in block.candidates
            if candidate.id in set(member_ids)
        )
        resolution = resolutions.get(members[0].id) if resolutions is not None else None
        digest = hashlib.sha256(
            "\0".join(sorted(member_ids)).encode("utf-8")
        ).hexdigest()[:16]
        accepted.append(
            CandidateGroup(
                id=f"candidate-group-{digest}",
                members=members,
                primary_risk_tag=(
                    resolution.tag if resolution is not None else RiskTag.GENERAL_REVIEW
                ),
                severity_proposal=members[0].severity_proposal,
                confidence=group.confidence,
                shared_root_cause=group.shared_root_cause,
                shared_behavior=group.shared_behavior,
                shared_fix=group.shared_fix,
            )
        )

    return _BlockApplyResult(
        candidates=block.candidates,
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

    block_decisions: dict[str, _BlockDecisionOutcome] = {}
    llm_call_count = len(multi) if llm is not None else 0
    block_failures: list[CandidateBlockFailure] = []

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
            max_workers=max(1, min(max_workers, MAX_DEDUP_WORKERS)),
        )
        for block, outcome in zip(multi, outcomes, strict=True):
            if outcome is None:
                block_failures.append(
                    CandidateBlockFailure(
                        block_id=block.id,
                        reason="parallel_execution_failed",
                    )
                )
                continue
            block_decisions[block.id] = outcome
            if outcome.failure:
                block_failures.append(
                    CandidateBlockFailure(
                        block_id=block.id,
                        reason=outcome.failure,
                    )
                )

    # 5. 组装结果
    all_accepted: list[CandidateGroup] = []
    all_rejected: list[RejectedCandidateGroup] = []

    for block in blocks:
        if len(block.candidates) == 1:
            continue
        outcome = block_decisions.get(block.id)
        if outcome is None or outcome.decision is None:
            continue
        result = _apply_decision(
            block,
            outcome.decision,
            tag_resolutions=tag_resolutions,
        )
        all_accepted.extend(result.accepted_groups)
        all_rejected.extend(result.rejected_groups)

    # 语义组只描述逻辑重复关系，不在证据/裁决前删除任何成员。
    all_candidates = ordered

    return CandidateDedupResult(
        candidates=all_candidates,
        raw_candidate_count=raw_count,
        block_count=len(blocks),
        multi_member_block_count=len(multi),
        llm_call_count=llm_call_count,
        accepted_groups=tuple(all_accepted),
        rejected_groups=tuple(all_rejected),
        block_failures=tuple(block_failures),
    )


# ── Prompt 渲染 ──

_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"


def _load_prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


def _build_user_prompt(
    block: _CandidateBlock,
    tasks_by_id: Mapping[str, ReviewTask],
    tag_resolutions: Mapping[str, CandidateTagResolution],
) -> str:
    """把 block 候选和关联 task 渲染为 JSON 数据，html 转义后嵌入 user prompt。"""
    payload: dict[str, Any] = {
        "block_id": block.id,
        "candidates": [
            {
                "candidate_id": candidate.id,
                "source_agent": candidate.source_agent,
                "file": _normalize_candidate_path(candidate.file),
                "line": candidate.line,
                "task_id": candidate.task_id,
                "type": candidate.type,
                "severity_proposal": candidate.severity_proposal.value,
                "primary_risk_tag": (
                    resolution.tag.value
                    if (resolution := tag_resolutions.get(candidate.id))
                    else RiskTag.GENERAL_REVIEW.value
                ),
                "tag_source": (
                    resolution.source
                    if (resolution := tag_resolutions.get(candidate.id))
                    else "general"
                ),
                "tag_confidence": (
                    resolution.confidence
                    if (resolution := tag_resolutions.get(candidate.id))
                    else 0.5
                ),
                "claim": candidate.claim,
                "suggestion": candidate.suggestion,
            }
            for candidate in block.candidates
        ],
        "tasks": [
            {
                "task_id": task_id,
                "patch": tasks_by_id[task_id].patch,
                "patch_complete": tasks_by_id[task_id].patch_complete,
            }
            for task_id in sorted({c.task_id for c in block.candidates})
            if task_id in tasks_by_id
        ],
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    escaped = html.escape(serialized, quote=False)
    return _load_prompt("candidate-dedup-user.txt").format(dedup_input=escaped)


# ── LLM 适配器 ──


def _invoke_block(
    block: _CandidateBlock,
    *,
    tasks_by_id: Mapping[str, ReviewTask],
    tag_resolutions: Mapping[str, CandidateTagResolution],
    llm: Any,
    structured_method: str,
) -> _BlockDecisionOutcome:
    """调用结构化 LLM 对单个 block 做语义归并。"""
    from codeguard_agent.llm.client import invoke_with_retry

    try:
        system_prompt = _load_prompt("candidate-dedup-system.txt")
        user_prompt = _build_user_prompt(block, tasks_by_id, tag_resolutions)
        structured = llm.with_structured_output(
            CandidateDedupDecision,
            method=structured_method,
        )
        result = invoke_with_retry(
            structured,
            [("system", system_prompt), ("human", user_prompt)],
            max_retries=1,
        )
        if result is None:
            return _BlockDecisionOutcome(decision=None, failure="empty_response")
        if not isinstance(result, CandidateDedupDecision):
            return _BlockDecisionOutcome(decision=None, failure="invalid_response")
        return _BlockDecisionOutcome(decision=result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("候选归并 LLM 调用失败: %s", exc)
        return _BlockDecisionOutcome(decision=None, failure="llm_error")


__all__ = [
    "CANDIDATE_LINE_WINDOW",
    "MIN_DEDUP_CONFIDENCE",
    "MAX_DEDUP_WORKERS",
    "CandidateGroup",
    "CandidateBlockFailure",
    "CandidateDedupDecision",
    "CandidateDedupResult",
    "CandidateDedupStats",
    "DuplicateGroup",
    "RejectedCandidateGroup",
    "deduplicate_candidates",
]
