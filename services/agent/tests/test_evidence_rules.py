"""Phase 5 风险证据策略注册表的完整性测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence_rules import (
    STRATEGIES_BY_ID,
    STRATEGIES_BY_TAG,
    EvidenceStrategy,
    ToolCallSpec,
    strategies_for,
)
from codeguard_agent.pipeline.evidence_rules import _build_registry
from codeguard_agent.pipeline.evidence_rules.recipes import callers_upstream


VALID_CONTEXT_KINDS = {
    "ast_structure",
    "sensitive_api",
    "find_callers",
    "get_code_metrics",
    "task_patch",
}
VALID_TOOLS = {
    "get_file_content",
    "find_sensitive_apis",
    "find_callers",
    "get_code_metrics",
}
UPSTREAM_TAGS = {
    RiskTag.AUTHORIZATION,
    RiskTag.AUTHENTICATION_SESSION,
    RiskTag.SQL_DATA_ACCESS,
    RiskTag.TRANSACTION_ATOMICITY,
    RiskTag.CONCURRENCY_CONSISTENCY,
    RiskTag.IDEMPOTENCY_RETRY,
    RiskTag.CACHE_CONSISTENCY,
    RiskTag.MESSAGE_DELIVERY,
    RiskTag.ERROR_HANDLING,
    RiskTag.RESOURCE_LIFECYCLE,
    RiskTag.API_CONTRACT,
    RiskTag.PERFORMANCE,
    RiskTag.OBSERVABILITY_TESTABILITY,
}


def _dossier(*facts: SimpleNamespace) -> SimpleNamespace:
    task = SimpleNamespace(
        file="src/OrderService.java",
        hunk_header="@@ -12,2 +12,2 @@",
        changed_lines=[12],
    )
    return SimpleNamespace(
        task=task,
        context_bundle=SimpleNamespace(facts=list(facts)),
    )


def _strategy(strategy_id: str) -> EvidenceStrategy:
    return EvidenceStrategy(
        id=strategy_id,
        tags=frozenset({RiskTag.GENERAL_REVIEW}),
        purpose="counter",
        priority=10,
        question_template="question",
        context_kinds=("task_patch",),
        allowed_tools=("get_file_content",),
        build_tool_calls=lambda dossier: [],
    )


def test_registry_covers_every_risk_tag():
    assert set(STRATEGIES_BY_TAG) == set(RiskTag)


@pytest.mark.parametrize("tag", list(RiskTag))
def test_each_tag_has_counter_support_and_severity(tag: RiskTag):
    slug = tag.value.lower()
    ids = {strategy.id for strategy in STRATEGIES_BY_TAG[tag]}

    assert {f"{slug}.counter", f"{slug}.support", f"{slug}.severity"} <= ids


def test_only_outer_semantic_tags_have_counter_upstream():
    actual = {
        tag
        for tag, strategies in STRATEGIES_BY_TAG.items()
        if any(strategy.id.endswith(".counter_upstream") for strategy in strategies)
    }

    assert actual == UPSTREAM_TAGS
    for tag in UPSTREAM_TAGS:
        upstream = STRATEGIES_BY_ID[f"{tag.value.lower()}.counter_upstream"]
        assert upstream.purpose == "counter"
        assert upstream.priority == 30
        assert upstream.allowed_tools == ("find_callers",)


def test_registry_builder_rejects_duplicate_raw_ids():
    duplicate = _strategy("duplicate")

    with pytest.raises(ValueError, match="duplicate"):
        _build_registry([duplicate, duplicate])


def test_every_strategy_has_valid_declarations_and_recipe_tools():
    dossier = _dossier()

    for strategy in STRATEGIES_BY_ID.values():
        assert strategy.question_template.strip()
        assert set(strategy.context_kinds) <= VALID_CONTEXT_KINDS
        assert set(strategy.allowed_tools) <= VALID_TOOLS
        assert len(strategy.tags) == 1
        calls = strategy.build_tool_calls(dossier)
        assert {call.tool_name for call in calls} <= set(strategy.allowed_tools)


@pytest.mark.parametrize("tag", list(RiskTag))
def test_strategies_for_is_priority_ordered_and_filters_purpose(tag: RiskTag):
    strategies = strategies_for(tag)

    assert [item.priority for item in strategies] == sorted(
        item.priority for item in strategies
    )
    assert strategies_for(tag) == strategies
    for purpose in ("counter", "support", "severity"):
        filtered = strategies_for(tag, purpose)
        assert all(item.purpose == purpose for item in filtered)
        assert filtered == tuple(item for item in strategies if item.purpose == purpose)


def test_authorization_counter_uses_file_and_sensitive_api_recipe():
    calls = STRATEGIES_BY_ID["authorization.counter"].build_tool_calls(_dossier())

    assert calls == [
        ToolCallSpec(
            "get_file_content",
            (("file_path", "src/OrderService.java"),),
        ),
        ToolCallSpec("find_sensitive_apis", ()),
    ]


@pytest.mark.parametrize(
    "tag",
    [
        RiskTag.PERFORMANCE,
        RiskTag.COMPLEXITY_CONTROL_FLOW,
        RiskTag.DUPLICATION_DESIGN,
        RiskTag.OBSERVABILITY_TESTABILITY,
    ],
)
def test_maintainability_base_strategies_use_file_and_metrics_recipe(tag: RiskTag):
    strategy = STRATEGIES_BY_ID[f"{tag.value.lower()}.support"]

    assert strategy.build_tool_calls(_dossier()) == [
        ToolCallSpec(
            "get_file_content",
            (("file_path", "src/OrderService.java"),),
        ),
        ToolCallSpec(
            "get_code_metrics",
            (("file_path", "src/OrderService.java"),),
        ),
    ]


def test_callers_upstream_returns_empty_without_ast_fact():
    assert callers_upstream(_dossier()) == []


def test_callers_upstream_ignores_truncated_ast_fact():
    ast_fact = SimpleNamespace(
        kind="ast_structure",
        content="AST for: src/OrderService.java\n    save() [L10-L20]",
        truncated=True,
    )

    assert callers_upstream(_dossier(ast_fact)) == []


def test_callers_upstream_returns_empty_when_method_cannot_be_resolved():
    ast_fact = SimpleNamespace(
        kind="ast_structure",
        content="AST for: src/OrderService.java\n  class: OrderService",
        truncated=False,
    )

    assert callers_upstream(_dossier(ast_fact)) == []


def test_callers_upstream_uses_exact_file_and_resolved_method_query():
    ast_fact = SimpleNamespace(
        kind="ast_structure",
        content=(
            "AST for: src/OrderService.java\n"
            "  class: OrderService\n"
            "    public void save(Order order) [L10-L20]\n"
        ),
        truncated=False,
    )

    assert callers_upstream(_dossier(ast_fact)) == [
        ToolCallSpec(
            "find_callers",
            (("query", "src/OrderService.java#save"),),
        )
    ]
