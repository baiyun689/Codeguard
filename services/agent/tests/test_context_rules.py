"""当前任务上下文事实筛选与预算的单测。"""

from __future__ import annotations

from codeguard_agent.models.council import ContextFact
from codeguard_agent.pipeline.context.rules import (
    truncate_task_facts,
)


def test_truncate_task_facts_keeps_all_when_within_budget():
    facts = [ContextFact(source="s1", kind="k", content="short")]

    kept, truncated = truncate_task_facts(facts, max_chars=100)

    assert kept == facts
    assert truncated is False


def test_truncate_task_facts_marks_clipped_fact_and_preserves_metadata():
    facts = [
        ContextFact(source="s1", kind="first", content="a" * 60),
        ContextFact(source="s2", kind="second", content="b" * 60),
    ]

    kept, truncated = truncate_task_facts(facts, max_chars=100)

    assert truncated is True
    assert sum(len(fact.content) for fact in kept) <= 100 + len("...(已截断)")
    assert kept[0] == facts[0]
    assert kept[1].source == "s2"
    assert kept[1].kind == "second"
    assert kept[1].content == "b" * 40 + "...(已截断)"
    assert kept[1].truncated is True


def test_truncate_task_facts_none_budget_means_unbounded():
    facts = [ContextFact(source="s1", kind="k", content="a" * 100_000)]

    kept, truncated = truncate_task_facts(facts, max_chars=None)

    assert kept == facts
    assert truncated is False
