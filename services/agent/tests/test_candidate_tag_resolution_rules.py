"""候选主张到证据主题的解析回归测试(ADR-046:纯规则,零 LLM)。"""

from __future__ import annotations

import re
import unicodedata

import pytest

from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence.rules import resolve_candidate_tag
from codeguard_agent.pipeline.evidence.rules.terms import CANDIDATE_TAG_TERMS


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    value = value.replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", " ", value).strip()


def _candidate(
    *,
    candidate_type: str = "",
    claim: str = "",
    suggestion: str = "",
) -> CandidateIssue:
    return CandidateIssue(
        id="candidate-1",
        task_id="task-1",
        source_agent="behavior",
        file="src/OrderService.java",
        line=24,
        type=candidate_type,
        severity_proposal=Severity.WARNING,
        claim=claim,
        suggestion=suggestion,
    )


def test_candidate_terms_cover_every_specific_tag_with_normalized_terms():
    specific_tags = set(RiskTag) - {RiskTag.GENERAL_REVIEW}
    assert set(CANDIDATE_TAG_TERMS) == specific_tags
    for terms in CANDIDATE_TAG_TERMS.values():
        assert terms.exact_type_aliases
        assert terms.strong_phrases
        assert terms.weak_terms
        all_terms = terms.exact_type_aliases | terms.strong_phrases | terms.weak_terms
        assert all(term == _normalized(term) for term in all_terms)


def test_exact_type_match_resolves_to_rule_tag():
    assert (
        resolve_candidate_tag(_candidate(candidate_type="ＮＵＬＬ＿ＰＯＩＮＴＥＲ"))
        is RiskTag.NULL_STATE_SAFETY
    )


def test_type_containing_exact_alias_is_not_scored_as_exact_match():
    assert (
        resolve_candidate_tag(_candidate(candidate_type="possible null pointer bug"))
        is RiskTag.NULL_STATE_SAFETY
    )


def test_unique_strong_claim_returns_rule_resolution():
    assert (
        resolve_candidate_tag(_candidate(claim="缺少 resource ownership 校验"))
        is RiskTag.AUTHORIZATION
    )


@pytest.mark.parametrize("claim", ["NoSQL storage concern", "fallback path changed"])
def test_ascii_terms_inside_larger_words_do_not_bypass_general_fallback(claim: str):
    assert resolve_candidate_tag(_candidate(claim=claim)) is RiskTag.GENERAL_REVIEW


@pytest.mark.parametrize(
    ("claim", "expected_tag"),
    [
        ("SQL predicate is incomplete", RiskTag.SQL_DATA_ACCESS),
        ("message ack is missing", RiskTag.MESSAGE_DELIVERY),
    ],
)
def test_independent_ascii_terms_still_resolve_by_rule(
    claim: str,
    expected_tag: RiskTag,
):
    assert resolve_candidate_tag(_candidate(claim=claim)) is expected_tag


def test_weak_only_claim_falls_back_to_general():
    assert (
        resolve_candidate_tag(_candidate(claim="owner 未覆盖"))
        is RiskTag.GENERAL_REVIEW
    )


@pytest.mark.parametrize(
    ("candidate_type", "claim", "suggestion"),
    [
        ("", "", ""),
        ("", "缓存性能退化", ""),
        ("possible 命令注入 bug", "输入校验缺失", "补充参数校验"),
    ],
    ids=["empty", "tied", "one_point_margin"],
)
def test_ambiguous_rule_scores_fall_back_to_general(
    candidate_type: str,
    claim: str,
    suggestion: str,
):
    assert (
        resolve_candidate_tag(
            _candidate(
                candidate_type=candidate_type,
                claim=claim,
                suggestion=suggestion,
            )
        )
        is RiskTag.GENERAL_REVIEW
    )


def test_two_point_margin_with_top_at_least_four_is_not_ambiguous():
    assert (
        resolve_candidate_tag(
            _candidate(
                candidate_type="possible 命令注入 bug",
                claim="输入校验缺失",
            )
        )
        is RiskTag.INJECTION
    )


def test_repeated_strong_suggestion_contributes_at_most_one_point():
    assert (
        resolve_candidate_tag(
            _candidate(suggestion="参数校验 参数校验 validation validation")
        )
        is RiskTag.GENERAL_REVIEW
    )


def test_candidate_type_drives_rule_score():
    assert (
        resolve_candidate_tag(_candidate(candidate_type="空指针"))
        is RiskTag.NULL_STATE_SAFETY
    )
