"""面向子任务 React 的 GraphPlan。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
import json

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.tasks import (
    InvestigationSeed,
    ReviewerKind,
    SubtaskInstruction,
    SubtaskPlan,
    TaskSymbolContext,
)
from codeguard_agent.pipeline.controlled.graph_plan import DOMAIN_TOOL_ALLOWLIST
from codeguard_agent.pipeline.controlled.llm_contracts import LlmSubtaskPlan
from codeguard_agent.pipeline.controlled.subtask_capabilities import (
    coherent_tool_bundle,
    normalize_investigation_seed,
    required_graph_tool,
)

_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts" / "controlled"
_DOMAIN_PROMPTS = {
    ReviewerKind.BEHAVIOR: "graph-plan-behavior.txt",
    ReviewerKind.THREAT_MODEL: "graph-plan-threat.txt",
    ReviewerKind.MAINTAINABILITY: "graph-plan-maintainability.txt",
}


def _system_prompt(reviewer: ReviewerKind) -> str:
    common = (_PROMPT_DIR / "graph-plan-subtask-common.txt").read_text(encoding="utf-8")
    domain = (_PROMPT_DIR / _DOMAIN_PROMPTS[reviewer]).read_text(encoding="utf-8")
    return f"{common.strip()}\n\n{domain.strip()}"


def prompt_hash(reviewer: ReviewerKind) -> str:
    return hashlib.sha256(_system_prompt(reviewer).encode("utf-8")).hexdigest()[:16]


def build_subtask_plan_user_prompt(
    *,
    reviewer: ReviewerKind,
    task: Any,
    seeds: tuple[InvestigationSeed, ...],
    symbol_context: TaskSymbolContext | None,
    max_tool_calls: int,
    max_rounds: int,
    max_subtasks: int,
    enabled_tools: frozenset[str] | set[str] | None = None,
) -> str:
    ordered_symbols = tuple(sorted(
        (symbol_context.symbols if symbol_context else ()),
        key=lambda symbol: symbol.symbol_id,
    ))
    aliases = {
        symbol.symbol_id: f"S{index:02d}"
        for index, symbol in enumerate(ordered_symbols, start=1)
        if symbol.symbol_id
    }

    def render_symbol(symbol: Any) -> str:
        payload = symbol.model_dump()
        payload["symbol_id"] = aliases.get(payload.get("symbol_id", ""), "UNAVAILABLE")
        if payload.get("owner_id"):
            payload["owner_id"] = aliases.get(payload["owner_id"], "UNAVAILABLE")
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def render_seed(seed: InvestigationSeed) -> str:
        payload = seed.model_dump(exclude_defaults=True)
        payload["initial_symbol_ids"] = [
            aliases.get(symbol_id, "UNAVAILABLE")
            for symbol_id in seed.initial_symbol_ids
        ]
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    symbols = "\n".join(
        render_symbol(symbol) for symbol in ordered_symbols
    ) or "(无已解析 symbol；只能生成无法执行的限制说明)"
    seed_text = "\n".join(render_seed(seed) for seed in seeds)
    allowed = ", ".join(sorted(enabled_tools)) if enabled_tools is not None else "全部已注册工具"
    return (
        f'<subtask_plan reviewer="{reviewer.value}" task_id="{task.id}" '
        f'max_subtasks="{max_subtasks}" max_tool_calls="{max_tool_calls}" max_rounds="{max_rounds}">\n'
        f"<task_patch file=\"{task.file}\">\n{task.patch}\n</task_patch>\n"
        f"<symbol_context>\n{symbols}\n</symbol_context>\n"
        f"<investigation_seeds>\n{seed_text}\n</investigation_seeds>\n"
        f"允许工具：{allowed}\n"
        "不要输出候选；每个 seed 恰好给出一个 bounded SubtaskInstruction。"
    )


def run_subtask_plan(
    *,
    reviewer: ReviewerKind,
    task: Any,
    seeds: tuple[InvestigationSeed, ...],
    symbol_context: TaskSymbolContext | None,
    llm: Any,
    max_retries: int,
    structured_method: str,
    max_tool_calls: int,
    max_rounds: int,
    max_subtasks: int,
    max_path_depth: int,
    enabled_tools: frozenset[str] | set[str] | None = None,
) -> tuple[SubtaskPlan, tuple[str, ...]]:
    diagnostics: list[str] = [f"subtask_plan_prompt_hash:{prompt_hash(reviewer)}"]
    normalized_seeds = tuple(normalize_investigation_seed(seed) for seed in seeds)
    seed_by_id = {seed.seed_id: seed for seed in normalized_seeds if seed.seed_id}
    if not normalized_seeds:
        return SubtaskPlan(reviewer=reviewer, task_id=task.id, subtasks=()), tuple(diagnostics)
    if llm is None:
        return _fallback_plan(reviewer, task.id, normalized_seeds, max_tool_calls, max_rounds, max_subtasks, max_path_depth, enabled_tools, diagnostics + ["subtask_plan_llm_unavailable"])
    prompt = build_subtask_plan_user_prompt(
        reviewer=reviewer,
        task=task,
        seeds=normalized_seeds,
        symbol_context=symbol_context,
        max_tool_calls=max_tool_calls,
        max_rounds=max_rounds,
        max_subtasks=max_subtasks,
        enabled_tools=enabled_tools,
    )
    try:
        raw = invoke_with_retry(
            llm.with_structured_output(LlmSubtaskPlan, method=structured_method),
            [("system", _system_prompt(reviewer)), ("human", prompt)],
            max_retries=max_retries,
        )
        payload = raw.model_dump() if hasattr(raw, "model_dump") else raw
        parsed = LlmSubtaskPlan.model_validate(payload)
        plan = SubtaskPlan.model_validate(parsed.model_dump())
    except Exception as exc:  # noqa: BLE001
        diagnostics.append(f"subtask_plan_schema_failed:{type(exc).__name__}")
        return _fallback_plan(reviewer, task.id, normalized_seeds, max_tool_calls, max_rounds, max_subtasks, max_path_depth, enabled_tools, diagnostics)
    normalized = _validate_plan(
        plan,
        reviewer=reviewer,
        task_id=task.id,
        seeds=seed_by_id,
        symbol_context=symbol_context,
        max_tool_calls=max_tool_calls,
        max_rounds=max_rounds,
        max_subtasks=max_subtasks,
        max_path_depth=max_path_depth,
        enabled_tools=enabled_tools,
        diagnostics=diagnostics,
    )
    if not normalized.subtasks:
        diagnostics.append("subtask_plan_empty_fallback")
        return _fallback_plan(reviewer, task.id, normalized_seeds, max_tool_calls, max_rounds, max_subtasks, max_path_depth, enabled_tools, diagnostics)
    return normalized, tuple(diagnostics)


def _validate_plan(
    plan: SubtaskPlan,
    *,
    reviewer: ReviewerKind,
    task_id: str,
    seeds: dict[str, InvestigationSeed],
    symbol_context: TaskSymbolContext | None,
    max_tool_calls: int,
    max_rounds: int,
    max_subtasks: int,
    max_path_depth: int,
    enabled_tools: frozenset[str] | set[str] | None,
    diagnostics: list[str],
) -> SubtaskPlan:
    allowed_ids = {symbol.symbol_id for symbol in (symbol_context.symbols if symbol_context else ())}
    ordered_symbols = tuple(sorted(
        (symbol_context.symbols if symbol_context else ()),
        key=lambda symbol: symbol.symbol_id,
    ))
    alias_to_raw = {
        f"S{index:02d}": symbol.symbol_id
        for index, symbol in enumerate(ordered_symbols, start=1)
        if symbol.symbol_id
    }
    enabled = set(enabled_tools) if enabled_tools is not None else None
    valid: list[SubtaskInstruction] = []
    seen_seed: set[str] = set()
    for item in plan.subtasks:
        seed = seeds.get(item.seed_id)
        if seed is None or item.reviewer is not reviewer:
            diagnostics.append(f"subtask_rejected_seed_or_reviewer:{item.seed_id}")
            continue
        decoded_symbol_ids = tuple(
            alias_to_raw.get(symbol_ref, "")
            for symbol_ref in item.initial_symbol_ids
        )
        if (
            not item.initial_symbol_ids
            or not all(decoded_symbol_ids)
            or not set(decoded_symbol_ids).issubset(allowed_ids)
        ):
            diagnostics.append(f"subtask_unknown_symbol:{item.seed_id}")
            continue
        domain = DOMAIN_TOOL_ALLOWLIST[reviewer]
        tools = coherent_tool_bundle(
            seed,
            item.allowed_tools,
            domain_tools=domain,
            enabled_tools=enabled,
            max_tools=4,
        )
        if not tools:
            diagnostics.append(f"subtask_no_allowed_tool:{item.seed_id}")
            continue
        primary = item.primary_tool if item.primary_tool in tools else _default_tool(seed, tools)
        depth = min(item.max_tool_calls, max_tool_calls)
        rounds = min(item.max_rounds, max_rounds)
        if depth < 0 or rounds < 1:
            continue
        normalized_item = item.model_copy(
            update={
                "subtask_id": f"subtask-{reviewer.value}-{task_id}-{len(valid)+1}",
                "reviewer": reviewer,
                "allowed_tools": tools,
                "initial_symbol_ids": decoded_symbol_ids,
                "primary_tool": primary,
                "path_kind": seed.path_kind,
                "direction": seed.direction,
                "max_tool_calls": depth,
                "max_rounds": rounds,
            }
        )
        # Providers occasionally emit a second instruction for the same seed
        # just to request a different tool.  Keep one continuous React and
        # merge the capabilities/facts instead of creating disconnected
        # graph-only and source-only subtasks.
        duplicate_index = next(
            (
                index
                for index, existing in enumerate(valid)
                if existing.seed_id == normalized_item.seed_id
            ),
            None,
        )
        if duplicate_index is not None:
            existing = valid[duplicate_index]
            merged_tools = tuple(dict.fromkeys(
                (*existing.allowed_tools, *normalized_item.allowed_tools)
            ))[:4]
            merged = existing.model_copy(update={
                "allowed_tools": merged_tools,
                "initial_symbol_ids": tuple(dict.fromkeys(
                    (*existing.initial_symbol_ids, *normalized_item.initial_symbol_ids)
                ))[:4],
                "objective": _merge_instruction_text(
                    existing.objective, normalized_item.objective
                ),
                "required_facts": tuple(dict.fromkeys(
                    (*existing.required_facts, *normalized_item.required_facts)
                ))[:6],
                "stop_conditions": tuple(dict.fromkeys(
                    (*existing.stop_conditions, *normalized_item.stop_conditions)
                ))[:6],
                "max_tool_calls": max(
                    existing.max_tool_calls, normalized_item.max_tool_calls
                ),
                "max_rounds": max(existing.max_rounds, normalized_item.max_rounds),
                "path_kind": existing.path_kind or normalized_item.path_kind,
                "direction": existing.direction or normalized_item.direction,
                "primary_tool": (
                    existing.primary_tool
                    if existing.primary_tool in merged_tools
                    else _default_tool(seed, merged_tools)
                ),
            })
            valid[duplicate_index] = merged
            diagnostics.append(f"subtask_duplicate_seed_merged:{item.seed_id}")
        else:
            if len(valid) >= max_subtasks:
                diagnostics.append("subtask_plan_task_limit")
                seen_seed.add(item.seed_id)
                continue
            valid.append(normalized_item)
        seen_seed.add(item.seed_id)
    missing = [seed for seed in seeds.values() if seed.seed_id not in seen_seed]
    if missing and len(valid) < max_subtasks:
        missing_count = len(missing)
        repaired_count = 0
        for seed in missing:
            fallback = _fallback_instruction(
                reviewer,
                task_id,
                seed,
                max_tool_calls=max_tool_calls,
                max_rounds=max_rounds,
                enabled_tools=enabled_tools,
                subtask_index=len(valid) + 1,
            )
            if fallback is None:
                continue
            valid.append(fallback)
            repaired_count += 1
            seen_seed.add(seed.seed_id)
            if len(valid) >= max_subtasks:
                break
        if repaired_count:
            diagnostics.append(
                f"subtask_plan_missing_seeds_repaired:{repaired_count}"
            )
        if repaired_count < missing_count:
            diagnostics.append(
                f"subtask_plan_missing_seeds:{missing_count - repaired_count}"
            )
    elif missing:
        # The provider returned more seeds than the configured executable
        # capacity.  Keep this as an unresolved diagnostic; unlike the branch
        # above no deterministic fallback instruction was installed.
        diagnostics.append(f"subtask_plan_missing_seeds:{len(missing)}")
    return SubtaskPlan(reviewer=reviewer, task_id=task_id, subtasks=tuple(valid))


def _merge_instruction_text(left: str, right: str, *, limit: int = 420) -> str:
    """Combine duplicate provider instructions without changing the objective."""

    values: list[str] = []
    for value in (left, right):
        text = " ".join(str(value).split()).strip()
        if text and text not in values:
            values.append(text)
    return "；".join(values)[:limit]


def _default_tool(seed: InvestigationSeed, tools: tuple[str, ...]) -> str:
    requested = required_graph_tool(seed)
    return requested if requested in tools else tools[0]


def _fallback_instruction(
    reviewer: ReviewerKind,
    task_id: str,
    seed: InvestigationSeed,
    *,
    max_tool_calls: int,
    max_rounds: int,
    enabled_tools: frozenset[str] | set[str] | None,
    subtask_index: int,
) -> SubtaskInstruction | None:
    enabled = set(enabled_tools) if enabled_tools is not None else set(
        DOMAIN_TOOL_ALLOWLIST[reviewer]
    )
    tools = coherent_tool_bundle(
        seed,
        seed.allowed_tools or tuple(DOMAIN_TOOL_ALLOWLIST[reviewer]),
        domain_tools=DOMAIN_TOOL_ALLOWLIST[reviewer],
        enabled_tools=enabled,
        max_tools=4,
    )
    if not tools:
        return None
    return SubtaskInstruction(
        subtask_id=f"subtask-{reviewer.value}-{task_id}-{subtask_index}",
        seed_id=seed.seed_id,
        reviewer=reviewer,
        change_unit_id=seed.change_unit_id,
        objective=seed.investigation_question,
        observed_change=seed.observed_change,
        initial_symbol_ids=seed.initial_symbol_ids,
        allowed_tools=tools,
        primary_tool=_default_tool(seed, tools),
        path_kind=seed.path_kind,
        direction=seed.direction,
        required_facts=(seed.investigation_question,),
        stop_conditions=(
            "事实已直接支持或反驳调查问题",
            "工具结果 partial 且预算耗尽时 inconclusive",
        ),
        max_tool_calls=min(max_tool_calls, 20),
        max_rounds=min(max_rounds, 12),
    )


def _fallback_plan(
    reviewer: ReviewerKind,
    task_id: str,
    seeds: tuple[InvestigationSeed, ...],
    max_tool_calls: int,
    max_rounds: int,
    max_subtasks: int,
    max_path_depth: int,
    enabled_tools: frozenset[str] | set[str] | None,
    diagnostics: list[str],
) -> tuple[SubtaskPlan, tuple[str, ...]]:
    items: list[SubtaskInstruction] = []
    for seed in seeds[:max_subtasks]:
        item = _fallback_instruction(
            reviewer,
            task_id,
            normalize_investigation_seed(seed),
            max_tool_calls=max_tool_calls,
            max_rounds=max_rounds,
            enabled_tools=enabled_tools,
            subtask_index=len(items) + 1,
        )
        if item is None:
            continue
        items.append(item)
    return SubtaskPlan(reviewer=reviewer, task_id=task_id, subtasks=tuple(items)), tuple(diagnostics + ["subtask_plan_deterministic_fallback"])


__all__ = ["build_subtask_plan_user_prompt", "run_subtask_plan"]
