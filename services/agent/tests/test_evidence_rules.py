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


def _dossier(*facts: SimpleNamespace, file_path: str = "src/OrderService.java"):
    symbol_fact = SimpleNamespace(
        kind="symbol_context",
        content=json.dumps({
            "file": file_path,
            "symbol_id": "java:OrderService#save(Order)",
            "start_line": 10,
            "end_line": 20,
        }),
        truncated=False,
    )
    return SimpleNamespace(
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
