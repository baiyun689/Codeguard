"""基于 Cause/Effect 的候选语义合并。

该模块只处理 EvidenceJudge 已保留的候选。LLM 提取因果画像并比较候选，
确定性代码根据 pairwise 关系生成合并组；任何异常或不确定结果都保留原候选。
"""

from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.council import (
    CandidateIssue,
    CausalAnalysisBatch,
    CausalComparison,
    CausalMergeGroup,
    CausalProfile,
)
from codeguard_agent.models.evidence import CandidateVerification
from codeguard_agent.models.schemas import Issue, Severity

logger = logging.getLogger("codeguard")

_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"
_BATCH_SIZE = 8
_MAX_CONCURRENCY = 4
_MAX_ATTEMPTS = 2
_UNKNOWN = "unknown"
_SEVERITY_ORDER = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.CRITICAL: 2,
}


@dataclass
class CausalMergeResult:
    final_issues: list[Issue]
    profiles: dict[str, CausalProfile] = field(default_factory=dict)
    comparisons: list[CausalComparison] = field(default_factory=list)
    groups: list[CausalMergeGroup] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    trace: list[tuple[str, str]] = field(default_factory=list)


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


def _candidate_payload(
    candidate: CandidateIssue,
    verification: CandidateVerification | None,
) -> dict[str, object]:
    evidence = []
    if verification is not None:
        evidence = [
            {
                "evidence_id": item.artifact_id,
                "source_kind": item.source_kind.value,
                "tool": item.tool,
                "content": item.content[:2000],
                "limitations": list(item.limitations),
            }
            for item in verification.valid_evidence
        ]
    return {
        "candidate_id": candidate.id,
        "message": candidate.claim,
        "suggestion": candidate.suggestion,
        "file": candidate.file,
        "line": candidate.line,
        "type": candidate.type,
        "verified_evidence": evidence,
    }


def _build_user_prompt(
    candidates: Sequence[CandidateIssue],
    verifications: dict[str, CandidateVerification],
    error: str = "",
) -> str:
    payload: dict[str, object] = {
        "candidates": [
            _candidate_payload(candidate, verifications.get(candidate.id))
            for candidate in candidates
        ],
    }
    if error:
        payload["previous_validation_error"] = error
    return _load_prompt("causal-merge-user.txt").replace(
        "{{candidates}}", _stable_json(payload)
    )


def _valid_profile(
    profile: CausalProfile,
    candidate_ids: set[str],
    verifications: dict[str, CandidateVerification],
) -> bool:
    if profile.candidate_id not in candidate_ids:
        return False
    if not profile.defect_mechanism.strip() or not profile.trigger.strip():
        return False
    if not profile.runtime_consequence.strip() or not profile.affected_scope.strip():
        return False
    if not profile.observable_behavior.strip() or not profile.location:
        return False
    verification = verifications.get(profile.candidate_id)
    if verification is None:
        return False
    valid_ids = {
        item.artifact_id
        for item in verification.valid_evidence
    }
    referenced = set(profile.cause_evidence_ids) | set(profile.effect_evidence_ids)
    return bool(referenced) and referenced <= valid_ids


def _valid_comparison(comparison: CausalComparison, candidate_ids: set[str]) -> bool:
    return (
        comparison.left_candidate_id in candidate_ids
        and comparison.right_candidate_id in candidate_ids
        and comparison.left_candidate_id != comparison.right_candidate_id
    )


def _profile_has_known_cause(profile: CausalProfile) -> bool:
    return (
        profile.defect_mechanism.lower() != _UNKNOWN
        and profile.trigger.lower() != _UNKNOWN
        and bool(profile.location)
        and all(item.strip().lower() != _UNKNOWN for item in profile.location)
    )


def _profile_has_known_effect(profile: CausalProfile) -> bool:
    return all(
        value.strip().lower() != _UNKNOWN
        for value in (
            profile.runtime_consequence,
            profile.affected_scope,
            profile.observable_behavior,
        )
    )


def _invoke_batch(
    candidates: Sequence[CandidateIssue],
    verifications: dict[str, CandidateVerification],
    *,
    llm: Any,
    structured_method: str,
) -> tuple[CausalAnalysisBatch | None, str]:
    if llm is None:
        return None, "llm_unavailable"

    structured = llm.with_structured_output(
        CausalAnalysisBatch,
        method=structured_method,
    )
    candidate_ids = {candidate.id for candidate in candidates}
    validation_error = ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            result = invoke_with_retry(
                structured,
                [
                    ("system", _load_prompt("causal-merge-system.txt")),
                    ("human", _build_user_prompt(candidates, verifications, validation_error)),
                ],
                max_retries=1,
            )
        except Exception as exc:  # noqa: BLE001
            validation_error = f"调用失败:{type(exc).__name__}"
            logger.warning("causal merge attempt %d failed: %s", attempt, exc)
            continue
        if result is None or not isinstance(result, CausalAnalysisBatch):
            validation_error = "返回不是有效的 CausalAnalysisBatch"
            continue
        if len({profile.candidate_id for profile in result.profiles}) != len(result.profiles):
            validation_error = "profiles 中存在重复 candidate_id"
            continue
        if {profile.candidate_id for profile in result.profiles} != candidate_ids:
            validation_error = "profiles 必须覆盖当前批次的全部候选"
            continue
        if not all(
            _valid_profile(profile, candidate_ids, verifications)
            for profile in result.profiles
        ):
            validation_error = "profile 缺少可用字段或引用了未验证证据"
            continue
        if not all(
            _valid_comparison(comparison, candidate_ids)
            for comparison in result.comparisons
        ):
            validation_error = "comparison 引用了未知或重复 candidate_id"
            continue
        return result, ""
    return None, validation_error or "causal_merge_failed"


def _comparison_map(
    comparisons: Sequence[CausalComparison],
) -> dict[frozenset[str], CausalComparison]:
    return {
        frozenset((comparison.left_candidate_id, comparison.right_candidate_id)): comparison
        for comparison in comparisons
    }


def _build_groups(
    candidates: Sequence[CandidateIssue],
    comparisons: Sequence[CausalComparison],
    profiles: dict[str, CausalProfile],
) -> list[CausalMergeGroup]:
    relation_map = _comparison_map(comparisons)

    def is_duplicate(left_id: str, right_id: str) -> bool:
        comparison = relation_map.get(frozenset((left_id, right_id)))
        left = profiles.get(left_id)
        right = profiles.get(right_id)
        if comparison is None or left is None or right is None:
            return False
        cause_known = _profile_has_known_cause(left) and _profile_has_known_cause(right)
        effect_known = _profile_has_known_effect(left) and _profile_has_known_effect(right)
        return (
            comparison.same_cause is True
            and comparison.same_effect is True
            and cause_known
            and effect_known
        )

    groups: list[list[str]] = []
    for candidate in candidates:
        placed = False
        for group in groups:
            if all(
                is_duplicate(candidate.id, member_id)
                for member_id in group
            ):
                group.append(candidate.id)
                placed = True
                break
        if not placed:
            groups.append([candidate.id])

    result: list[CausalMergeGroup] = []
    for member_ids in groups:
        if len(member_ids) < 2:
            continue
        digest = hashlib.sha256("\0".join(member_ids).encode()).hexdigest()[:16]
        result.append(CausalMergeGroup(id=f"causal-group-{digest}", member_ids=tuple(member_ids)))
    return result


def _merge_issue_group(
    member_ids: Sequence[str],
    issue_by_id: dict[str, Issue],
) -> Issue:
    members = [issue_by_id[member_id] for member_id in member_ids]
    primary = members[0]
    unique_types = list(dict.fromkeys(member.type for member in members if member.type))
    unique_messages = list(dict.fromkeys(member.message for member in members if member.message))
    unique_suggestions = list(dict.fromkeys(member.suggestion for member in members if member.suggestion))
    severity = max(members, key=lambda issue: _SEVERITY_ORDER[issue.severity]).severity
    return primary.model_copy(update={
        "severity": severity,
        "type": " / ".join(unique_types),
        "message": "；".join(unique_messages),
        "suggestion": "；".join(unique_suggestions),
        "confidence": min(member.confidence for member in members),
    })


def merge_survivors(
    candidates: Sequence[CandidateIssue],
    survivor_ids: Sequence[str],
    survivor_issues: Sequence[Issue],
    verifications: dict[str, CandidateVerification],
    *,
    llm: Any,
    structured_method: str,
) -> CausalMergeResult:
    """对 EvidenceJudge survivors 做 Cause/Effect 分析并保守合并。"""
    issue_by_id = {
        candidate_id: issue
        for candidate_id, issue in zip(survivor_ids, survivor_issues, strict=False)
    }
    survivor_candidates = [candidate for candidate in candidates if candidate.id in issue_by_id]
    all_profiles: dict[str, CausalProfile] = {}
    all_comparisons: list[CausalComparison] = []
    groups: list[CausalMergeGroup] = []
    trace: list[tuple[str, str]] = []
    attempted = 0
    succeeded = 0
    failed = 0

    buckets: dict[tuple[str, str], list[CandidateIssue]] = {}
    for candidate in survivor_candidates:
        buckets.setdefault((candidate.task_id, candidate.file), []).append(candidate)

    # task/file 是确定性边界：不同审查任务之间不做语义去重。
    batches: list[tuple[tuple[str, str], list[CandidateIssue]]] = []
    for bucket_key, bucket in buckets.items():
        for offset in range(0, len(bucket), _BATCH_SIZE):
            batch = bucket[offset:offset + _BATCH_SIZE]
            if len(batch) >= 2:
                batches.append((bucket_key, batch))

    def analyze(item: tuple[tuple[str, str], list[CandidateIssue]]):
        bucket_key, batch = item
        analysis, error = _invoke_batch(
            batch,
            verifications,
            llm=llm,
            structured_method=structured_method,
        )
        return bucket_key, batch, analysis, error

    if batches:
        with ThreadPoolExecutor(
            max_workers=min(_MAX_CONCURRENCY, len(batches))
        ) as pool:
            results = list(pool.map(analyze, batches))
    else:
        results = []

    for bucket_key, batch, analysis, error in results:
        attempted += 1
        if analysis is None:
            failed += 1
            trace.append(("causal_merge_failed", _stable_json({
                "task_id": bucket_key[0],
                "file": bucket_key[1],
                "candidate_ids": [candidate.id for candidate in batch],
                "reason": error,
            })))
            continue
        succeeded += 1
        all_profiles.update({profile.candidate_id: profile for profile in analysis.profiles})
        all_comparisons.extend(analysis.comparisons)
        batch_profiles = {
            profile.candidate_id: profile for profile in analysis.profiles
        }
        batch_groups = _build_groups(batch, analysis.comparisons, batch_profiles)
        groups.extend(batch_groups)
        trace.append(("causal_merge_batch_completed", _stable_json({
            "task_id": bucket_key[0],
            "file": bucket_key[1],
            "candidate_ids": [candidate.id for candidate in batch],
            "comparisons": len(analysis.comparisons),
            "groups": len(batch_groups),
        })))

    group_by_member = {
        member_id: group
        for group in groups
        for member_id in group.member_ids
    }
    emitted: set[str] = set()
    final_issues: list[Issue] = []
    for candidate_id in survivor_ids:
        issue = issue_by_id.get(candidate_id)
        if issue is None:
            continue
        group = group_by_member.get(candidate_id)
        if group is None:
            final_issues.append(issue)
            continue
        if group.id in emitted:
            continue
        emitted.add(group.id)
        final_issues.append(_merge_issue_group(group.member_ids, issue_by_id))

    return CausalMergeResult(
        final_issues=final_issues,
        profiles=all_profiles,
        comparisons=all_comparisons,
        groups=groups,
        stats={
            "batch_count": attempted,
            "successful_batch_count": succeeded,
            "failed_batch_count": failed,
            "merged_group_count": len(groups),
            "merged_candidate_count": sum(len(group.member_ids) for group in groups),
        },
        trace=trace,
    )


__all__ = ["CausalMergeResult", "merge_survivors"]
