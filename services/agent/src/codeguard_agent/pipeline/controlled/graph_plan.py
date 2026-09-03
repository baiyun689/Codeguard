"""受控模式 GraphPlan：把可疑候选转换为有限证据步骤。"""

from __future__ import annotations

import logging
import hashlib
from pathlib import Path
from typing import Any

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.tasks import (
    CandidateSeed,
    EvidenceStep,
    ReviewerGraphPlan,
    ReviewerKind,
    TaskSymbolContext,
    WorkItem,
)
from codeguard_agent.pipeline.controlled.contracts import get_tool_proof_contract
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
    seed_text = "\n".join(seed.model_dump_json() for seed in seeds)
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
        if len(item.evidence_steps) > 2:
            diagnostics.append(f"too_many_steps:{item.seed_id}")
            continue
        steps: list[EvidenceStep] = []
        invalid = False
        step_names: set[str] = set()
        step_id_map = {
            step.step_id or f"step-{step_index}": f"step-{step_index}"
            for step_index, step in enumerate(item.evidence_steps, start=1)
        }
        for step_index, step in enumerate(item.evidence_steps, start=1):
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
    for seed in seeds:
        question = seed.graph_question
        if question is None:
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
    return validate_graph_plan(
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
            llm.with_structured_output(ReviewerGraphPlan, method=structured_method),
            [("system", system), ("human", user)],
            max_retries=max_retries,
        )
        parsed = ReviewerGraphPlan.model_validate(raw) if raw is not None else None
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
                llm.with_structured_output(ReviewerGraphPlan, method=structured_method),
                [("system", system), ("human", repair_user)],
                max_retries=1,
            )
            parsed = ReviewerGraphPlan.model_validate(raw) if raw is not None else None
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
    "run_graph_plan",
    "prompt_hash",
    "validate_delta_step",
    "validate_graph_plan",
]
