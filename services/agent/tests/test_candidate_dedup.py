"""候选归并模块的确定性逻辑测试（排序、分块、校验、应用）。"""

from __future__ import annotations

import threading
import time

import pytest

from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.schemas import Severity
from codeguard_agent.pipeline.candidate_dedup import (
    CANDIDATE_LINE_WINDOW,
    MIN_DEDUP_CONFIDENCE,
    CandidateDedupDecision,
    CandidateDedupResult,
    DuplicateGroup,
    _build_candidate_blocks,
    _canonical_candidates,
    _apply_decision,
    _CandidateBlock,
    deduplicate_candidates,
)


def _candidate(
    cid: str,
    *,
    file: str = "src/OrderService.java",
    line: int = 10,
    task_id: str = "src/OrderService.java#h0",
    source: str = "behavior",
    typ: str = "error handling",
    claim: str = "claim",
) -> CandidateIssue:
    return CandidateIssue(
        id=cid,
        task_id=task_id,
        source_agent=source,
        file=file,
        line=line,
        type=typ,
        severity_proposal=Severity.WARNING,
        claim=claim,
        confidence=0.8,
    )


def _group(*ids: str, representative: str, confidence: float = 0.95):
    return DuplicateGroup(
        member_ids=list(ids),
        representative_id=representative,
        same_root_cause=True,
        same_affected_behavior=True,
        single_fix_resolves_all=True,
        confidence=confidence,
        reason="one fix removes all reports",
    )


# ── canonical ordering ──


def test_different_directories_with_same_basename_never_share_block():
    candidates = [
        _candidate("a", file="service/A.java", line=10),
        _candidate("b", file="model/A.java", line=11),
    ]
    blocks = _build_candidate_blocks(_canonical_candidates(candidates))
    assert [tuple(c.id for c in block.candidates) for block in blocks] == [
        ("b",),
        ("a",),
    ]


def test_same_file_same_task_or_five_line_window_share_block():
    candidates = [
        _candidate("a", line=10, task_id="task-a"),
        _candidate("b", line=15, task_id="task-b"),
        _candidate("c", line=40, task_id="task-c"),
        _candidate("d", line=80, task_id="task-c"),
    ]
    blocks = _build_candidate_blocks(_canonical_candidates(candidates))
    assert [tuple(c.id for c in block.candidates) for block in blocks] == [
        ("a", "b"),
        ("c", "d"),
    ]


def test_six_line_gap_in_different_tasks_stays_separate():
    candidates = [
        _candidate("a", line=10, task_id="task-a"),
        _candidate("b", line=16, task_id="task-b"),
    ]
    blocks = _build_candidate_blocks(_canonical_candidates(candidates))
    assert all(len(block.candidates) == 1 for block in blocks)


def test_canonical_order_ignores_fan_in_arrival_order():
    candidates = [
        _candidate("b", line=11, source="maintainability"),
        _candidate("a", line=10, source="threat_model"),
    ]
    forward = [c.id for c in _canonical_candidates(candidates)]
    reverse = [c.id for c in _canonical_candidates(list(reversed(candidates)))]
    assert forward == reverse == ["a", "b"]


# ── validation & application ──


def test_valid_group_keeps_existing_representative_at_earliest_member_position():
    block = _CandidateBlock(
        id="block-1",
        candidates=(
            _candidate("a", line=10),
            _candidate("b", line=12),
            _candidate("c", line=14),
        ),
    )
    result = _apply_decision(
        block,
        CandidateDedupDecision(groups=[_group("a", "b", representative="b")]),
    )
    assert [candidate.id for candidate in result.candidates] == ["b", "c"]


@pytest.mark.parametrize(
    "group,reason",
    [
        (_group("a", representative="a"), "too_few_members"),
        (_group("a", "missing", representative="a"), "unknown_member"),
        (_group("a", "b", representative="missing"), "invalid_representative"),
        (_group("a", "b", representative="a", confidence=0.89), "low_confidence"),
    ],
)
def test_invalid_group_retains_every_candidate(group, reason):
    block = _CandidateBlock(
        id="block-1",
        candidates=(_candidate("a", line=10), _candidate("b", line=12)),
    )
    result = _apply_decision(
        block,
        CandidateDedupDecision(groups=[group]),
    )
    assert [candidate.id for candidate in result.candidates] == ["a", "b"]
    assert result.rejected_groups[0].reason == reason


def test_overlapping_groups_are_all_rejected():
    block = _CandidateBlock(
        id="block-1",
        candidates=(
            _candidate("a", line=10),
            _candidate("b", line=11),
            _candidate("c", line=12),
        ),
    )
    decision = CandidateDedupDecision(
        groups=[
            _group("a", "b", representative="a"),
            _group("b", "c", representative="c"),
        ]
    )
    result = _apply_decision(block, decision)
    assert [candidate.id for candidate in result.candidates] == ["a", "b", "c"]


def test_false_semantic_booleans_rejected():
    block = _CandidateBlock(
        id="block-1",
        candidates=(_candidate("a", line=10), _candidate("b", line=12)),
    )
    group = DuplicateGroup(
        member_ids=["a", "b"],
        representative_id="a",
        same_root_cause=False,
        same_affected_behavior=True,
        single_fix_resolves_all=True,
        confidence=0.95,
        reason="partial match",
    )
    result = _apply_decision(block, CandidateDedupDecision(groups=[group]))
    assert [candidate.id for candidate in result.candidates] == ["a", "b"]
    assert result.rejected_groups[0].reason == "semantic_criteria_not_met"


def test_blank_reason_rejected():
    block = _CandidateBlock(
        id="block-1",
        candidates=(_candidate("a", line=10), _candidate("b", line=12)),
    )
    group = _group("a", "b", representative="a")
    group = group.model_copy(update={"reason": "  "})
    result = _apply_decision(block, CandidateDedupDecision(groups=[group]))
    assert [candidate.id for candidate in result.candidates] == ["a", "b"]
    assert result.rejected_groups[0].reason == "empty_reason"


def test_cross_file_members_rejected():
    block = _CandidateBlock(
        id="block-1",
        candidates=(
            _candidate("a", file="src/A.java", line=10),
            _candidate("b", file="src/A.java", line=12),
        ),
    )
    group = DuplicateGroup(
        member_ids=["a", "b"],
        representative_id="a",
        same_root_cause=True,
        same_affected_behavior=True,
        single_fix_resolves_all=True,
        confidence=0.95,
        reason="merge",
    )
    result = _apply_decision(block, CandidateDedupDecision(groups=[group]))
    assert [candidate.id for candidate in result.candidates] == ["a"]


def test_connected_chain_with_nonadjacent_pair_fully_rejected():
    """a-b 相邻 (line 10-15, same task), b-c 相邻 (line 15, line 40, diff task but <=5)，
    但 a-c 既不同 task 也不在 5 行内 → 整组被拒绝。"""
    block = _CandidateBlock(
        id="block-1",
        candidates=(
            _candidate("a", line=10, task_id="task-a"),
            _candidate("b", line=15, task_id="task-a"),
            _candidate("c", line=40, task_id="task-c"),
        ),
    )
    group = _group("a", "b", "c", representative="a")
    result = _apply_decision(block, CandidateDedupDecision(groups=[group]))
    assert [candidate.id for candidate in result.candidates] == ["a", "b", "c"]
    assert result.rejected_groups[0].reason == "members_outside_locality"


# ── public interface (no-LLM) ──


def test_deduplicate_without_llm_only_canonicalizes_and_keeps_candidates():
    candidates = [
        _candidate("b", line=12),
        _candidate("a", line=10),
    ]
    result = deduplicate_candidates(
        candidates,
        tasks_by_id={},
        tag_resolutions={},
        llm=None,
        structured_method="function_calling",
    )
    assert [candidate.id for candidate in result.candidates] == ["a", "b"]
    assert result.llm_call_count == 0
    assert result.accepted_groups == ()
