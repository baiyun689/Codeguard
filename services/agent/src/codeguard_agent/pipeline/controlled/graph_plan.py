"""受控模式 GraphPlan：把可疑候选转换为有限证据步骤。"""

from __future__ import annotations

import logging
import hashlib
from pathlib import Path
from typing import Any

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.tasks import (
    AssessmentStatus,
    CandidateSeed,
    EvidenceAssessment,
    EvidenceNeed,
    EvidenceStep,
    ProofMatch,
    ProofMatchStatus,
    ReviewerGraphPlan,
    ReviewerKind,
    TaskSymbolContext,
    WorkItem,
)
from codeguard_agent.pipeline.controlled.contracts import get_tool_proof_contract
from codeguard_agent.pipeline.controlled.llm_contracts import LlmReviewerGraphPlan
from codeguard_agent.pipeline.controlled.routing import validate_graph_question

logger = logging.getLogger("codeguard")
_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts" / "controlled"

_DOMAIN_PROMPTS = {
    ReviewerKind.BEHAVIOR: "graph-plan-behavior.txt",
    ReviewerKind.THREAT_MODEL: "graph-plan-threat.txt",
    ReviewerKind.MAINTAINABILITY: "graph-plan-maintainability.txt",
}

# Domain allowlists are deliberately explicit even though the current release
# exposes the same four tools. Future domain-specific tools must be added here,
# then added to the proof contract registry and tests together.
DOMAIN_TOOL_ALLOWLIST: dict[ReviewerKind, frozenset[str]] = {
    ReviewerKind.BEHAVIOR: frozenset({
        "get_file_content", "inspect_structure", "inspect_change_impact", "inspect_path"
    }),
    ReviewerKind.THREAT_MODEL: frozenset({
        "get_file_content", "inspect_structure", "inspect_change_impact", "inspect_path"
    }),
    ReviewerKind.MAINTAINABILITY: frozenset({
        "get_file_content", "inspect_structure", "inspect_change_impact", "inspect_path"
    }),
}


def graph_replan_reason(
    assessment: EvidenceAssessment | None,
    proof: ProofMatch | None = None,
) -> str | None:
    """Return the bounded-replan reason for an evidence assessment.

    ``NEEDS_EVIDENCE`` is the explicit LLM request, but it is not the only
    way an evidence gap can be represented.  Deterministic proof matching and
    provider-compatible models may instead produce ``indeterminate`` or
    ``unresolved`` after a graph response is incomplete.  Those states still
    have a chance of being closed by one additional query and must not be
    dropped before Graph Replan.  ``partial`` is eligible when the model
    leaves a concrete open question or bounded additional step.  It is also
    eligible when the deterministic matcher marks the proof itself
    partial/indeterminate, because the model can still choose a source lookup
    from the already-visible graph facts.

    Definitive negative/rejection states intentionally return ``None``.  The
    final candidate gate remains fail-closed after this one bounded attempt.
    """

    if assessment is None:
        return None
    status = assessment.status
    if status is AssessmentStatus.NEEDS_EVIDENCE:
        return "needs_evidence"
    if status is AssessmentStatus.INDETERMINATE:
        return "indeterminate"
    if status is AssessmentStatus.UNRESOLVED:
        return "unresolved"
    if status is AssessmentStatus.PARTIAL and (
        bool(assessment.additional_evidence_question.strip())
        or bool(assessment.additional_steps)
        or (
            proof is not None
            and proof.status
            in {ProofMatchStatus.PARTIAL, ProofMatchStatus.INDETERMINATE}
        )
    ):
        return "partial_with_open_gap"
    return None


def _system_prompt(reviewer: ReviewerKind) -> str:
    common = (_PROMPT_DIR / "graph-plan-common.txt").read_text(encoding="utf-8")
    domain = (_PROMPT_DIR / _DOMAIN_PROMPTS[reviewer]).read_text(encoding="utf-8")
    return f"{common.strip()}\n\n{domain.strip()}"


def prompt_hash(reviewer: ReviewerKind) -> str:
    return hashlib.sha256(_system_prompt(reviewer).encode("utf-8")).hexdigest()[:16]


def build_graph_plan_user_prompt(
    *,
    reviewer: ReviewerKind,
    task_id: str,
    seeds: tuple[CandidateSeed, ...],
    symbol_context: TaskSymbolContext | None,
    max_path_depth: int,
    enabled_tools: frozenset[str] | set[str] | None = None,
) -> str:
    symbols = (
        "\n".join(symbol.model_dump_json() for symbol in symbol_context.symbols)
        if symbol_context is not None and symbol_context.symbols
        else "(无已解析 symbol；不能生成可执行图谱步骤)"
    )
    # Do not echo provider-compatibility defaults into the next LLM prompt;
    # they are transport-only fields and their empty values invite the model
    # to reproduce metadata instead of planning executable evidence steps.
    seed_text = "\n".join(
        seed.model_dump_json(exclude_defaults=True) for seed in seeds
    )
    tool_text = ", ".join(sorted(enabled_tools)) if enabled_tools is not None else "全部已注册工具"
    return (
        f'<graph_plan reviewer="{reviewer.value}" task_id="{task_id}" '
        f'max_path_depth="{max_path_depth}">\n'
        f"<symbol_context>\n{symbols}\n</symbol_context>\n"
        f"<graph_seeds>\n{seed_text}\n</graph_seeds>\n"
        f"允许工具：{tool_text}\n"
        "只为 graph_needed seed 生成 WorkItem；subject_ref 必须是 symbol_context 中出现的精确 symbol_id。"
    )


def _normalise_step(
    step: EvidenceStep,
    *,
    seed: CandidateSeed,
    allowed_symbols: set[str],
    max_path_depth: int,
    reviewer: ReviewerKind,
    index: int,
    enabled_tools: frozenset[str] | set[str] | None = None,
) -> tuple[EvidenceStep | None, tuple[str, ...]]:
    diagnostics: list[str] = []
    contract = get_tool_proof_contract(step.tool)
    if contract is None:
        return None, (f"unknown_tool:{step.tool}",)
    if step.tool not in DOMAIN_TOOL_ALLOWLIST[reviewer]:
        return None, (f"tool_not_allowed:{step.tool}",)
    if enabled_tools is not None and step.tool not in enabled_tools:
        return None, (f"tool_disabled:{step.tool}",)
    if step.subject_ref not in allowed_symbols:
        return None, (f"unknown_subject_ref:{step.subject_ref}",)
    question = seed.graph_question
    if question is None:
        return None, ("seed_graph_question_missing",)
    if not step.purpose or not step.expected_fact:
        step = step.model_copy(
            update={
                "purpose": step.purpose or "获取该步骤允许的直接证据",
                "expected_fact": step.expected_fact or question.question,
            }
        )
        diagnostics.append("step_explanation_filled")
    if step.tool == "inspect_path":
        if question.direction != "downstream":
            return None, ("inspect_path_requires_downstream_question",)
        if step.path_kind not in {"behavior", "security"}:
            return None, ("inspect_path_requires_path_kind",)
        if question.path_kind is not None and step.path_kind != question.path_kind:
            return None, ("step_path_kind_mismatch",)
        depth = step.max_depth or question.max_depth
        if depth > max_path_depth:
            diagnostics.append("path_depth_capped")
            depth = max_path_depth
        step = step.model_copy(update={"max_depth": depth})
    elif step.tool == "inspect_change_impact":
        if question.direction != "upstream":
            return None, ("inspect_change_impact_requires_upstream_question",)
        if step.path_kind is not None or step.max_depth is not None:
            return None, ("impact_step_does_not_accept_path_arguments",)
    elif step.tool == "get_file_content":
        if step.path_kind is not None or step.max_depth is not None:
            return None, ("source_step_has_graph_arguments",)
    elif step.tool == "inspect_structure":
        if step.path_kind is not None or step.max_depth is not None:
            return None, ("structure_step_has_path_arguments",)
    return step.model_copy(update={"step_id": f"step-{index}"}), tuple(diagnostics)


def validate_graph_plan(
    plan: ReviewerGraphPlan,
    *,
    reviewer: ReviewerKind,
    task_id: str,
    seeds: tuple[CandidateSeed, ...],
    symbol_context: TaskSymbolContext | None,
    max_path_depth: int,
    enabled_tools: frozenset[str] | set[str] | None = None,
) -> tuple[ReviewerGraphPlan, tuple[str, ...]]:
    """确定性校验和绑定 GraphPlan；非法 WorkItem 隔离。"""

    diagnostics: list[str] = []
    seed_by_id = {seed.seed_id: seed for seed in seeds}
    allowed_symbols = {
        symbol.symbol_id
        for symbol in (symbol_context.symbols if symbol_context is not None else ())
        if symbol.symbol_id
    }
    source_symbols = {
        symbol.symbol_id
        for symbol in (symbol_context.symbols if symbol_context is not None else ())
        if symbol.symbol_id
        and str(symbol.kind).upper()
        in {"METHOD", "CONSTRUCTOR", "FIELD", "FRAMEWORK_ENTRYPOINT"}
    }
    valid_items: list[WorkItem] = []
    seen_seeds: set[str] = set()
    for item_index, item in enumerate(plan.work_items, start=1):
        seed = seed_by_id.get(item.seed_id)
        if seed is None:
            diagnostics.append(f"unknown_seed:{item.seed_id}")
            continue
        if item.seed_id in seen_seeds:
            diagnostics.append(f"duplicate_seed:{item.seed_id}")
            continue
        seen_seeds.add(item.seed_id)
        if item.reviewer is not reviewer:
            diagnostics.append(f"reviewer_mismatch:{item.seed_id}")
            continue
        question_errors = validate_graph_question(seed.graph_question) if seed.graph_question else ("graph_question_missing",)
        if question_errors:
            diagnostics.extend(f"{item.seed_id}:{error}" for error in question_errors)
            continue
        if seed.graph_question is not None and seed.graph_question.subject_ref not in allowed_symbols:
            diagnostics.append(
                f"{item.seed_id}:unknown_question_subject:{seed.graph_question.subject_ref}"
            )
            continue
        # Models often describe the same graph query twice with different
        # prose (for example, one step for the path and another for the
        # listener branch).  Deduplicate by executable query before enforcing
        # the two-step budget; otherwise a harmless wording difference causes
        # the whole WorkItem to fall back to the weak one-step baseline.
        raw_steps = list(item.evidence_steps)
        deduped_steps: list[EvidenceStep] = []
        step_aliases: dict[str, str] = {}
        query_owner: dict[tuple[str, str, str, int | None], str] = {}
        for original_index, step in enumerate(raw_steps, start=1):
            original_id = step.step_id or f"step-{original_index}"
            query_key = (
                step.tool,
                step.subject_ref,
                step.path_kind or "",
                step.max_depth,
            )
            owner = query_owner.get(query_key)
            if owner is not None:
                step_aliases[original_id] = owner
                diagnostics.append(f"duplicate_query:{item.seed_id}:{original_id}:{owner}")
                continue
            query_owner[query_key] = original_id
            step_aliases[original_id] = original_id
            deduped_steps.append(step)
        if len(deduped_steps) > 2:
            diagnostics.append(f"too_many_steps_trimmed:{item.seed_id}")
            # Do not trim here.  The first two provider steps are not
            # necessarily the two contractually useful steps (providers often
            # emit an unrelated impact query before the requested path).  The
            # stabilizer below selects the required graph fact and the subject
            # source from the complete, already bounded provider response.
        if _dependency_cycle(tuple(deduped_steps)):
            diagnostics.append(f"dependency_cycle:{item.seed_id}")
            diagnostics.append(f"work_item_isolated:{item.seed_id}")
            continue
        deduped_steps, lifecycle_diagnostics = _stabilize_graph_steps(
            deduped_steps,
            seed=seed,
            allowed_symbols=allowed_symbols,
            source_symbols=source_symbols,
            symbol_context=symbol_context,
            max_path_depth=max_path_depth,
            enabled_tools=enabled_tools,
        )
        diagnostics.extend(
            f"{item.seed_id}:{diagnostic}" for diagnostic in lifecycle_diagnostics
        )
        if not deduped_steps:
            diagnostics.append(f"work_item_isolated:{item.seed_id}")
            continue
        steps: list[EvidenceStep] = []
        invalid = False
        step_names: set[str] = set()
        step_id_map = {
            original_id: f"step-{step_index}"
            for step_index, step in enumerate(deduped_steps, start=1)
            for original_id in (
                step.step_id or f"step-{step_index}",
            )
        }
        # Map a duplicate's declared id to the canonical step id as well.
        # Dependencies are checked after this remapping, so duplicate query
        # aliases cannot create a false unknown-dependency failure.
        canonical_names = {
            step.step_id or f"step-{step_index}": f"step-{step_index}"
            for step_index, step in enumerate(deduped_steps, start=1)
        }
        step_id_map.update(
            {
                original_id: canonical_names.get(owner, owner)
                for original_id, owner in step_aliases.items()
                if owner in canonical_names
            }
        )
        for step_index, step in enumerate(deduped_steps, start=1):
            if step.step_id and step.step_id in step_names:
                diagnostics.append(f"duplicate_step:{item.seed_id}:{step.step_id}")
                invalid = True
                break
            step_names.add(step.step_id)
            normalized, step_diagnostics = _normalise_step(
                step,
                seed=seed,
                allowed_symbols=allowed_symbols,
                max_path_depth=max_path_depth,
                reviewer=reviewer,
                index=step_index,
                enabled_tools=enabled_tools,
            )
            diagnostics.extend(f"{item.seed_id}:{error}" for error in step_diagnostics)
            if normalized is None:
                invalid = True
                break
            steps.append(
                normalized.model_copy(
                    update={
                        "depends_on": tuple(
                            step_id_map.get(dep, dep) for dep in step.depends_on
                        )
                    }
                )
            )
        if invalid or not steps:
            diagnostics.append(f"work_item_isolated:{item.seed_id}")
            continue
        step_ids = {step.step_id for step in steps}
        if any(dep not in step_ids for step in steps for dep in step.depends_on):
            diagnostics.append(f"unknown_dependency:{item.seed_id}")
            continue
        if any(step.step_id in step.depends_on for step in steps):
            diagnostics.append(f"self_dependency:{item.seed_id}")
            continue
        if _dependency_cycle(tuple(steps)):
            diagnostics.append(f"dependency_cycle:{item.seed_id}")
            continue
        valid_items.append(
            item.model_copy(
                update={
                    "work_item_id": f"wi-{reviewer.value}-{task_id}-{len(valid_items)+1}",
                    "evidence_steps": tuple(steps),
                }
            )
        )
    return ReviewerGraphPlan(reviewer=reviewer, task_id=task_id, work_items=tuple(valid_items)), tuple(diagnostics)


def _stabilize_graph_steps(
    steps: list[EvidenceStep],
    *,
    seed: CandidateSeed,
    allowed_symbols: set[str],
    source_symbols: set[str] | None = None,
    symbol_context: TaskSymbolContext | None = None,
    max_path_depth: int,
    enabled_tools: frozenset[str] | set[str] | None = None,
) -> tuple[list[EvidenceStep], tuple[str, ...]]:
    """Make every graph-required WorkItem carry complementary proof.

    GraphPlan remains LLM-owned: the model chooses the target symbols and
    purpose.  This deterministic contract only repairs an under-specified
    plan.  A downstream question must have one ``inspect_path`` step; an
    upstream question must have one ``inspect_change_impact`` step.  When the
    source tool is enabled, the second bounded slot reads the question's
    subject symbol so the Judge always sees the changed method's direct code
    alongside the graph fact.  Existing model-selected steps are preserved
    whenever they already satisfy those two roles.
    """

    diagnostics: list[str] = []
    question = seed.graph_question
    if question is None:
        return steps[:2], ()

    graph_tool = _graph_tool_for_seed(seed)
    graph_enabled = enabled_tools is None or graph_tool in enabled_tools

    # A GraphQuestion is the executable contract.  Search the whole bounded
    # provider plan for a step satisfying that contract instead of trusting
    # provider ordering.  A model may emit an impact query, a duplicate path,
    # or an endpoint source before the actual requested graph fact.
    graph_step: EvidenceStep | None = None
    for step in steps:
        if step.tool != graph_tool or step.subject_ref != question.subject_ref:
            continue
        if graph_tool == "inspect_path" and (
            step.path_kind != question.path_kind
            or step.path_kind not in {"behavior", "security"}
        ):
            continue
        if graph_tool != "inspect_path" and (
            step.path_kind is not None or step.max_depth is not None
        ):
            continue
        graph_step = step
        break

    if graph_step is None and graph_enabled:
        graph_step = EvidenceStep(
            step_id="graph-proof",
            tool=graph_tool,
            subject_ref=question.subject_ref,
            path_kind=(
                question.path_kind
                if graph_tool == "inspect_path"
                else None
            ),
            max_depth=(
                min(question.max_depth, max_path_depth)
                if graph_tool == "inspect_path"
                else None
            ),
            purpose="执行 GraphQuestion 要求的有界关系查询",
            expected_fact=question.question,
            required=True,
        )
        diagnostics.append(f"{graph_tool}_step_inserted")

    # If the configured tool budget intentionally disables the required graph
    # tool, there is no truthful graph proof to execute.  Keep only a bounded
    # provider step; normal validation will isolate it when its direction is
    # incompatible, rather than silently changing the question.
    if graph_step is None:
        return steps[:2], tuple(diagnostics)

    selected: list[EvidenceStep] = [graph_step]
    source_enabled = enabled_tools is None or "get_file_content" in enabled_tools
    source_subject_ref = _source_subject_for_seed(
        seed=seed,
        subject_ref=question.subject_ref,
        source_symbols=source_symbols,
        symbol_context=symbol_context,
    )
    if (
        source_enabled
        and source_subject_ref in allowed_symbols
    ):
        source_step = next(
            (
                step
                for step in steps
                if step.tool == "get_file_content"
                and step.subject_ref == source_subject_ref
                and step.path_kind is None
                and step.max_depth is None
            ),
            None,
        )
        if source_step is None:
            source_step = EvidenceStep(
                step_id="subject-source",
                tool="get_file_content",
                subject_ref=source_subject_ref,
                purpose="读取 GraphQuestion 主体 symbol 的局部源码",
                expected_fact="确认候选涉及的主体方法及其变更相关语义",
                required=True,
            )
            diagnostics.append("subject_source_step_added")
        selected.append(source_step)

    # The initial plan has exactly two proof slots: the canonical graph fact
    # and the subject's local source.  Any endpoint/branch exploration remains
    # a Delta decision after the graph response, so it cannot evict the
    # contractually required fact here.
    return selected[:2], tuple(diagnostics)


def _source_subject_for_seed(
    *,
    seed: CandidateSeed,
    subject_ref: str,
    source_symbols: set[str] | None,
    symbol_context: TaskSymbolContext | None,
) -> str:
    """Choose a bounded source symbol when a provider selected a type symbol.

    Graph questions sometimes use an owning type as their subject even though
    the changed line belongs to a field or method.  A type-level source read
    is intentionally rejected by the Gateway size guard; resolving to one
    already-parsed member at the candidate line keeps the complementary local
    proof executable without guessing a new symbol or changing the graph
    question itself.
    """

    allowed = source_symbols or set()
    if subject_ref in allowed:
        return subject_ref
    if symbol_context is None or seed.location_line <= 0:
        return ""
    task_file = seed.location_file.replace("\\", "/").strip().lower()
    candidates = [
        symbol
        for symbol in symbol_context.symbols
        if symbol.symbol_id in allowed
        and symbol.file.replace("\\", "/").strip().lower() == task_file
        and symbol.start_line <= seed.location_line <= symbol.end_line
    ]
    if not candidates:
        return ""
    # Exact field/constructor declarations are the smallest and most precise
    # source unit for modifier/initialization changes.  Otherwise select one
    # enclosing method only when the line identifies it unambiguously.
    exact_fields = [
        symbol
        for symbol in candidates
        if str(symbol.kind).upper() == "FIELD"
        and symbol.start_line == seed.location_line
    ]
    if len(exact_fields) == 1:
        return exact_fields[0].symbol_id
    methods = [
        symbol
        for symbol in candidates
        if str(symbol.kind).upper() in {"METHOD", "CONSTRUCTOR", "FRAMEWORK_ENTRYPOINT"}
    ]
    if len(methods) == 1:
        return methods[0].symbol_id
    return ""


def _graph_tool_for_seed(seed: CandidateSeed) -> str:
    """Map a typed evidence need to its one canonical graph tool.

    This is a protocol repair, not a domain-specific rule: the triage model's
    declared need wins, while the GraphQuestion direction is the compatibility
    fallback for older providers that omitted ``evidence_need``.
    """

    if seed.evidence_need is EvidenceNeed.INSPECT_CHANGE_IMPACT:
        return "inspect_change_impact"
    if seed.evidence_need is EvidenceNeed.INSPECT_STRUCTURE:
        return "inspect_structure"
    if seed.evidence_need is EvidenceNeed.INSPECT_PATH:
        return "inspect_path"
    if seed.graph_question is not None and seed.graph_question.direction == "upstream":
        return "inspect_change_impact"
    return "inspect_path"


def _dependency_cycle(steps: tuple[EvidenceStep, ...]) -> bool:
    """检测 WorkItem 内的依赖环，避免 Executor 进入不可执行的伪 DAG。"""

    dependencies = {step.step_id: set(step.depends_on) for step in steps}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str) -> bool:
        if step_id in visiting:
            return True
        if step_id in visited:
            return False
        visiting.add(step_id)
        if any(visit(dep) for dep in dependencies.get(step_id, ())):
            return True
        visiting.remove(step_id)
        visited.add(step_id)
        return False

    return any(visit(step_id) for step_id in dependencies)


def validate_delta_step(
    step: EvidenceStep,
    *,
    seed: CandidateSeed,
    reviewer: ReviewerKind,
    allowed_symbols: set[str],
    max_path_depth: int,
    enabled_tools: frozenset[str] | set[str] | None = None,
) -> tuple[EvidenceStep | None, tuple[str, ...]]:
    """校验一次 DeltaPlan，复用与初始 GraphPlan 相同的工具护栏。

    Delta 可以引用初始响应新发现的 symbol，因此允许集合由调用方显式传入；
    其它工具、方向、path_kind 和深度约束不能因进入 Delta 而放宽。
    """

    return _normalise_step(
        step,
        seed=seed,
        allowed_symbols=allowed_symbols,
        max_path_depth=max_path_depth,
        reviewer=reviewer,
        index=1,
        enabled_tools=enabled_tools,
    )


def _repair_delta_tool_for_direction(
    step: EvidenceStep,
    *,
    seed: CandidateSeed,
    max_path_depth: int,
    enabled_tools: frozenset[str] | set[str] | None,
) -> tuple[EvidenceStep, tuple[str, ...]]:
    """Repair a provider's graph-tool alias to the GraphQuestion contract.

    The replan model is allowed to describe the missing fact in prose, but
    direction is already fixed by ``GraphQuestion``.  A downstream question
    cannot be executed with ``inspect_change_impact`` and vice versa.  When a
    provider selects the opposite graph tool, translate it to the canonical
    tool instead of discarding the one bounded Delta opportunity.  This never
    introduces a symbol or changes the question's direction.
    """

    question = seed.graph_question
    if question is None:
        return step, ()
    enabled = set(enabled_tools) if enabled_tools is not None else None
    if (
        step.tool == "inspect_change_impact"
        and question.direction == "downstream"
        and (enabled is None or "inspect_path" in enabled)
    ):
        return (
            step.model_copy(
                update={
                    "tool": "inspect_path",
                    "path_kind": question.path_kind or "behavior",
                    "max_depth": min(step.max_depth or question.max_depth, max_path_depth),
                }
            ),
            ("graph_replan_tool_repaired:inspect_change_impact->inspect_path",),
        )
    if (
        step.tool == "inspect_path"
        and question.direction == "upstream"
        and (enabled is None or "inspect_change_impact" in enabled)
    ):
        return (
            step.model_copy(
                update={
                    "tool": "inspect_change_impact",
                    "path_kind": None,
                    "max_depth": None,
                }
            ),
            ("graph_replan_tool_repaired:inspect_path->inspect_change_impact",),
        )
    return step, ()


def _fallback_delta_step(
    *,
    seed: CandidateSeed,
    visible_symbols: set[str],
    visible_source_symbols: set[str],
    executed_queries: set[tuple[str, str, str, int | None]],
    max_path_depth: int,
    enabled_tools: frozenset[str] | set[str] | None,
    assessment: EvidenceAssessment,
) -> tuple[EvidenceStep | None, tuple[str, ...]]:
    """Choose one deterministic, already-visible alternative after bad LLM output.

    Replan remains LLM-directed in the normal case.  This fallback is only
    used when the provider repeats a query or violates the direction contract;
    it chooses among the original subject and symbols already visible in the
    projection, and never performs name-based discovery.
    """

    question = seed.graph_question
    if question is None:
        return None, ("graph_replan_no_graph_question",)
    enabled = set(enabled_tools) if enabled_tools is not None else {
        "get_file_content",
        "inspect_structure",
        "inspect_change_impact",
        "inspect_path",
    }
    purpose = assessment.additional_evidence_question.strip() or seed.mechanism or seed.claim

    def unused(tool: str, subject: str, path_kind: str = "", depth: int | None = None) -> bool:
        return (tool, subject, path_kind, depth) not in executed_queries

    candidates: list[EvidenceStep] = []
    canonical_graph_tool = (
        "inspect_change_impact"
        if question.direction == "upstream"
        else "inspect_path"
    )
    if question.subject_ref in visible_symbols and canonical_graph_tool in enabled:
        path_kind = question.path_kind or "behavior"
        depth = (
            min(question.max_depth, max_path_depth)
            if canonical_graph_tool == "inspect_path"
            else None
        )
        if unused(canonical_graph_tool, question.subject_ref, path_kind if depth else "", depth):
            candidates.append(
                EvidenceStep(
                    tool=canonical_graph_tool,
                    subject_ref=question.subject_ref,
                    path_kind=path_kind if depth else None,
                    max_depth=depth,
                    purpose="补足 GraphQuestion 要求的有界关系事实",
                    expected_fact=purpose,
                    required=True,
                )
            )

    # Prefer a source excerpt for an endpoint named by the question, then any
    # other visible source endpoint.  Source IDs are accepted only from the
    # projection's explicit source set.
    source_candidates = [
        symbol_id
        for symbol_id in sorted(visible_source_symbols)
        if symbol_id in visible_symbols and symbol_id not in {question.subject_ref}
    ]
    source_candidates = [
        *[symbol_id for symbol_id in question.expected_targets if symbol_id in source_candidates],
        *[symbol_id for symbol_id in source_candidates if symbol_id not in question.expected_targets],
    ]
    if "get_file_content" in enabled:
        for symbol_id in source_candidates:
            if unused("get_file_content", symbol_id):
                candidates.append(
                    EvidenceStep(
                        tool="get_file_content",
                        subject_ref=symbol_id,
                        purpose="读取已见端点的局部源码以补足证据",
                        expected_fact=purpose,
                        required=True,
                    )
                )
                break

    if "inspect_structure" in enabled:
        structure_symbols = [
            symbol_id
            for symbol_id in (
                question.subject_ref,
                *question.expected_targets,
                *sorted(visible_symbols),
            )
            if symbol_id in visible_symbols
        ]
        for symbol_id in dict.fromkeys(structure_symbols):
            if unused("inspect_structure", symbol_id):
                candidates.append(
                    EvidenceStep(
                        tool="inspect_structure",
                        subject_ref=symbol_id,
                        purpose="读取已见 symbol 的一跳结构事实",
                        expected_fact=purpose,
                        required=True,
                    )
                )
                break
    if not candidates:
        return None, ("graph_replan_no_alternative_step",)
    return candidates[0], (f"graph_replan_fallback_step:{candidates[0].tool}:{candidates[0].subject_ref}",)


def build_graph_replan_user_prompt(
    *,
    reviewer: ReviewerKind,
    task_id: str,
    seed: CandidateSeed,
    work_item: WorkItem,
    assessment: EvidenceAssessment,
    visible_symbols: tuple[str, ...],
    visible_source_symbols: tuple[str, ...],
    executed_queries: tuple[str, ...],
    max_path_depth: int,
    enabled_tools: frozenset[str] | set[str] | None = None,
) -> str:
    """构造一次性 Delta Replan 请求。

    Replan 只接受运行时已经暴露的 symbol；它不是新的候选发现，也不能
    修改原始 WorkItem。把缺口、可见 symbol 和已执行查询显式写入 prompt，
    可让模型只回答“下一步补什么证据”，而不是恢复自由探索。
    """

    tool_text = ", ".join(sorted(enabled_tools)) if enabled_tools is not None else "全部已注册工具"
    return (
        f'<graph_replan reviewer="{reviewer.value}" task_id="{task_id}" '
        f'work_item_id="{work_item.work_item_id}" max_path_depth="{max_path_depth}">\n'
        f"<candidate_seed>\n{seed.model_dump_json(exclude_defaults=True)}\n</candidate_seed>\n"
        f"<original_work_item>\n{work_item.model_dump_json(exclude_defaults=True)}\n</original_work_item>\n"
        f"<assessment>\n{assessment.model_dump_json(exclude_defaults=True)}\n</assessment>\n"
        f"<visible_symbols>{', '.join(visible_symbols) or '(none)'}</visible_symbols>\n"
        f"<visible_source_symbols>{', '.join(visible_source_symbols) or '(none)'}</visible_source_symbols>\n"
        f"<executed_queries>{'; '.join(executed_queries) or '(none)'}</executed_queries>\n"
        f"允许工具：{tool_text}\n"
        "只输出一个 ReviewerGraphPlan：必须保留同一 reviewer、task_id、seed_id；"
        "work_item_id 按现有 schema 约定可以留空，运行时会绑定回原 WorkItem，"
        "且恰好包含一个 evidence_step。该步骤只能补足 assessment 指出的一个明确事实缺口；"
        "不能重复已执行查询，不能创建新 WorkItem，不能改变 GraphQuestion，不能猜测未出现的 symbol。"
        "get_file_content 只能使用 visible_source_symbols 中的 METHOD/CONSTRUCTOR/FIELD/"
        "FRAMEWORK_ENTRYPOINT；TYPE/INTERFACE/ENUM 等类型级 symbol 即使带有 file 也不能读取。"
    )


def run_graph_replan(
    *,
    reviewer: ReviewerKind,
    task_id: str,
    seed: CandidateSeed,
    work_item: WorkItem,
    assessment: EvidenceAssessment,
    proof: ProofMatch | None = None,
    visible_symbols: set[str],
    visible_source_symbols: set[str],
    executed_queries: set[tuple[str, str, str, int | None]],
    llm: Any,
    max_retries: int,
    structured_method: str,
    max_path_depth: int = 3,
    enabled_tools: frozenset[str] | set[str] | None = None,
) -> tuple[EvidenceStep | None, tuple[str, ...]]:
    """为证据不足的单个 WorkItem 生成并校验一次 Delta Plan。

    该函数故意返回一个 ``EvidenceStep`` 而不是完整计划：调用方已经持有
    原始 WorkItem，追加步骤不会删除、改写或扩展首轮计划。任何协议错误、
    未知 symbol、重复查询或多步骤输出都 fail-closed，交由上层按预算丢弃。
    """

    replan_reason = graph_replan_reason(assessment, proof)
    if replan_reason is None:
        return None, ("graph_replan_not_needed",)
    diagnostics: list[str] = [
        "graph_replan_requested",
        f"graph_replan_trigger:{replan_reason}",
    ]
    if llm is None:
        return None, ("graph_replan_llm_unavailable",)
    if not visible_symbols:
        return None, ("graph_replan_no_visible_symbols",)

    enabled_tool_set = set(enabled_tools) if enabled_tools is not None else None
    system = _system_prompt(reviewer) + "\n\n你现在处于一次性 Delta Replan；只生成一个补充 EvidenceStep。"
    user = build_graph_replan_user_prompt(
        reviewer=reviewer,
        task_id=task_id,
        seed=seed,
        work_item=work_item,
        assessment=assessment,
        visible_symbols=tuple(sorted(visible_symbols)),
        visible_source_symbols=tuple(sorted(visible_source_symbols)),
        executed_queries=tuple(
            f"{tool}:{subject}:{path}:{depth or ''}"
            for tool, subject, path, depth in sorted(
                executed_queries,
                key=lambda item: tuple("" if part is None else str(part) for part in item),
            )
        ),
        max_path_depth=max_path_depth,
        enabled_tools=enabled_tool_set,
    )
    try:
        raw = invoke_with_retry(
            llm.with_structured_output(LlmReviewerGraphPlan, method=structured_method),
            [("system", system), ("human", user)],
            max_retries=max(1, max_retries),
        )
        if raw is None:
            return None, ("graph_replan_empty_response",)
        parsed = ReviewerGraphPlan.model_validate(
            LlmReviewerGraphPlan.model_validate(
                raw.model_dump() if hasattr(raw, "model_dump") else raw
            ).model_dump()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("controlled graph replan failed: %s", exc)
        return None, (f"graph_replan_invalid:{type(exc).__name__}",)

    if parsed.reviewer is not reviewer or parsed.task_id != task_id:
        return None, ("graph_replan_scope_mismatch",)
    if len(parsed.work_items) != 1:
        return None, ("graph_replan_requires_one_work_item",)
    item = parsed.work_items[0]
    if item.seed_id != seed.seed_id or item.work_item_id not in {"", work_item.work_item_id}:
        return None, ("graph_replan_work_item_mismatch",)
    if len(item.evidence_steps) != 1:
        return None, ("graph_replan_requires_one_step",)
    step = item.evidence_steps[0].model_copy(update={"depends_on": ()})
    step, repair_diagnostics = _repair_delta_tool_for_direction(
        step,
        seed=seed,
        max_path_depth=max_path_depth,
        enabled_tools=enabled_tool_set,
    )
    diagnostics.extend(repair_diagnostics)
    allowed = visible_source_symbols if step.tool == "get_file_content" else visible_symbols
    normalized, validation = validate_delta_step(
        step,
        seed=seed,
        reviewer=reviewer,
        allowed_symbols=set(allowed),
        max_path_depth=max_path_depth,
        enabled_tools=enabled_tool_set,
    )
    diagnostics.extend(validation)
    if normalized is None:
        # Do not repair a symbol/permission violation into a different
        # request.  The fallback is only for a provider's executable-step
        # mistake, never for an unknown or unauthorized subject.
        if any(
            diagnostic.startswith(
                ("unknown_subject_ref:", "unknown_tool:", "tool_not_allowed:", "tool_disabled:")
            )
            for diagnostic in validation
        ):
            return None, tuple((*diagnostics, "graph_replan_rejected"))
        fallback, fallback_diagnostics = _fallback_delta_step(
            seed=seed,
            visible_symbols=visible_symbols,
            visible_source_symbols=visible_source_symbols,
            executed_queries=executed_queries,
            max_path_depth=max_path_depth,
            enabled_tools=enabled_tool_set,
            assessment=assessment,
        )
        diagnostics.extend(fallback_diagnostics)
        if fallback is None:
            return None, tuple((*diagnostics, "graph_replan_rejected"))
        normalized, fallback_validation = validate_delta_step(
            fallback,
            seed=seed,
            reviewer=reviewer,
            allowed_symbols=(
                visible_source_symbols
                if fallback.tool == "get_file_content"
                else visible_symbols
            ),
            max_path_depth=max_path_depth,
            enabled_tools=enabled_tool_set,
        )
        diagnostics.extend(fallback_validation)
        if normalized is None:
            return None, tuple((*diagnostics, "graph_replan_rejected"))
    query_key = (
        normalized.tool,
        normalized.subject_ref,
        normalized.path_kind or "",
        normalized.max_depth,
    )
    if query_key in executed_queries:
        fallback, fallback_diagnostics = _fallback_delta_step(
            seed=seed,
            visible_symbols=visible_symbols,
            visible_source_symbols=visible_source_symbols,
            executed_queries=executed_queries,
            max_path_depth=max_path_depth,
            enabled_tools=enabled_tool_set,
            assessment=assessment,
        )
        diagnostics.extend(("graph_replan_duplicate_query", *fallback_diagnostics))
        if fallback is None:
            return None, tuple(diagnostics)
        normalized, fallback_validation = validate_delta_step(
            fallback,
            seed=seed,
            reviewer=reviewer,
            allowed_symbols=(
                visible_source_symbols
                if fallback.tool == "get_file_content"
                else visible_symbols
            ),
            max_path_depth=max_path_depth,
            enabled_tools=enabled_tool_set,
        )
        diagnostics.extend(fallback_validation)
        if normalized is None:
            return None, tuple((*diagnostics, "graph_replan_rejected"))
        query_key = (
            normalized.tool,
            normalized.subject_ref,
            normalized.path_kind or "",
            normalized.max_depth,
        )
        if query_key in executed_queries:
            return None, tuple((*diagnostics, "graph_replan_duplicate_query"))
    diagnostics.append(f"graph_replan_step:{normalized.tool}:{normalized.subject_ref}")
    return normalized, tuple(diagnostics)


def _baseline_graph_plan(
    *,
    reviewer: ReviewerKind,
    task_id: str,
    seeds: tuple[CandidateSeed, ...],
    symbol_context: TaskSymbolContext | None,
    max_path_depth: int,
    enabled_tools: frozenset[str] | set[str] | None,
) -> tuple[ReviewerGraphPlan, tuple[str, ...]]:
    """构造无需 LLM 的最短保底计划。

    GraphPlan 失败不能把 graph-required 候选静默丢掉。保底计划只把
    DirectTriage 已经声明的问题映射到一个最短事实查询；它不猜新的
    symbol，也不扩展查询深度，随后仍经过同一套确定性校验。
    """

    items: list[WorkItem] = []
    allowed_symbols = {
        symbol.symbol_id
        for symbol in (symbol_context.symbols if symbol_context is not None else ())
        if symbol.symbol_id
    }
    diagnostics: list[str] = []
    for seed in seeds:
        question = seed.graph_question
        if question is None:
            continue
        # Baseline construction is deliberately defensive: a malformed
        # provider question must be isolated, not allowed to instantiate an
        # EvidenceStep with an empty/unknown subject and crash the whole
        # review (which would force a full pipeline retry).  The normal
        # validator applies the same boundary; checking before construction
        # keeps the fallback itself total.
        if not question.subject_ref or question.subject_ref not in allowed_symbols:
            diagnostics.append(
                f"{seed.seed_id}:baseline_unknown_subject:{question.subject_ref}"
            )
            continue
        if question.direction == "downstream":
            step = EvidenceStep(
                tool="inspect_path",
                subject_ref=question.subject_ref,
                path_kind=question.path_kind or "behavior",
                max_depth=min(question.max_depth, max_path_depth),
                purpose="验证 DirectTriage 声明的最短下游路径",
                expected_fact=question.question,
            )
        else:
            step = EvidenceStep(
                tool="inspect_change_impact",
                subject_ref=question.subject_ref,
                purpose="验证 DirectTriage 声明的最短上游影响路径",
                expected_fact=question.question,
            )
        items.append(
            WorkItem(
                seed_id=seed.seed_id,
                reviewer=reviewer,
                hypothesis=seed.claim,
                expected_mechanism=seed.mechanism,
                evidence_steps=(step,),
                candidate_criteria="GraphQuestion 的目标/关系和工具响应完整性满足主张",
                rejection_criteria="响应缺少目标、关系或完整性不足以支持主张",
            )
        )
    normalized, validation = validate_graph_plan(
        ReviewerGraphPlan(
            reviewer=reviewer,
            task_id=task_id,
            work_items=tuple(items),
        ),
        reviewer=reviewer,
        task_id=task_id,
        seeds=seeds,
        symbol_context=symbol_context,
        max_path_depth=max_path_depth,
        enabled_tools=enabled_tools,
    )
    return normalized, (*diagnostics, *validation)


def run_graph_plan(
    *,
    reviewer: ReviewerKind,
    task_id: str,
    seeds: tuple[CandidateSeed, ...],
    symbol_context: TaskSymbolContext | None,
    llm: Any,
    max_retries: int,
    structured_method: str,
    max_path_depth: int = 3,
    enabled_tools: list[str] | None = None,
) -> tuple[ReviewerGraphPlan, tuple[str, ...]]:
    """为一个 reviewer/task 执行一次 GraphPlan，非法 WorkItem 不影响其它 reviewer。"""

    empty = ReviewerGraphPlan(reviewer=reviewer, task_id=task_id)
    if not seeds:
        return empty, ()
    enabled_tool_set = set(enabled_tools) if enabled_tools is not None else None

    def baseline(diagnostics: list[str]) -> tuple[ReviewerGraphPlan, tuple[str, ...]]:
        fallback, validation = _baseline_graph_plan(
            reviewer=reviewer,
            task_id=task_id,
            seeds=seeds,
            symbol_context=symbol_context,
            max_path_depth=max_path_depth,
            enabled_tools=enabled_tool_set,
        )
        return fallback, tuple((*diagnostics, "graph_plan_baseline_used", *validation))

    if llm is None:
        return baseline(["graph_plan_llm_unavailable"])
    system = _system_prompt(reviewer)
    user = build_graph_plan_user_prompt(
        reviewer=reviewer,
        task_id=task_id,
        seeds=seeds,
        symbol_context=symbol_context,
        max_path_depth=max_path_depth,
        enabled_tools=enabled_tool_set,
    )
    diagnostics: list[str] = [f"graph_plan_prompt_hash:{prompt_hash(reviewer)}"]
    try:
        raw = invoke_with_retry(
            llm.with_structured_output(LlmReviewerGraphPlan, method=structured_method),
            [("system", system), ("human", user)],
            max_retries=max_retries,
        )
        parsed = (
            ReviewerGraphPlan.model_validate(
                LlmReviewerGraphPlan.model_validate(
                    raw.model_dump() if hasattr(raw, "model_dump") else raw
                ).model_dump()
            )
            if raw is not None
            else None
        )
    except Exception as exc:  # noqa: BLE001
        parsed = None
        diagnostics.append(f"graph_plan_error:{type(exc).__name__}")
    if parsed is None:
        repair_user = (
            f"{user}\n\n上一次 GraphPlan 未通过结构化协议（{diagnostics[-1] if diagnostics else 'missing'}）。"
            "请只修复协议和工具步骤，仍然不得调用工具或增加 WorkItem。"
        )
        try:
            raw = invoke_with_retry(
                llm.with_structured_output(LlmReviewerGraphPlan, method=structured_method),
                [("system", system), ("human", repair_user)],
                max_retries=1,
            )
            parsed = (
                ReviewerGraphPlan.model_validate(
                    LlmReviewerGraphPlan.model_validate(
                        raw.model_dump() if hasattr(raw, "model_dump") else raw
                    ).model_dump()
                )
                if raw is not None
                else None
            )
        except Exception as exc:  # noqa: BLE001
            parsed = None
            diagnostics.append(f"graph_plan_repair_error:{type(exc).__name__}")
        if parsed is None:
            diagnostics.append("graph_plan_failed")
            return baseline(diagnostics)
        diagnostics.append("graph_plan_protocol_repaired")
    if parsed.task_id != task_id:
        diagnostics.append(f"graph_plan_task_mismatch:{parsed.task_id}")
        return baseline(diagnostics)
    if parsed.reviewer is not reviewer:
        diagnostics.append(f"graph_plan_reviewer_mismatch:{parsed.reviewer.value}")
        return baseline(diagnostics)
    normalized, validation = validate_graph_plan(
        parsed,
        reviewer=reviewer,
        task_id=task_id,
        seeds=seeds,
        symbol_context=symbol_context,
        max_path_depth=max_path_depth,
        enabled_tools=enabled_tool_set,
    )
    diagnostics.extend(validation)
    # A provider may return a mixed plan where one WorkItem is valid but
    # another is rejected (for example a dependency self-cycle).  Do not let
    # that partial validation silently erase the rejected seed: add the
    # deterministic one-step baseline for every missing seed and validate the
    # merged plan again so IDs/dependencies remain canonical.
    planned_seed_ids = {item.seed_id for item in normalized.work_items}
    missing_seeds = tuple(seed for seed in seeds if seed.seed_id not in planned_seed_ids)
    if missing_seeds and normalized.work_items:
        baseline_missing, baseline_validation = _baseline_graph_plan(
            reviewer=reviewer,
            task_id=task_id,
            seeds=missing_seeds,
            symbol_context=symbol_context,
            max_path_depth=max_path_depth,
            enabled_tools=enabled_tool_set,
        )
        if baseline_missing.work_items:
            merged, merged_validation = validate_graph_plan(
                ReviewerGraphPlan(
                    reviewer=reviewer,
                    task_id=task_id,
                    work_items=(*normalized.work_items, *baseline_missing.work_items),
                ),
                reviewer=reviewer,
                task_id=task_id,
                seeds=seeds,
                symbol_context=symbol_context,
                max_path_depth=max_path_depth,
                enabled_tools=enabled_tool_set,
            )
            normalized = merged
            diagnostics.extend(
                (
                    f"graph_plan_baseline_for_missing_seed:{seed.seed_id}"
                    for seed in missing_seeds
                )
            )
            diagnostics.extend(baseline_validation)
            diagnostics.extend(merged_validation)
    if not normalized.work_items:
        fallback, fallback_validation = _baseline_graph_plan(
            reviewer=reviewer,
            task_id=task_id,
            seeds=seeds,
            symbol_context=symbol_context,
            max_path_depth=max_path_depth,
            enabled_tools=enabled_tool_set,
        )
        if fallback.work_items:
            diagnostics.extend(("graph_plan_baseline_used", *fallback_validation))
            return fallback, tuple(diagnostics)
    return normalized, tuple(diagnostics)


__all__ = [
    "DOMAIN_TOOL_ALLOWLIST",
    "_baseline_graph_plan",
    "build_graph_plan_user_prompt",
    "build_graph_replan_user_prompt",
    "graph_replan_reason",
    "run_graph_replan",
    "run_graph_plan",
    "prompt_hash",
    "validate_delta_step",
    "validate_graph_plan",
]
