"""候选问题到风险标签的确定性规则解析(ADR-046:纯规则,零 LLM)。"""

from __future__ import annotations

import re

from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence.rules.terms import (
    CANDIDATE_TAG_TERMS,
    normalize_candidate_text,
)


def _contains_any(text: str, terms: frozenset[str]) -> bool:
    return any(_term_matches(text, term) for term in terms)


def _term_matches(text: str, term: str) -> bool:
    if re.search(r"[a-z0-9]", term):
        pattern = rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return term in text


def _score_candidate(candidate: CandidateIssue) -> RiskTag | None:
    """规则评分(与旧 dossier 版逐行一致,输入改为 CandidateIssue)。

    标签只驱动配方开关与候选分块,不再需要 LLM 高置信分类(ADR-046)。
    """
    candidate_type = normalize_candidate_text(candidate.type)
    claim = normalize_candidate_text(candidate.claim)
    suggestion = normalize_candidate_text(candidate.suggestion)
    scores: dict[RiskTag, int] = {}

    for tag, terms in CANDIDATE_TAG_TERMS.items():
        score = 0
        if candidate_type and candidate_type in terms.exact_type_aliases:
            score += 8
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
        return None
    return top_tag


def resolve_candidate_tag(candidate: CandidateIssue) -> RiskTag:
    """规则解析候选语义标签;歧义回落 GENERAL_REVIEW。

    标签只驱动配方开关与候选分块,不再需要 LLM 高置信分类(ADR-046)。
    """
    tag = _score_candidate(candidate)
    return tag if tag is not None else RiskTag.GENERAL_REVIEW


__all__ = ["resolve_candidate_tag"]
