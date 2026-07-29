"""Phase 5 风险证据策略注册表的完整性测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence.rules import (
    STRATEGIES_BY_ID,
    STRATEGIES_BY_TAG,
    EvidenceStrategy,
    ToolCallSpec,
    strategies_for,
)
from codeguard_agent.pipeline.evidence.rules import _build_registry
from codeguard_agent.pipeline.evidence.rules import recipes
from codeguard_agent.pipeline.evidence.rules.recipes import callers_upstream
from codeguard_agent.pipeline.evidence.rules.types import (
    CAPABILITY_TO_TOOL,
    EvidenceCapability,
)


VALID_CONTEXT_KINDS = {
    "ast_structure",
    "sensitive_api",
    "find_callers",
    "get_code_metrics",
    "task_patch",
    "symbol_context",
}
VALID_TOOLS = {
    "get_file_content",
    "find_sensitive_apis",
    "find_callers",
    "get_code_metrics",
    "inspect_security_path",
    "inspect_change_impact",
    "inspect_structure",
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
UPSTREAM_QUESTIONS = {
    RiskTag.AUTHORIZATION: (
        "上游调用方是否已完成鉴权或资源归属校验，使当前方法无需重复校验"
    ),
    RiskTag.AUTHENTICATION_SESSION: (
        "上游是否验证 token/session 有效期、撤销和主体绑定"
    ),
    RiskTag.SQL_DATA_ACCESS: (
        "调用方是否已强制租户/查询边界或使用安全参数，使当前数据访问条件受控"
    ),
    RiskTag.TRANSACTION_ATOMICITY: (
        "外层调用方是否建立事务边界或可靠补偿，覆盖当前副作用"
    ),
    RiskTag.CONCURRENCY_CONSISTENCY: (
        "调用方是否在锁、原子边界或线程封闭范围内调用当前方法"
    ),
    RiskTag.IDEMPOTENCY_RETRY: (
        "上游是否提供幂等键、去重或唯一约束覆盖重复触发"
    ),
    RiskTag.CACHE_CONSISTENCY: (
        "上游是否协调持久化与缓存更新/失效，覆盖当前路径"
    ),
    RiskTag.MESSAGE_DELIVERY: (
        "上游发布/消费链是否提供 ack、retry、DLQ、outbox 或去重保证"
    ),
    RiskTag.ERROR_HANDLING: (
        "上游是否捕获并正确传播、转换或恢复当前错误"
    ),
    RiskTag.RESOURCE_LIFECYCLE: (
        "上游生命周期是否明确托管并释放当前资源"
    ),
    RiskTag.API_CONTRACT: (
        "调用方是否已同步适配新的请求/响应/签名契约"
    ),
    RiskTag.PERFORMANCE: (
        "上游是否限制输入规模/调用频率或批量化调用以控制成本"
    ),
    RiskTag.OBSERVABILITY_TESTABILITY: (
        "上游入口是否提供覆盖当前调用的日志、指标、trace 或可替换 seam"
    ),
}


def _dossier(*facts: SimpleNamespace, file_path: str = "src/OrderService.java") -> SimpleNamespace:
    task = SimpleNamespace(
        file=file_path,
        hunk_header="@@ -12,2 +12,2 @@",
        changed_lines=[12],
    )
    symbol_fact = SimpleNamespace(
        kind="symbol_context",
        content=json.dumps(
            {
                "file": file_path,
                "symbol_id": "java:OrderService#save(Order)",
                "start_line": 10,
                "end_line": 20,
            }
        ),
        truncated=False,
    )
    return SimpleNamespace(
        task=task,
        context_bundle=SimpleNamespace(facts=[symbol_fact, *facts]),
    )


def _strategy(strategy_id: str) -> EvidenceStrategy:
    return EvidenceStrategy(
        id=strategy_id,
        tags=frozenset({RiskTag.GENERAL_REVIEW}),
        purpose="counter",
        priority=10,
        question_template="question",
        context_kinds=("task_patch",),
        allowed_capabilities=(EvidenceCapability.CURRENT_IMPLEMENTATION,),
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
        assert upstream.allowed_tools == ("inspect_change_impact",)


def test_registry_builder_rejects_duplicate_raw_ids():
    duplicate = _strategy("duplicate")

    with pytest.raises(ValueError, match="duplicate"):
        _build_registry([duplicate, duplicate])


def test_every_strategy_has_valid_declarations_and_recipe_tools():
    dossier = _dossier()

    for strategy in STRATEGIES_BY_ID.values():
        # claim.* 策略的 question_template 在注册表中为空，运行时由 goal.proposition 注入
        if not strategy.id.startswith("claim."):
            assert strategy.question_template.strip()
            assert len(strategy.tags) == 1
        else:
            # claim 策略不绑定 RiskTag，tags 为空 frozenset
            assert len(strategy.tags) == 0
        assert set(strategy.context_kinds) <= VALID_CONTEXT_KINDS
        assert set(strategy.allowed_tools) <= VALID_TOOLS
        calls = strategy.build_tool_calls(dossier)
        assert {call.tool_name for call in calls} <= {
            CAPABILITY_TO_TOOL[capability]
            for capability in strategy.allowed_capabilities
        }


def test_non_upstream_strategy_allowlist_exactly_matches_recipe_tools():
    dossier = _dossier()

    for strategy in STRATEGIES_BY_ID.values():
        if strategy.id.endswith(".counter_upstream"):
            continue
        # claim.* 策略的工具可用性依赖运行时上下文（如 symbol_id），
        # 静态 dossier 无法保证全部 capability 都有对应的工具调用
        if strategy.id.startswith("claim."):
            continue
        calls = strategy.build_tool_calls(dossier)
        actual_tools = tuple(dict.fromkeys(call.tool_name for call in calls))
        assert tuple(
            CAPABILITY_TO_TOOL[capability]
            for capability in strategy.allowed_capabilities
        ) == actual_tools, strategy.id


def test_upstream_strategy_allowlist_exactly_matches_callers_recipe():
    ast_fact = SimpleNamespace(
        kind="ast_structure",
        content=(
            "AST for: src/OrderService.java\n"
            "  class: OrderService\n"
            "    public void save(Order order) [L10-L20]\n"
        ),
        truncated=False,
    )
    dossier = _dossier(ast_fact)

    for tag in UPSTREAM_TAGS:
        strategy = STRATEGIES_BY_ID[f"{tag.value.lower()}.counter_upstream"]
        calls = strategy.build_tool_calls(dossier)
        actual_tools = tuple(dict.fromkeys(call.tool_name for call in calls))
        assert actual_tools == ("inspect_change_impact",)


def test_upstream_strategies_use_explicit_tag_specific_questions():
    assert set(UPSTREAM_QUESTIONS) == UPSTREAM_TAGS
    for tag, expected_question in UPSTREAM_QUESTIONS.items():
        strategy = STRATEGIES_BY_ID[f"{tag.value.lower()}.counter_upstream"]
        assert strategy.question_template == expected_question
        assert strategy.question_template.strip()
        assert not strategy.question_template.startswith("外层调用方是否提供以下保护：")


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
            "CURRENT_IMPLEMENTATION",
            (("file_path", "src/OrderService.java"),),
        ),
        ToolCallSpec(
            "SECURITY_PATH",
            (("symbol_id", "java:OrderService#save(Order)"),),
        ),
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
            "CURRENT_IMPLEMENTATION",
            (("file_path", "src/OrderService.java"),),
        ),
            ToolCallSpec(
                "STRUCTURAL_METRICS",
                (("symbol_id", "java:OrderService#save(Order)"),),
            ),
    ]


def test_callers_upstream_uses_prefetched_symbol_context():
    assert callers_upstream(_dossier()) == [
        ToolCallSpec(
            "UPSTREAM_REACHABILITY",
            (("symbol_id", "java:OrderService#save(Order)"),),
        )
    ]


def test_callers_upstream_returns_empty_without_context_bundle():
    dossier = _dossier()
    dossier.context_bundle = None

    assert callers_upstream(dossier) == []


def test_callers_upstream_ignores_unrelated_legacy_ast_and_uses_symbol_id():
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
            "UPSTREAM_REACHABILITY",
            (("symbol_id", "java:OrderService#save(Order)"),),
        )
    ]


def test_recipes_do_not_expose_unused_caller_combinations():
    assert not hasattr(recipes, "file_callers")
    assert not hasattr(recipes, "file_metrics_callers")


def test_file_metrics_skips_get_code_metrics_for_non_java_file():
    """非 .java 文件只调 get_file_content，不调 get_code_metrics。"""
    dossier_non_java = _dossier(file_path="pom.xml")
    calls = recipes.file_metrics(dossier_non_java)
    tool_names = {c.tool_name for c in calls}
    assert tool_names == {"get_file_content"}


def test_file_metrics_includes_get_code_metrics_for_java_file():
    """.java 文件同时调 get_file_content 和 get_code_metrics。"""
    dossier_java = _dossier(file_path="src/main/java/com/example/UserService.java")
    calls = recipes.file_metrics(dossier_java)
    tool_names = {c.tool_name for c in calls}
    assert tool_names == {"get_file_content", "inspect_structure"}
