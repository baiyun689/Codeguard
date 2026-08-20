"""确定性 Top-K Knowledge 选择器。

组合 Risk prior、patch semantics、file role 和 context symbols 的多源评分，
产出有预算的 KnowledgeBundle。
"""

from __future__ import annotations

import logging
import re
import unicodedata

from codeguard_agent.models.knowledge import (
    KnowledgeBudget,
    KnowledgeBundle,
    KnowledgeFragment,
    KnowledgeSelectionSource,
    SelectedKnowledge,
)
from codeguard_agent.models.tasks import (
    ReviewerKind,
    ReviewTask,
    TaskContextBundle,
    TaskRiskPrior,
)
from codeguard_agent.pipeline.knowledge.catalog import KnowledgeCatalog
from codeguard_agent.pipeline.risk.rules.roles import matching_roles

logger = logging.getLogger("codeguard")

# ── 评分权重(启发式默认值,标定工具 = eval-triage-off 消融档)──
# risk prior 权重最高但 path 来源打 5 折(路径只是上下文);patch 强词命中最重
# (审查对象本身);file role 中等(文件名是弱角色信号);context symbol 命中
# (预取事实里的注解/类名)介于两者之间。数值未做数据标定,调参需对照重跑。
_MIN_SCORE_THRESHOLD = 0.5
_RISK_PRIOR_WEIGHT = 2.0
_STRONG_TERM_SCORE = 1.2
_WEAK_TERM_SCORE = 0.3
_SUBSTRING_CAP = 0.3
_FILE_ROLE_SCORE = 0.6
_CONTEXT_SYMBOL_SCORE_MIN = 0.4
_CONTEXT_SYMBOL_SCORE_MAX = 0.8
_PATH_PRIOR_DISCOUNT = 0.5

def _normalize(text: str) -> str:
    """稳定小写 + 去连字符 + 空白归一。"""
    normalized = unicodedata.normalize("NFKC", text).lower()
    normalized = normalized.replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", " ", normalized).strip()


def _contains_any_strong(normalized_text: str, terms: tuple[str, ...]) -> bool:
    """强词匹配：完整词边界。"""
    for term in terms:
        pattern = rf"(?<![a-z0-9一-鿿]){re.escape(term)}(?![a-z0-9一-鿿])"
        if re.search(pattern, normalized_text):
            return True
    return False


def _contains_any_weak(normalized_text: str, terms: tuple[str, ...]) -> bool:
    """弱词匹配：子串即可。"""
    return any(term in normalized_text for term in terms)


def _score_patch_semantics(
    patch_text: str, fragment: KnowledgeFragment,
) -> tuple[float, KnowledgeSelectionSource | None]:
    if not patch_text.strip():
        return 0.0, None
    norm = _normalize(patch_text)
    strong_terms = tuple(_normalize(t) for t in fragment.strong_terms)
    weak_terms = tuple(_normalize(t) for t in fragment.weak_terms)
    score = 0.0
    if _contains_any_strong(norm, strong_terms):
        score += _STRONG_TERM_SCORE
    if _contains_any_weak(norm, weak_terms):
        score += min(_WEAK_TERM_SCORE, _SUBSTRING_CAP)
    return score, KnowledgeSelectionSource.PATCH_SEMANTICS if score > 0 else None


def _score_risk_prior(
    fragment: KnowledgeFragment, prior: TaskRiskPrior,
) -> tuple[float, KnowledgeSelectionSource | None]:
    if fragment.risk_tag is None:
        return 0.0, None
    for h in prior.hypotheses:
        if h.tag == fragment.risk_tag:
            weight = _RISK_PRIOR_WEIGHT
            if h.source_kind == "path":
                weight *= _PATH_PRIOR_DISCOUNT
            return weight * h.match_confidence, KnowledgeSelectionSource.RISK_PRIOR
    return 0.0, None


def _score_file_role(
    fragment: KnowledgeFragment, file_path: str,
) -> tuple[float, KnowledgeSelectionSource | None]:
    # 角色 → 标签映射来自 risk/rules/roles.py 单一注册表,与 triage 的
    # 弱路径信号共用一份事实源。
    if fragment.risk_tag is None:
        return 0.0, None
    if any(
        fragment.risk_tag in spec.tags for spec in matching_roles(file_path)
    ):
        return _FILE_ROLE_SCORE, KnowledgeSelectionSource.FILE_ROLE
    return 0.0, None


def _score_context_symbols(
    fragment: KnowledgeFragment, context: TaskContextBundle | None,
) -> tuple[float, KnowledgeSelectionSource | None]:
    if context is None or not context.facts or fragment.risk_tag is None:
        return 0.0, None
    tag_value = fragment.risk_tag.value
    joined = " ".join(fact.content for fact in context.facts)
    norm = _normalize(joined)
    topic_indicators: dict[str, tuple[str, ...]] = {
        "TRANSACTION_ATOMICITY": ("@transactional", "transactiontemplate", "transaction"),
        "MESSAGE_DELIVERY": ("kafka", "rabbit", "publish", "eventpublisher", "applicationevent"),
        "SQL_DATA_ACCESS": ("@query", "@modifying", "jdbctemplate", "entitymanager"),
        "CACHE_CONSISTENCY": ("@cacheable", "@cacheevict", "@cacheput", "caffeine"),
        "AUTHORIZATION": ("@preauthorize", "@postauthorize", "@secured"),
        "CONCURRENCY_CONSISTENCY": ("@lock", "synchronized", "reentrantlock"),
    }
    indicators = topic_indicators.get(tag_value, ())
    for ind in indicators:
        if _normalize(ind) in norm:
            return _CONTEXT_SYMBOL_SCORE_MAX, KnowledgeSelectionSource.CONTEXT_SYMBOL
    return 0.0, None


def _select_specialized(
    fragments: list[KnowledgeFragment],
    task: ReviewTask,
    prior: TaskRiskPrior,
    context: TaskContextBundle | None,
    budget: KnowledgeBudget,
) -> tuple[list[SelectedKnowledge], tuple[str, ...], tuple[str, ...]]:
    scored: list[SelectedKnowledge] = []
    diagnostics: list[str] = []

    for fragment in fragments:
        risk_score, risk_source = _score_risk_prior(fragment, prior)
        patch_score, patch_source = _score_patch_semantics(task.patch, fragment)
        file_score, file_source = _score_file_role(fragment, task.file)
        ctx_score, ctx_source = _score_context_symbols(fragment, context)

        total = risk_score + patch_score + file_score + ctx_score
        reasons = []
        if risk_score > 0:
            reasons.append(f"risk prior ({risk_score:.1f})")
        if patch_score > 0:
            reasons.append(f"patch semantics ({patch_score:.1f})")
        if file_score > 0:
            reasons.append(f"file role ({file_score:.1f})")
        if ctx_score > 0:
            reasons.append(f"context symbol ({ctx_score:.1f})")

        if total >= _MIN_SCORE_THRESHOLD:
            scored.append(SelectedKnowledge(
                fragment=fragment,
                score=total,
                reasons=tuple(reasons),
            ))

    scored.sort(key=lambda s: (-s.score, s.fragment.topic))

    selected: list[SelectedKnowledge] = []
    seen_clusters: set[str] = set()
    for sk in scored:
        cluster = sk.fragment.topic.split("_")[0] if "_" in sk.fragment.topic else sk.fragment.topic
        if cluster in seen_clusters:
            continue
        if len(selected) >= budget.max_specialized_fragments:
            break
        seen_clusters.add(cluster)
        selected.append(sk)

    omitted = tuple(sk.fragment.topic for sk in scored if sk not in selected)
    if omitted:
        diagnostics.append(f"omitted topics: {', '.join(omitted)}")

    return selected, omitted, tuple(diagnostics)


def _render_bundle(
    base: SelectedKnowledge | None,
    specialized: list[SelectedKnowledge],
    task_id: str,
    reviewer: ReviewerKind,
    budget: KnowledgeBudget,
    omitted: tuple[str, ...],
    diagnostics: tuple[str, ...],
) -> KnowledgeBundle:
    parts: list[str] = []
    truncated = False
    omitted_topics = list(omitted)

    if base is not None:
        base_content = base.fragment.content
        if len(base_content) > budget.reserved_base_chars:
            base_content = base_content[:budget.reserved_base_chars] + "\n...(BASE 已达预算上限)"
        parts.append("## Reviewer base method")
        parts.append(base_content)
        parts.append("")

    if specialized:
        parts.append("## Specialized review hypotheses")
        for sk in specialized:
            reason_str = "; ".join(sk.reasons) if sk.reasons else "selected"
            parts.append(f"### {sk.fragment.topic}")
            parts.append(f"Selection reason: {reason_str}")
            parts.append(sk.fragment.content)
            parts.append("")

    parts.append("## Knowledge usage constraints")
    parts.append("- These are review heuristics, not facts about the current code.")
    parts.append("- Support every candidate with the patch, supplied facts, or tool output.")
    parts.append("- Ignore a heuristic when the current code does not satisfy its trigger.")

    rendered = "\n".join(parts).strip()

    # 预算截断：从最低分 specialized 开始移除
    while len(rendered) > budget.max_chars and specialized:
        removed = specialized.pop()
        omitted_topics.append(removed.fragment.topic)
        truncated = True
        # 重新渲染
        parts = []
        if base is not None:
            base_content = base.fragment.content
            if len(base_content) > budget.reserved_base_chars:
                base_content = base_content[:budget.reserved_base_chars] + "\n...(BASE 已达预算上限)"
            parts.append("## Reviewer base method")
            parts.append(base_content)
            parts.append("")
        if specialized:
            parts.append("## Specialized review hypotheses")
            for sk in specialized:
                reason_str = "; ".join(sk.reasons) if sk.reasons else "selected"
                parts.append(f"### {sk.fragment.topic}")
                parts.append(f"Selection reason: {reason_str}")
                parts.append(sk.fragment.content)
                parts.append("")
        parts.append("## Knowledge usage constraints")
        parts.append("- These are review heuristics, not facts about the current code.")
        parts.append("- Support every candidate with the patch, supplied facts, or tool output.")
        parts.append("- Ignore a heuristic when the current code does not satisfy its trigger.")
        rendered = "\n".join(parts).strip()

    # 硬截断：当移除所有 specialized 后仍超预算时
    if len(rendered) > budget.max_chars:
        rendered = rendered[:budget.max_chars]
        truncated = True

    return KnowledgeBundle(
        task_id=task_id,
        reviewer=reviewer,
        base=base,
        specialized=tuple(specialized),
        rendered_text=rendered,
        truncated=truncated,
        omitted_topics=tuple(omitted_topics),
        diagnostics=diagnostics,
    )


def select_knowledge(
    *,
    reviewer: ReviewerKind,
    task: ReviewTask,
    prior: TaskRiskPrior,
    context: TaskContextBundle | None,
    catalog: KnowledgeCatalog,
    budget: KnowledgeBudget,
) -> KnowledgeBundle:
    """为一个 (task, reviewer) 选择两层 Knowledge 包。"""
    diagnostics: list[str] = []

    base_fragment = catalog.base_fragment(reviewer)
    base: SelectedKnowledge | None = None
    if base_fragment is not None:
        base = SelectedKnowledge(
            fragment=base_fragment,
            score=1.0,
            reasons=("reviewer baseline methodology",),
        )
    else:
        diagnostics.append(f"missing_base:{reviewer.value}")

    all_specialized = list(catalog.specialized_fragments(reviewer))
    selected, omitted, select_diags = _select_specialized(
        all_specialized, task, prior, context, budget,
    )
    diagnostics.extend(select_diags)

    return _render_bundle(
        base, selected, task.id, reviewer, budget, omitted, tuple(diagnostics),
    )
