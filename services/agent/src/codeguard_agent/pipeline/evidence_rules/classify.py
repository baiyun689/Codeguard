"""把候选问题解析为证据主题标签。"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence_rules.terms import (
    CANDIDATE_TAG_TERMS,
    normalize_candidate_text,
)


logger = logging.getLogger("codeguard")


class CandidateTagResolution(BaseModel):
    tag: RiskTag
    confidence: float = Field(ge=0.0, le=1.0)
    source: Literal["rule", "llm", "general"]
    reason: str


class _LlmTagResolution(BaseModel):
    tag: str
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"


def _load_prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


def _contains_any(text: str, terms: frozenset[str]) -> bool:
    return any(_term_matches(text, term) for term in terms)


def _term_matches(text: str, term: str) -> bool:
    if re.search(r"[a-z0-9]", term):
        pattern = rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return term in text


def _score_candidate(dossier: Any) -> tuple[RiskTag | None, bool, str]:
    candidate = dossier.candidate
    candidate_type = normalize_candidate_text(candidate.type)
    claim = normalize_candidate_text(candidate.claim)
    suggestion = normalize_candidate_text(candidate.suggestion)
    scores: dict[RiskTag, int] = {}
    exact_hits: set[RiskTag] = set()

    for tag, terms in CANDIDATE_TAG_TERMS.items():
        score = 0
        if candidate_type and candidate_type in terms.exact_type_aliases:
            score += 8
            exact_hits.add(tag)
        elif _contains_any(candidate_type, terms.strong_phrases):
            score += 6

        if _contains_any(claim, terms.strong_phrases):
            score += 4
        elif _contains_any(claim, terms.weak_terms):
            score += 1

        if _contains_any(suggestion, terms.strong_phrases):
            score += 1
        scores[tag] = score

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    top_tag, top_score = ranked[0]
    second_score = ranked[1][1]
    top_count = sum(score == top_score for score in scores.values())
    ambiguous = top_score < 4 or top_count != 1 or top_score - second_score < 2
    if ambiguous:
        reason = (
            "规则得分存在歧义: "
            f"top={top_score}, second={second_score}, tied={top_count != 1}"
        )
        return None, False, reason
    return top_tag, top_tag in exact_hits, f"候选语义规则命中 {top_tag.value}"


def resolve_candidate_evidence_tag(
    dossier: Any,
    classifier_llm: Any,
    *,
    structured_method: str,
) -> CandidateTagResolution:
    """先规则解析；歧义时才用受限结构化 LLM 分类。"""
    tag, exact_hit, reason = _score_candidate(dossier)
    if tag is not None:
        return CandidateTagResolution(
            tag=tag,
            confidence=0.95 if exact_hit else 0.85,
            source="rule",
            reason=reason,
        )
    if classifier_llm is None:
        return _general_resolution(reason)

    candidate = dossier.candidate
    tag_scores = getattr(getattr(dossier, "risk_profile", None), "tag_scores", {})
    task_tags = sorted(
        item.value if isinstance(item, RiskTag) else str(item) for item in tag_scores
    )
    user_prompt = _load_prompt("evidence-tag-classifier-user.txt").format(
        candidate_type=candidate.type,
        candidate_claim=candidate.claim,
        candidate_suggestion=candidate.suggestion,
        task_patch=dossier.task.patch,
        task_tags=", ".join(task_tags) or "(无)",
        allowed_tags=", ".join(item.value for item in RiskTag),
    )
    messages = [
        ("system", _load_prompt("evidence-tag-classifier-system.txt")),
        ("human", user_prompt),
    ]

    from codeguard_agent.llm.client import invoke_with_retry

    try:
        structured_llm = classifier_llm.with_structured_output(
            _LlmTagResolution,
            method=structured_method,
        )
        result = invoke_with_retry(structured_llm, messages, max_retries=1)
    except Exception:  # noqa: BLE001 分类失败必须安全回退通用主题
        logger.warning(
            "候选证据主题 LLM 分类失败,回退 GENERAL_REVIEW",
            exc_info=True,
        )
        return _general_resolution("LLM 分类调用失败")

    if result is None or not isinstance(result, _LlmTagResolution):
        return _general_resolution("LLM 未返回有效结构化分类")
    if not result.reason.strip():
        return _general_resolution("LLM 分类理由为空")
    try:
        resolved_tag = RiskTag(result.tag)
    except ValueError:
        return _general_resolution("LLM 返回未知证据主题")
    if result.confidence < 0.75:
        return _general_resolution("LLM 分类置信度不足")
    return CandidateTagResolution(
        tag=resolved_tag,
        confidence=result.confidence,
        source="llm",
        reason=result.reason,
    )


def _general_resolution(reason: str) -> CandidateTagResolution:
    return CandidateTagResolution(
        tag=RiskTag.GENERAL_REVIEW,
        confidence=0.5,
        source="general",
        reason=reason,
    )


def resolve_candidate_tags(
    dossiers: Sequence[Any],
    *,
    classifier_llm: Any,
    structured_method: str,
    max_workers: int = 8,
) -> dict[str, CandidateTagResolution]:
    """批量解析候选证据主题标签，返回按输入顺序的 candidate_id → 标签映射。

    每个 dossier 独立调用 resolve_candidate_evidence_tag，
    失败/None 回退 GENERAL_REVIEW。
    """
    from codeguard_agent.pipeline.concurrency import run_bounded_parallel

    ordered = list(dossiers)
    outcomes = run_bounded_parallel(
        ordered,
        lambda dossier: resolve_candidate_evidence_tag(
            dossier,
            classifier_llm,
            structured_method=structured_method,
        ),
        max_workers=max_workers,
    )
    resolved: dict[str, CandidateTagResolution] = {}
    for dossier, outcome in zip(ordered, outcomes, strict=True):
        resolved[dossier.candidate.id] = (
            outcome
            if isinstance(outcome, CandidateTagResolution)
            else _general_resolution("候选证据主题并发解析失败")
        )
    return resolved


__all__ = [
    "CandidateTagResolution",
    "resolve_candidate_evidence_tag",
    "resolve_candidate_tags",
]
