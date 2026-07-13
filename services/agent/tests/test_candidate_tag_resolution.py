"""候选主张到证据主题的解析测试。"""

from __future__ import annotations

import logging
import re
import unicodedata
from types import SimpleNamespace

import pytest

from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence_rules import resolve_candidate_evidence_tag
from codeguard_agent.pipeline.evidence_rules.terms import CANDIDATE_TAG_TERMS


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    value = value.replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", " ", value).strip()


def test_candidate_terms_cover_every_specific_tag_with_normalized_terms():
    specific_tags = set(RiskTag) - {RiskTag.GENERAL_REVIEW}

    assert set(CANDIDATE_TAG_TERMS) == specific_tags
    for terms in CANDIDATE_TAG_TERMS.values():
        assert terms.exact_type_aliases
        assert terms.strong_phrases
        assert terms.weak_terms
        all_terms = (
            terms.exact_type_aliases | terms.strong_phrases | terms.weak_terms
        )
        assert all(term == _normalized(term) for term in all_terms)


def _dossier(
    *,
    candidate_type: str = "",
    claim: str = "",
    suggestion: str = "",
    task_tags: tuple[RiskTag, ...] = (),
) -> SimpleNamespace:
    candidate = CandidateIssue(
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
    return SimpleNamespace(
        candidate=candidate,
        task=SimpleNamespace(
            id="task-1",
            file="src/OrderService.java",
            patch="+ return order.getOwner().getName();",
        ),
        risk_profile=SimpleNamespace(tag_scores={tag: 10 for tag in task_tags}),
    )


class _ForbiddenLlm:
    def with_structured_output(self, *_args, **_kwargs):
        raise AssertionError("规则明确时绝不能调用 LLM")


class _StructuredInvoker:
    def __init__(self, owner: "_ClassifierLlm", schema):
        self.owner = owner
        self.schema = schema

    def invoke(self, messages):
        self.owner.messages = messages
        if isinstance(self.owner.response, Exception):
            raise self.owner.response
        if isinstance(self.owner.response, dict):
            return self.schema(**self.owner.response)
        return self.owner.response


class _ClassifierLlm:
    def __init__(self, response):
        self.response = response
        self.method = None
        self.messages = None

    def with_structured_output(self, schema, method=None):
        self.method = method
        return _StructuredInvoker(self, schema)


class _BadRequestError(RuntimeError):
    status_code = 400


class _StructuredOutputFailureLlm:
    def with_structured_output(self, *_args, **_kwargs):
        raise _BadRequestError("structured output unavailable")


def test_exact_type_match_returns_high_confidence_rule_without_llm():
    result = resolve_candidate_evidence_tag(
        _dossier(candidate_type="ＮＵＬＬ＿ＰＯＩＮＴＥＲ"),
        _ForbiddenLlm(),
        structured_method="json_schema",
    )

    assert result.tag == RiskTag.NULL_STATE_SAFETY
    assert result.source == "rule"
    assert result.confidence == 0.95


def test_type_containing_exact_alias_is_not_scored_as_exact_match():
    result = resolve_candidate_evidence_tag(
        _dossier(candidate_type="possible null pointer bug"),
        None,
        structured_method="function_calling",
    )

    assert result.tag == RiskTag.NULL_STATE_SAFETY
    assert result.source == "rule"
    assert result.confidence == 0.85


def test_unique_strong_claim_returns_rule_resolution():
    result = resolve_candidate_evidence_tag(
        _dossier(claim="缺少 resource ownership 校验"),
        None,
        structured_method="function_calling",
    )

    assert result.tag == RiskTag.AUTHORIZATION
    assert result.source == "rule"
    assert result.confidence == 0.85


@pytest.mark.parametrize("claim", ["NoSQL storage concern", "fallback path changed"])
def test_ascii_terms_inside_larger_words_do_not_bypass_general_fallback(claim: str):
    result = resolve_candidate_evidence_tag(
        _dossier(claim=claim),
        None,
        structured_method="function_calling",
    )

    assert result.tag == RiskTag.GENERAL_REVIEW
    assert result.source == "general"


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
    result = resolve_candidate_evidence_tag(
        _dossier(claim=claim),
        None,
        structured_method="function_calling",
    )

    assert result.tag == expected_tag
    assert result.source == "rule"


def test_weak_only_claim_falls_back_to_general_without_llm():
    result = resolve_candidate_evidence_tag(
        _dossier(claim="owner 未覆盖"),
        None,
        structured_method="function_calling",
    )

    assert result.tag == RiskTag.GENERAL_REVIEW
    assert result.source == "general"
    assert result.confidence == 0.5


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
    result = resolve_candidate_evidence_tag(
        _dossier(
            candidate_type=candidate_type,
            claim=claim,
            suggestion=suggestion,
        ),
        None,
        structured_method="function_calling",
    )

    assert result.tag == RiskTag.GENERAL_REVIEW
    assert result.source == "general"


def test_two_point_margin_with_top_at_least_four_is_not_ambiguous():
    result = resolve_candidate_evidence_tag(
        _dossier(
            candidate_type="possible 命令注入 bug",
            claim="输入校验缺失",
        ),
        None,
        structured_method="function_calling",
    )

    assert result.tag == RiskTag.INJECTION
    assert result.source == "rule"
    assert result.confidence == 0.85


def test_repeated_strong_suggestion_contributes_at_most_one_point():
    result = resolve_candidate_evidence_tag(
        _dossier(suggestion="参数校验 参数校验 validation validation"),
        None,
        structured_method="function_calling",
    )

    assert result.tag == RiskTag.GENERAL_REVIEW


def test_task_risk_tags_do_not_contribute_to_rule_score():
    result = resolve_candidate_evidence_tag(
        _dossier(
            candidate_type="空指针",
            task_tags=(RiskTag.TRANSACTION_ATOMICITY,),
        ),
        _ForbiddenLlm(),
        structured_method="function_calling",
    )

    assert result.tag == RiskTag.NULL_STATE_SAFETY
    assert result.source == "rule"


def test_ambiguous_candidate_uses_constrained_llm_and_renders_context():
    llm = _ClassifierLlm(
        {
            "tag": "API_CONTRACT",
            "confidence": 0.88,
            "reason": "返回字段发生破坏性变化",
        }
    )

    result = resolve_candidate_evidence_tag(
        _dossier(task_tags=(RiskTag.TRANSACTION_ATOMICITY,)),
        llm,
        structured_method="json_schema",
    )

    assert result.tag == RiskTag.API_CONTRACT
    assert result.source == "llm"
    assert result.confidence == 0.88
    assert llm.method == "json_schema"
    assert llm.messages is not None
    rendered = "\n".join(message for _role, message in llm.messages)
    assert "+ return order.getOwner().getName();" in rendered
    assert "TRANSACTION_ATOMICITY" in rendered
    assert "先验" in rendered
    assert "GENERAL_REVIEW" in rendered
    assert all(tag.value in rendered for tag in RiskTag)


@pytest.mark.parametrize(
    "response",
    [
        None,
        _BadRequestError("classifier unavailable"),
        {"tag": "AUTHORIZATION", "confidence": 0.74, "reason": "low"},
        {"tag": "UNKNOWN_TOPIC", "confidence": 0.99, "reason": "unknown"},
        {"tag": "AUTHORIZATION", "confidence": 0.99},
    ],
    ids=[
        "none",
        "exception",
        "low_confidence",
        "unknown_enum",
        "missing_reason",
    ],
)
def test_invalid_llm_result_falls_back_to_general(response):
    result = resolve_candidate_evidence_tag(
        _dossier(),
        _ClassifierLlm(response),
        structured_method="function_calling",
    )

    assert result.tag == RiskTag.GENERAL_REVIEW
    assert result.source == "general"
    assert result.confidence == 0.5


@pytest.mark.parametrize(
    "llm",
    [
        _StructuredOutputFailureLlm(),
        _ClassifierLlm(_BadRequestError("classifier unavailable")),
    ],
    ids=["with_structured_output", "invoke"],
)
def test_supplier_exception_falls_back_and_logs_warning(caplog, llm):
    with caplog.at_level(logging.WARNING, logger="codeguard"):
        result = resolve_candidate_evidence_tag(
            _dossier(),
            llm,
            structured_method="function_calling",
        )

    assert result.tag == RiskTag.GENERAL_REVIEW
    records = [
        record
        for record in caplog.records
        if "候选证据主题 LLM 分类失败" in record.getMessage()
    ]
    assert len(records) == 1
    assert records[0].exc_info is not None


@pytest.mark.parametrize("reason", ["", "   \t"])
def test_empty_llm_reason_is_invalid_and_has_explicit_fallback_reason(reason: str):
    result = resolve_candidate_evidence_tag(
        _dossier(),
        _ClassifierLlm(
            {
                "tag": "AUTHORIZATION",
                "confidence": 0.9,
                "reason": reason,
            }
        ),
        structured_method="function_calling",
    )

    assert result.tag == RiskTag.GENERAL_REVIEW
    assert result.source == "general"
    assert result.reason == "LLM 分类理由为空"
