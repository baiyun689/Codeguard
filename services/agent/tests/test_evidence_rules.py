"""Claim-driven evidence strategy registry contracts."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from codeguard_agent.models.council import EvidenceFactType, EvidencePolarity
from codeguard_agent.pipeline.evidence.rules import (
    STRATEGIES_BY_ID,
    EvidenceStrategy,
    ToolCallSpec,
    _build_registry,
)
from codeguard_agent.pipeline.evidence.rules import recipes
from codeguard_agent.pipeline.evidence.rules.recipes import callers_upstream
from codeguard_agent.pipeline.evidence.strategy_types import (
    CAPABILITY_TO_TOOL,
    EvidenceCapability,
)


def _symbol_fact(
    symbol_id: str = "java:OrderService#save(Order)",
    start_line: int = 10,
    end_line: int = 20,
    truncated: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        kind="symbol_context",
        content=json.dumps({
            "file": "src/OrderService.java",
            "symbol_id": symbol_id,
            "start_line": start_line,
            "end_line": end_line,
        }),
        truncated=truncated,
    )


def _dossier(*facts: SimpleNamespace, file_path: str = "src/OrderService.java", line: int = 0):
    symbol_fact = _symbol_fact()
    return SimpleNamespace(
        candidate=SimpleNamespace(line=line),
        task=SimpleNamespace(
            file=file_path,
            hunk_header="@@ -12,2 +12,2 @@",
            changed_lines=[12],
        ),
        context_bundle=SimpleNamespace(facts=[symbol_fact, *facts]),
    )


def _strategy(strategy_id: str) -> EvidenceStrategy:
    return EvidenceStrategy(
        id=strategy_id,
        purpose="counter",
        priority=0,
        question_template="",
        context_kinds=("task_patch",),
        allowed_capabilities=(EvidenceCapability.CURRENT_IMPLEMENTATION,),
        build_tool_calls=lambda dossier: [],
    )


def test_registry_contains_only_claim_strategies():
    assert STRATEGIES_BY_ID
    assert all(strategy_id.startswith("claim.") for strategy_id in STRATEGIES_BY_ID)


def test_registry_covers_every_fact_type_and_polarity():
    expected = {
        f"claim.{fact_type.value}.{polarity.value}"
        for fact_type in EvidenceFactType
        for polarity in EvidencePolarity
    }
    assert set(STRATEGIES_BY_ID) == expected


def test_registry_builder_rejects_duplicate_ids():
    duplicate = _strategy("claim.duplicate.counter")
    with pytest.raises(ValueError, match="duplicate"):
        _build_registry([duplicate, duplicate])


def test_claim_strategy_tools_stay_within_declared_capabilities():
    dossier = _dossier()
    for strategy in STRATEGIES_BY_ID.values():
        assert strategy.question_template == ""
        calls = strategy.build_tool_calls(dossier)
        assert {call.tool_name for call in calls} <= {
            CAPABILITY_TO_TOOL[capability]
            for capability in strategy.allowed_capabilities
        }


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


def test_symbol_id_matches_candidate_line_range():
    """候选行号命中第二个 symbol 区间时,应返回该 symbol 而非第一个。"""
    second = _symbol_fact(
        symbol_id="java:OrderService#archive(Order)",
        start_line=30,
        end_line=40,
    )
    dossier = _dossier(second, line=35)
    assert callers_upstream(dossier) == [
        ToolCallSpec(
            "UPSTREAM_REACHABILITY",
            (("symbol_id", "java:OrderService#archive(Order)"),),
        )
    ]


def test_symbol_id_falls_back_to_first_when_line_outside_any_range():
    """行号未命中任何 symbol 区间时回退第一个非空 symbol,保证工具调用不缺失。"""
    dossier = _dossier(line=100)
    assert callers_upstream(dossier) == [
        ToolCallSpec(
            "UPSTREAM_REACHABILITY",
            (("symbol_id", "java:OrderService#save(Order)"),),
        )
    ]


def test_symbol_id_falls_back_to_first_when_line_zero():
    """line=0(无法定位)时行号匹配失效,回退第一个非空 symbol。"""
    dossier = _dossier(line=0)
    assert callers_upstream(dossier) == [
        ToolCallSpec(
            "UPSTREAM_REACHABILITY",
            (("symbol_id", "java:OrderService#save(Order)"),),
        )
    ]


def test_symbol_id_skips_truncated_symbol_facts():
    """truncated 的 symbol 事实被跳过,即使其行号区间命中候选行。"""
    truncated = _symbol_fact(
        symbol_id="java:OrderService#truncated()",
        start_line=30,
        end_line=40,
        truncated=True,
    )
    dossier = _dossier(truncated, line=35)
    assert callers_upstream(dossier) == [
        ToolCallSpec(
            "UPSTREAM_REACHABILITY",
            (("symbol_id", "java:OrderService#save(Order)"),),
        )
    ]


def test_file_metrics_skips_structural_metrics_for_non_java_file():
    calls = recipes.file_metrics(_dossier(file_path="pom.xml"))
    assert {call.tool_name for call in calls} == {"get_file_content"}


def test_file_metrics_includes_structural_metrics_for_java_file():
    calls = recipes.file_metrics(
        _dossier(file_path="src/main/java/com/example/UserService.java")
    )
    assert {call.tool_name for call in calls} == {
        "get_file_content",
        "inspect_structure",
    }
