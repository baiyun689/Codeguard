"""受控模式的 task 级知识路由。

ReviewPlan 在 controlled 模式不选择 reviewer、工具或 symbol，只从闭合主题注册表
中为每个 task 选择少量审查方法。统一 Reviewer 随后共享同一份结果。
"""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
from pathlib import Path
from typing import Any

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.tasks import (
    KnowledgeRoutePlan,
    PlanUnit,
    ReviewTask,
    TaskKnowledgeRoute,
)
from codeguard_agent.pipeline.knowledge.catalog import KnowledgeCatalog

_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts" / "controlled"


def prompt_hash() -> str:
    content = (_PROMPT_DIR / "knowledge-plan.txt").read_text(encoding="utf-8")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def knowledge_topic_registry(catalog: KnowledgeCatalog) -> tuple[str, ...]:
    """返回稳定、去重后的闭合主题 ID 集合。"""

    return tuple(fragment.topic for fragment in catalog.shared_specialized_fragments())


def run_knowledge_route(
    *,
    plan_unit: PlanUnit,
    tasks: Sequence[ReviewTask],
    llm: Any,
    catalog: KnowledgeCatalog,
    max_retries: int,
    structured_method: str,
    max_topics: int = 4,
) -> tuple[KnowledgeRoutePlan, tuple[str, ...]]:
    """执行一次 PlanUnit 级 ReviewPlan，并确定性清理不合规主题。"""

    task_by_id = {task.id: task for task in tasks if task.id in plan_unit.task_ids}
    allowed_topics = knowledge_topic_registry(catalog)
    empty = KnowledgeRoutePlan(
        plan_unit_id=plan_unit.id,
        task_routes=tuple(TaskKnowledgeRoute(task_id=task_id) for task_id in task_by_id),
    )
    if llm is None or not task_by_id:
        return empty, ("knowledge_plan_unavailable",) if llm is None else ()

    system = (_PROMPT_DIR / "knowledge-plan.txt").read_text(encoding="utf-8")
    task_blocks = []
    for task_id in plan_unit.task_ids:
        task = task_by_id.get(task_id)
        if task is None:
            continue
        task_blocks.append(
            f'<task id="{task.id}" file="{task.file}" changed_lines="{",".join(map(str, task.changed_lines))}">\n'
            f"{task.patch}\n</task>"
        )
    task_text = "\n".join(task_blocks)
    user = (
        f'<plan_unit id="{plan_unit.id}">\n{task_text}\n</plan_unit>\n'
        f"允许的 knowledge topic ID（只能从中选择，最多 {max_topics} 个/task）："
        f"{', '.join(allowed_topics) or '(无专项主题)'}\n"
        "只输出 KnowledgeRoutePlan；每个 task 都可返回空 knowledge_topics。"
    )
    diagnostics: list[str] = [f"knowledge_plan_prompt_hash:{prompt_hash()}"]
    try:
        result = invoke_with_retry(
            llm.with_structured_output(KnowledgeRoutePlan, method=structured_method),
            [("system", system), ("human", user)],
            max_retries=max_retries,
        )
    except Exception as exc:  # noqa: BLE001
        diagnostics.append(f"knowledge_plan_error:{type(exc).__name__}")
        return empty, tuple(diagnostics)
    if result is None:
        diagnostics.append("knowledge_plan_missing")
        return empty, tuple(diagnostics)
    try:
        parsed = KnowledgeRoutePlan.model_validate(result)
    except Exception:  # noqa: BLE001
        diagnostics.append("knowledge_plan_invalid")
        return empty, tuple(diagnostics)

    if parsed.plan_unit_id != plan_unit.id:
        diagnostics.append(f"plan_unit_mismatch:{parsed.plan_unit_id}")
        return empty, tuple(diagnostics)

    allowed = set(allowed_topics)
    normalized: list[TaskKnowledgeRoute] = []
    seen_tasks: set[str] = set()
    for route in parsed.task_routes:
        if route.task_id not in task_by_id:
            diagnostics.append(f"unknown_task:{route.task_id}")
            continue
        if route.task_id in seen_tasks:
            diagnostics.append(f"duplicate_task:{route.task_id}")
            continue
        seen_tasks.add(route.task_id)
        topics: list[str] = []
        seen_topics: set[str] = set()
        for topic in route.knowledge_topics:
            if topic in seen_topics:
                diagnostics.append(f"duplicate_topic:{topic}")
                continue
            seen_topics.add(topic)
            if topic not in allowed:
                diagnostics.append(f"rejected_topic:{topic}")
                continue
            if len(topics) >= max_topics:
                diagnostics.append(f"topic_limit:{topic}")
                continue
            topics.append(topic)
        normalized.append(TaskKnowledgeRoute(task_id=route.task_id, knowledge_topics=tuple(topics)))
    for task_id in task_by_id:
        if task_id not in seen_tasks:
            normalized.append(TaskKnowledgeRoute(task_id=task_id))
            diagnostics.append(f"missing_task_route:{task_id}")
    return KnowledgeRoutePlan(plan_unit_id=plan_unit.id, task_routes=tuple(normalized)), tuple(diagnostics)


__all__ = ["knowledge_topic_registry", "prompt_hash", "run_knowledge_route"]
