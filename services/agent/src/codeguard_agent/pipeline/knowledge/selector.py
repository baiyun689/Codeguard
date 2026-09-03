"""按 Plan 显式主题选择 Knowledge，并执行确定性校验与截断。"""

from __future__ import annotations

from codeguard_agent.models.knowledge import (
    KnowledgeBudget,
    KnowledgeBundle,
    KnowledgeFragment,
    SelectedKnowledge,
)
from codeguard_agent.models.tasks import ReviewerKind
from codeguard_agent.pipeline.knowledge.catalog import KnowledgeCatalog


def _render_bundle(
    base: SelectedKnowledge | None,
    specialized: list[SelectedKnowledge],
    reviewer: ReviewerKind,
    budget: KnowledgeBudget,
    omitted: tuple[str, ...],
    diagnostics: tuple[str, ...],
) -> KnowledgeBundle:
    def render(items: list[SelectedKnowledge]) -> str:
        parts: list[str] = []
        if base is not None:
            parts.extend(("## Reviewer base method", base.fragment.content, ""))
        if items:
            parts.append("## Specialized review methods")
            for item in items:
                parts.extend((f"### {item.fragment.topic}", item.fragment.content, ""))
        parts.extend(
            (
                "## Knowledge usage constraints",
                "- These are review methods, not facts about the current code.",
                "- Support every candidate with the patch, supplied facts, or tool output.",
            )
        )
        return "\n".join(parts).strip()

    selected = list(specialized)
    truncated = False
    omitted_topics = list(omitted)
    rendered = render(selected)
    while len(rendered) > budget.max_chars and selected:
        omitted_topics.append(selected.pop().fragment.topic)
        truncated = True
        rendered = render(selected)
    if len(rendered) > budget.max_chars:
        rendered = rendered[: budget.max_chars]
        truncated = True
    return KnowledgeBundle(
        task_id="plan",
        reviewer=reviewer,
        base=base,
        specialized=tuple(selected),
        rendered_text=rendered,
        truncated=truncated,
        omitted_topics=tuple(omitted_topics),
        diagnostics=diagnostics,
    )


def select_knowledge(
    *,
    reviewer: ReviewerKind,
    requested_topics: tuple[str, ...],
    catalog: KnowledgeCatalog,
    budget: KnowledgeBudget,
) -> KnowledgeBundle:
    """只接受 Plan 主题；非法、跨 Reviewer 或重复主题均确定性拒绝。"""
    diagnostics: list[str] = []
    base_fragment = catalog.base_fragment(reviewer)
    base = (
        SelectedKnowledge(
            fragment=base_fragment,
            score=1.0,
            reasons=("reviewer baseline methodology",),
        )
        if base_fragment is not None
        else None
    )
    if base is None:
        diagnostics.append(f"missing_base:{reviewer.value}")

    by_topic: dict[str, KnowledgeFragment] = {
        fragment.topic: fragment
        for fragment in catalog.specialized_fragments(reviewer)
    }
    selected: list[SelectedKnowledge] = []
    omitted: list[str] = []
    seen: set[str] = set()
    for topic in requested_topics:
        if topic in seen:
            diagnostics.append(f"duplicate_topic:{topic}")
            continue
        seen.add(topic)
        fragment = by_topic.get(topic)
        if fragment is None:
            omitted.append(topic)
            diagnostics.append(f"rejected_topic:{topic}")
            continue
        if len(selected) >= budget.max_specialized_fragments:
            omitted.append(topic)
            diagnostics.append(f"topic_limit:{topic}")
            continue
        selected.append(
            SelectedKnowledge(
                fragment=fragment,
                score=1.0,
                reasons=("selected by Plan",),
            )
        )
    return _render_bundle(
        base,
        selected,
        reviewer,
        budget,
        tuple(omitted),
        tuple(diagnostics),
    )


def select_shared_knowledge(
    *,
    requested_topics: tuple[str, ...],
    catalog: KnowledgeCatalog,
    budget: KnowledgeBudget,
    task_id: str = "plan",
) -> KnowledgeBundle:
    """为 controlled task 选择一个供三个 reviewer 共享的专项知识包。

    这里不接受 reviewer 参数，也不根据领域拆分。主题必须来自合并后的闭集；
    非法主题被记录并忽略，超出预算的主题按 Plan 顺序省略。
    """
    diagnostics: list[str] = []
    by_topic = {
        fragment.topic: fragment
        for fragment in catalog.shared_specialized_fragments()
    }
    selected: list[SelectedKnowledge] = []
    omitted: list[str] = []
    seen: set[str] = set()
    for topic in requested_topics:
        if topic in seen:
            diagnostics.append(f"duplicate_topic:{topic}")
            continue
        seen.add(topic)
        fragment = by_topic.get(topic)
        if fragment is None:
            omitted.append(topic)
            diagnostics.append(f"rejected_topic:{topic}")
            continue
        if len(selected) >= budget.max_specialized_fragments:
            omitted.append(topic)
            diagnostics.append(f"topic_limit:{topic}")
            continue
        selected.append(
            SelectedKnowledge(
                fragment=fragment,
                score=1.0,
                reasons=("selected by task ReviewPlan",),
            )
        )

    # Use a neutral base marker; reviewer-specific BASE methodology is supplied by
    # each direct prompt and is intentionally not routed by ReviewPlan.
    return _render_bundle(
        base=None,
        specialized=selected,
        reviewer=ReviewerKind.BEHAVIOR,
        budget=budget,
        omitted=tuple(omitted),
        diagnostics=tuple(diagnostics),
    ).model_copy(update={"task_id": task_id})
