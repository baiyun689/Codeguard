"""面向子任务 React 的 GraphPlan。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
import json

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.tasks import (
    EvidenceNeed,
    InvestigationSeed,
    ReviewerKind,
    SubtaskInstruction,
    SubtaskPlan,
    TaskSymbolContext,
)
from codeguard_agent.pipeline.controlled.graph_plan import DOMAIN_TOOL_ALLOWLIST
from codeguard_agent.pipeline.controlled.llm_contracts import LlmSubtaskPlan

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
        "不要输出候选；每个 seed 至少给出一个 bounded SubtaskInstruction。"
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
    seed_by_id = {seed.seed_id: seed for seed in seeds if seed.seed_id}
    if not seeds:
        return SubtaskPlan(reviewer=reviewer, task_id=task.id, subtasks=()), tuple(diagnostics)
    if llm is None:
        return _fallback_plan(reviewer, task.id, seeds, max_tool_calls, max_rounds, max_subtasks, max_path_depth, enabled_tools, diagnostics + ["subtask_plan_llm_unavailable"])
    prompt = build_subtask_plan_user_prompt(
        reviewer=reviewer,
        task=task,
        seeds=seeds,
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
        return _fallback_plan(reviewer, task.id, seeds, max_tool_calls, max_rounds, max_subtasks, max_path_depth, enabled_tools, diagnostics)
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
        return _fallback_plan(reviewer, task.id, seeds, max_tool_calls, max_rounds, max_subtasks, max_path_depth, enabled_tools, diagnostics)
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
        if item.seed_id in seen_seed:
            diagnostics.append(f"subtask_duplicate_seed:{item.seed_id}")
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
        tools = tuple(dict.fromkeys(item.allowed_tools))
        domain = DOMAIN_TOOL_ALLOWLIST[reviewer]
        seed_tools = set(seed.allowed_tools)
        if enabled is not None:
            tools = tuple(tool for tool in tools if tool in enabled)
        tools = tuple(tool for tool in tools if tool in domain)
        if seed_tools:
            tools = tuple(tool for tool in tools if tool in seed_tools)
        directional_tools = {
            EvidenceNeed.INSPECT_PATH: {"inspect_path", "get_file_content"},
            EvidenceNeed.INSPECT_CHANGE_IMPACT: {
                "inspect_change_impact", "get_file_content",
            },
            EvidenceNeed.INSPECT_STRUCTURE: {
                "inspect_structure", "get_file_content",
            },
        }.get(seed.evidence_need)
        if directional_tools is not None:
            tools = tuple(tool for tool in tools if tool in directional_tools)
        # ``inspect_structure`` returns one-hop relationships and declarations,
        # not the implementation body.  Keep the source reader available for
        # parent-method/field-initialization questions instead of allowing a
        # structure-only subtask to claim behavior it cannot observe.
        if (
            seed.evidence_need is EvidenceNeed.INSPECT_STRUCTURE
            and "inspect_structure" in tools
            and "get_file_content" not in tools
            and "get_file_content" in domain
            and (enabled is None or "get_file_content" in enabled)
            and len(tools) < 3
        ):
            tools = (*tools, "get_file_content")
        tools = tuple(dict.fromkeys(tools))[:3]
        if not tools:
            diagnostics.append(f"subtask_no_allowed_tool:{item.seed_id}")
            continue
        primary = item.primary_tool if item.primary_tool in tools else _default_tool(seed, tools)
        depth = min(item.max_tool_calls, max_tool_calls)
        rounds = min(item.max_rounds, max_rounds)
        if depth < 0 or rounds < 1:
            continue
        valid.append(
            item.model_copy(
                update={
                    "subtask_id": f"subtask-{reviewer.value}-{task_id}-{len(valid)+1}",
                    "reviewer": reviewer,
                    "allowed_tools": tools,
                    "initial_symbol_ids": decoded_symbol_ids,
                    "primary_tool": primary,
                    "max_tool_calls": depth,
                    "max_rounds": rounds,
                }
            )
        )
        seen_seed.add(item.seed_id)
        if len(valid) >= max_subtasks:
            diagnostics.append("subtask_plan_task_limit")
            break
    missing = [seed for seed in seeds.values() if seed.seed_id not in seen_seed]
    if missing and len(valid) < max_subtasks:
        diagnostics.append(f"subtask_plan_missing_seeds:{len(missing)}")
    return SubtaskPlan(reviewer=reviewer, task_id=task_id, subtasks=tuple(valid))


def _default_tool(seed: InvestigationSeed, tools: tuple[str, ...]) -> str:
    requested = {
        EvidenceNeed.INSPECT_PATH: "inspect_path",
        EvidenceNeed.INSPECT_CHANGE_IMPACT: "inspect_change_impact",
        EvidenceNeed.INSPECT_STRUCTURE: "inspect_structure",
    }.get(seed.evidence_need)
    return requested if requested in tools else tools[0]


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
    enabled = set(enabled_tools) if enabled_tools is not None else set(DOMAIN_TOOL_ALLOWLIST[reviewer])
    items: list[SubtaskInstruction] = []
    for seed in seeds[:max_subtasks]:
        tools = tuple(
            tool
            for tool in (
                seed.allowed_tools
                or tuple(DOMAIN_TOOL_ALLOWLIST[reviewer])
            )
            if tool in enabled
        )
        directional_tools = {
            EvidenceNeed.INSPECT_PATH: {"inspect_path", "get_file_content"},
            EvidenceNeed.INSPECT_CHANGE_IMPACT: {
                "inspect_change_impact", "get_file_content",
            },
            EvidenceNeed.INSPECT_STRUCTURE: {
                "inspect_structure", "get_file_content",
            },
        }.get(seed.evidence_need)
        if directional_tools is not None:
            tools = tuple(tool for tool in tools if tool in directional_tools)
        if (
            seed.evidence_need is EvidenceNeed.INSPECT_STRUCTURE
            and "inspect_structure" in tools
            and "get_file_content" not in tools
            and "get_file_content" in enabled
            and len(tools) < 3
        ):
            tools = (*tools, "get_file_content")
        tools = tuple(dict.fromkeys(tools))[:3]
        if not tools:
            continue
        items.append(SubtaskInstruction(
            subtask_id=f"subtask-{reviewer.value}-{task_id}-{len(items)+1}",
            seed_id=seed.seed_id,
            reviewer=reviewer,
            change_unit_id=seed.change_unit_id,
            objective=seed.investigation_question,
            observed_change=seed.observed_change,
            initial_symbol_ids=seed.initial_symbol_ids,
            allowed_tools=tools,
            primary_tool=_default_tool(seed, tools),
            required_facts=(seed.investigation_question,),
            stop_conditions=("事实已直接支持或反驳调查问题", "工具结果 partial 且预算耗尽时 inconclusive"),
            max_tool_calls=min(max_tool_calls, 4),
            max_rounds=min(max_rounds, 4),
        ))
    return SubtaskPlan(reviewer=reviewer, task_id=task_id, subtasks=tuple(items)), tuple(diagnostics + ["subtask_plan_deterministic_fallback"])


__all__ = ["build_subtask_plan_user_prompt", "run_subtask_plan"]
