"""当前任务上下文事实筛选与预算的单测。"""

from __future__ import annotations

from codeguard_agent.models.council import ContextFact
from codeguard_agent.models.tasks import ReviewTask
from codeguard_agent.pipeline.context.rules import (
    sensitive_api_rows_for_task,
    truncate_task_facts,
)


def test_sensitive_api_rows_for_task_filters_by_file_and_hunk_range():
    sensitive_text = (
        "# 敏感 API 扫描\n"
        "扫描 1 个文件, 跳过 0 个不可解析文件, 发现 2 处敏感 API 调用\n\n"
        "| 危险等级 | API | 文件 | 行号 | 调用参数 |\n"
        "|---------|-----|------|------|----------|\n"
        "| 🔴 HIGH | `Statement.execute` | A.java:12 | `sql` |\n"
        "| 🟡 MEDIUM | `Files.copy` | A.java:99 | `p1, p2` |\n"
    )
    task = ReviewTask(
        id="A.java#h0",
        file="A.java",
        hunk_header="@@ -10,5 +10,5 @@",
        patch="+x",
        changed_lines=[12],
    )

    rows = sensitive_api_rows_for_task(sensitive_text, task)

    assert len(rows) == 1
    assert "Statement.execute" in rows[0]
    assert "Files.copy" not in "\n".join(rows)


def test_sensitive_api_rows_for_task_accepts_whole_file_for_fallback_task():
    sensitive_text = (
        "| 危险等级 | API | 文件 | 行号 | 调用参数 |\n"
        "|---------|-----|------|------|----------|\n"
        "| 🔴 HIGH | `Statement.execute` | A.java:500 | `sql` |\n"
    )
    task = ReviewTask(id="A.java#file", file="A.java", patch="+x", changed_lines=[])

    rows = sensitive_api_rows_for_task(sensitive_text, task)

    assert len(rows) == 1


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
