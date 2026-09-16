"""受控取证后的证据评估与候选绑定。"""

from __future__ import annotations


import re


from pathlib import Path


from codeguard_agent.models.council import CandidateIssue


_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts" / "controlled"


def collapse_candidate_duplicates(
    candidates: list[CandidateIssue],
) -> tuple[list[CandidateIssue], int]:
    """按位置和主张相似度归并重复候选。

    候选须属于同一任务和文件，行号相距不超过三行，且问题说明具有足够的词汇重合。
    此处只进行确定性去重，候选是否成立由后续裁决处理。
    """

    result: list[CandidateIssue] = []
    collapsed = 0
    for candidate in candidates:
        match_index = next(
            (
                index
                for index, existing in enumerate(result)
                if _same_controlled_mechanism(existing, candidate)
            ),
            None,
        )
        if match_index is None:
            result.append(candidate)
            continue
        existing = result[match_index]
        winner, loser = _prefer_candidate(existing, candidate)
        refs = list(winner.evidence_refs)
        ref_ids = {ref.artifact_id for ref in refs}
        for ref in loser.evidence_refs:
            if ref.artifact_id not in ref_ids:
                refs.append(ref)
                ref_ids.add(ref.artifact_id)
        result[match_index] = winner.model_copy(
            update={
                "evidence_refs": refs,
                "confidence": max(existing.confidence, candidate.confidence),
                "evidence_observation": "；".join(
                    dict.fromkeys(
                        item
                        for item in (
                            existing.evidence_observation,
                            candidate.evidence_observation,
                        )
                        if item
                    )
                ),
            }
        )
        collapsed += 1
    return result, collapsed


def _candidate_tokens(candidate: CandidateIssue) -> set[str]:
    text = f"{candidate.claim} {candidate.type}".lower()
    return {
        token
        for token in re.findall(r"[a-z0-9_#]+|[\u4e00-\u9fff]{2,}", text)
        if token
        not in {
            "candidate",
            "issue",
            "problem",
            "可能",
            "需要",
            "存在",
            "变化",
            "行为",
            "逻辑",
        }
    }


def _same_controlled_mechanism(
    left: CandidateIssue,
    right: CandidateIssue,
) -> bool:
    if left.task_id != right.task_id:
        return False
    if left.file.replace("\\", "/").lower() != right.file.replace("\\", "/").lower():
        return False
    if left.line and right.line and abs(left.line - right.line) > 3:
        return False
    left_tokens = _candidate_tokens(left)
    right_tokens = _candidate_tokens(right)
    shared = left_tokens & right_tokens
    if len(shared) < 2:
        return False
    union = left_tokens | right_tokens
    similarity = len(shared) / len(union) if union else 0.0
    if left.type == right.type or similarity >= 0.35:
        return True
    # 同一行上类型标签不同的候选，可按主张词汇重合度识别为重复。
    # 合并时保留优先级较高的来源；位置与词汇约束用于区分独立问题。
    overlap_coefficient = len(shared) / min(len(left_tokens), len(right_tokens))
    return left.line == right.line and len(shared) >= 3 and overlap_coefficient >= 0.30


def _prefer_candidate(
    left: CandidateIssue,
    right: CandidateIssue,
) -> tuple[CandidateIssue, CandidateIssue]:
    rank = {"behavior": 0, "threat_model": 1, "maintainability": 2}
    if rank.get(right.source_agent, 9) < rank.get(left.source_agent, 9):
        return right, left
    return left, right
