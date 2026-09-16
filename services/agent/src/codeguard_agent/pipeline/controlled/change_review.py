"""按变更声明分组，预取有界源码与关系上下文，并执行统一审查。

方法、类型和构造器分别调查；同一文件任务中的字段合并调查。
分组由程序确定，源码与图谱事实通过 Java 工具服务读取。
"""

from pathlib import Path
from hashlib import sha256
import json
import re
from typing import Any, TypedDict
from codeguard_agent.models.council import CandidateIssue, CouncilTrace
from codeguard_agent.models.state import collect_candidate_reducer
from codeguard_agent.models.tasks import SubtaskInstruction, SubtaskPlan
from codeguard_agent.pipeline.controlled.assessment import collapse_candidate_duplicates
from codeguard_agent.pipeline.controlled.subtask_react import SubtaskReactEngine
from codeguard_agent.pipeline.evidence.ledger import (
    EvidenceCatalogBuilder,
    capture_tool_records,
)
from codeguard_agent.pipeline.evidence.projection import graph_projection_focus
from codeguard_agent.pipeline.execution.concurrency import run_bounded_parallel
from codeguard_agent.pipeline.execution.discovery import (
    CoordinatedDiscoveryToolClient,
    DiscoveryToolCoordinator,
)

_PROMPTS = Path(__file__).resolve().parents[2] / "prompts" / "controlled"


class SubtaskPromptContext(TypedDict):
    """调查组的提示词视图；证据账本仍保存原始任务内容。"""

    patch: str
    primary_change_lines: tuple[int, ...]
    deletion_anchors: tuple[Any, ...]
    other_changes_index: tuple[dict[str, Any], ...]
    scope_kind: str


def prepare_change_context(client, group, instruction, scoped_context=None) -> str:
    """在工具预算内预取源码首页与一跳关系。

    先读取根符号源码，再依次查询各根符号的入向和出向关系。
    预取不自动扩展返回符号或追踪分页游标，与后续探索共用预算。
    """
    budget = instruction.max_tool_calls
    reserve = min(budget, max(2, budget // 3))
    preparation_limit = min(6, budget - reserve)
    reserve = budget - preparation_limit
    prepared = []
    attempted = []
    primary_lines = set((scoped_context or {}).get("primary_change_lines", ()))
    primary_lines.update(
        anchor.anchor_line
        for anchor in (scoped_context or {}).get("deletion_anchors", ())
    )
    if "read_symbol" in instruction.allowed_tools:
        for symbol in group[: min(budget // 2, preparation_limit)]:
            symbol_lines = [
                line
                for line in primary_lines
                if symbol.start_line <= line <= symbol.end_line
            ]
            if symbol_lines:
                start_line = max(symbol.start_line, min(symbol_lines) - 4)
                end_line = min(symbol.end_line, start_line + 119)
            else:
                start_line = symbol.start_line
                end_line = min(symbol.end_line, symbol.start_line + 119)
            response = client.read_symbol(
                symbol.symbol_id,
                start_line=start_line,
                end_line=end_line,
            )
            prepared.append(str(response.result or response.error))
            attempted.append({"symbol_id": symbol.symbol_id, "tool": "read_symbol"})
    relation_types = {
        "METHOD": ("callers", "callees", "entrypoints", "type_references"),
        "CONSTRUCTOR": ("callers", "callees", "entrypoints", "type_references"),
        "FIELD": ("field_readers", "field_writers"),
        "TYPE": (
            "implementations",
            "parents",
            "children",
            "type_users",
            "type_references",
        ),
    }
    scheduled = [
        (symbol.symbol_id, relations[index])
        for index in range(2)
        for symbol in group
        if index < len((relations := relation_types.get(symbol.kind.upper(), ())))
        and relations[index] in instruction.allowed_relations
    ]
    remaining = scheduled
    if "query_relations" in instruction.allowed_tools:
        count = min(len(scheduled), max(0, preparation_limit - len(attempted)))
        for symbol_id, relation in scheduled[:count]:
            response = client.query_relations(
                symbol_id,
                relation,
                depth=1,
                limit=6,
                include_callsite=True,
                include_context=True,
            )
            prepared.append(str(response.result or response.error))
            attempted.append(
                {
                    "symbol_id": symbol_id,
                    "tool": "query_relations",
                    "relation": relation,
                }
            )
        remaining = scheduled[count:]
    manifest = {
        "attempted": attempted,
        "unqueried_relations": remaining,
        "reserved_tool_attempts": reserve,
        "coverage_note": "First pages only; inspect each response coverage/limitations/next_cursor. Unqueried is not absent.",
    }
    return (
        "<preparation_scope>"
        + json.dumps(manifest, ensure_ascii=False)
        + "</preparation_scope>\n"
        + "\n\n".join(prepared)
    )


def review_group_id(task_id: str, index: int) -> str:
    """生成跨文件任务唯一的调查组标识，用于关联工具轨迹。"""
    return f"change-{sha256(task_id.encode('utf-8')).hexdigest()[:12]}-{index + 1}"


def change_groups(task, context) -> list[tuple]:
    """根据变更声明生成确定性的调查分组。

    非字段声明各自成组，同一文件任务中的字段声明共用一组。
    分组顺序按照声明在源码中首次出现的位置排列。
    """
    symbols = list(context.symbols) if context else []
    selected = {}
    blank_lines = set()
    line_number = 0
    for raw in task.patch.splitlines():
        hunk = re.match("@@ -\\d+(?:,\\d+)? \\+(\\d+)", raw)
        if hunk:
            line_number = int(hunk[1])
        elif raw.startswith("+") and (not raw.startswith("+++")):
            if not raw[1:].strip():
                blank_lines.add(line_number)
            line_number += 1
        elif raw.startswith(" "):
            line_number += 1
    anchors = {a.anchor_line for a in task.deletion_anchors}
    for line in task.resolution_lines:
        if line in blank_lines and line not in anchors:
            continue
        symbol = _symbol_for_change_line(symbols, task, line)
        if symbol is not None:
            selected[symbol.symbol_id] = symbol
    ordered = sorted(selected.values(), key=lambda s: (s.start_line, s.symbol_id))
    groups: list[tuple] = []
    fields: list[Any] = []
    field_group_index: int | None = None
    for symbol in ordered:
        if symbol.kind.upper() == "FIELD":
            if field_group_index is None:
                field_group_index = len(groups)
                groups.append(())
            fields.append(symbol)
        else:
            groups.append((symbol,))
    if field_group_index is not None:
        groups[field_group_index] = tuple(fields)
    if groups and _unresolved_change_lines(task, context):
        # 将未解析的变更位置单独保留为未完成的审查范围。
        groups.append(())
    return groups or [()]


def _symbol_for_change_line(symbols, task, line: int):
    """返回包含变更行的最内层已解析声明。"""
    enclosing = [
        symbol
        for symbol in symbols
        if symbol.file == task.file
        and symbol.start_line <= line <= symbol.end_line
    ]
    return min(
        enclosing,
        key=lambda symbol: (symbol.end_line - symbol.start_line, symbol.symbol_id),
        default=None,
    )


def _unresolved_change_lines(task, context) -> tuple[int, ...]:
    """返回没有解析出所属声明的非空变更行。"""
    if context is None or not getattr(context, "symbols", ()):
        return ()
    blank = _blank_added_lines(task)
    anchors = {anchor.anchor_line for anchor in task.deletion_anchors}
    symbols = tuple(context.symbols)
    return tuple(
        sorted(
            line
            for line in task.resolution_lines
            if not (line in blank and line not in anchors)
            and _symbol_for_change_line(symbols, task, line) is None
        )
    )


def _blank_added_lines(task) -> set[int]:
    """找出空白新增行，将其排除在主要定位锚点之外。"""
    blank: set[int] = set()
    line_number = 0
    for raw in task.patch.splitlines():
        hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", raw)
        if hunk:
            line_number = int(hunk[1])
        elif raw.startswith("+") and not raw.startswith("+++"):
            if not raw[1:].strip():
                blank.add(line_number)
            line_number += 1
        elif raw.startswith(" "):
            line_number += 1
    return blank


def _group_change_lines(task, context, group) -> tuple[set[int], set[int]]:
    """返回本组声明对应的新增行和删除锚点。"""
    if not group:
        return set(task.changed_lines), {
            anchor.anchor_line for anchor in task.deletion_anchors
        }
    symbols = tuple(getattr(context, "symbols", ()) or ())
    group_ids = {symbol.symbol_id for symbol in group}
    blank = _blank_added_lines(task)
    anchor_lines = {
        anchor.anchor_line for anchor in task.deletion_anchors
    }
    primary: set[int] = set()
    for line in task.resolution_lines:
        if line in blank and line not in anchor_lines:
            continue
        owner = _symbol_for_change_line(symbols, task, line)
        if owner is not None and owner.symbol_id in group_ids:
            if line in task.changed_lines:
                primary.add(line)
    anchors = {
        line
        for line in anchor_lines
        if (
            (owner := _symbol_for_change_line(symbols, task, line)) is not None
            and owner.symbol_id in group_ids
        )
    }
    return primary, anchors


def _other_changes_index(
    task, context, group, *, include_all: bool = False
) -> tuple[dict[str, Any], ...]:
    """为其他变更生成简要导航索引，不展开其 diff 正文。"""
    symbols = tuple(getattr(context, "symbols", ()) or ())
    group_ids = {symbol.symbol_id for symbol in group}
    if not symbols or (not group and not include_all):
        return ()
    index: list[dict[str, Any]] = []
    for symbol in sorted(symbols, key=lambda item: (item.start_line, item.symbol_id)):
        if symbol.symbol_id in group_ids:
            continue
        primary, anchors = _group_change_lines(task, context, (symbol,))
        lines = sorted(primary | anchors)
        if not lines:
            continue
        index.append(
            {
                "symbol_id": symbol.symbol_id,
                "kind": symbol.kind,
                "range": [symbol.start_line, symbol.end_line],
                "changed_lines": lines,
            }
        )
    return tuple(index)


def _scoped_patch(task, primary_lines: set[int], anchor_lines: set[int]) -> str:
    """在提示词中隐藏属于其他声明的变更行。

    该视图只影响模型输入；账本中的原始 patch、证据摘要及候选定位依据保持不变。
    """
    if not primary_lines and not anchor_lines:
        return task.patch
    rendered: list[str] = []
    new_line: int | None = None
    previous_surviving: int | None = None
    pending_deletions: list[str] = []

    def flush_deletions(next_line: int | None = None) -> None:
        nonlocal pending_deletions
        if not pending_deletions:
            return
        anchor = next_line if next_line is not None else previous_surviving
        if anchor is not None and anchor in anchor_lines:
            rendered.extend(pending_deletions)
        else:
            rendered.append("~ [other deletion omitted from this subtask]")
        pending_deletions = []

    for raw in task.patch.splitlines():
        hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", raw)
        if hunk:
            flush_deletions()
            new_line = int(hunk[1])
            previous_surviving = None
            rendered.append(raw)
            continue
        if raw.startswith("---") or raw.startswith("+++"):
            flush_deletions()
            rendered.append(raw)
            continue
        if raw.startswith("-"):
            pending_deletions.append(raw)
            continue
        if raw.startswith("+"):
            flush_deletions(new_line)
            visible = new_line is None or new_line in primary_lines
            rendered.append(raw if visible else "~ [other change omitted from this subtask]")
            if new_line is not None:
                previous_surviving = new_line
                new_line += 1
            continue
        if raw.startswith(" "):
            flush_deletions(new_line)
            rendered.append(raw)
            if new_line is not None:
                previous_surviving = new_line
                new_line += 1
            continue
        flush_deletions()
        rendered.append(raw)
    flush_deletions()
    return "\n".join(rendered)


def build_subtask_context(
    task, context, group, *, unresolved_lines: tuple[int, ...] | None = None
) -> SubtaskPromptContext:
    """构建单个调查组的提示词上下文范围。"""
    if unresolved_lines is not None:
        primary_lines = set(unresolved_lines) & set(task.changed_lines)
        anchor_lines = {
            anchor.anchor_line
            for anchor in task.deletion_anchors
            if anchor.anchor_line in unresolved_lines
        }
        scope_kind = "unresolved"
    else:
        primary_lines, anchor_lines = _group_change_lines(task, context, group)
        scope_kind = "resolved" if group else "unresolved"
    return {
        "patch": _scoped_patch(task, primary_lines, anchor_lines),
        "primary_change_lines": tuple(sorted(primary_lines)),
        "deletion_anchors": tuple(
            anchor
            for anchor in getattr(task, "deletion_anchors", ())
            if anchor.anchor_line in anchor_lines
        ),
        "other_changes_index": _other_changes_index(
            task, context, group, include_all=unresolved_lines is not None
        ),
        "scope_kind": scope_kind,
    }


def _group_projection_focus(task, context, group, scoped_context):
    """将图谱投影的相关性范围限定为当前声明组。"""
    focused_task = task.model_copy(
        update={
            "changed_lines": list(scoped_context.get("primary_change_lines", ())),
            "deletion_anchors": list(scoped_context.get("deletion_anchors", ())),
        }
    )
    focused_context = (
        context.model_copy(update={"symbols": tuple(group)})
        if context is not None and group
        else None
    )
    return graph_projection_focus(focused_task, focused_context)


def _candidate_in_group(
    candidate, task, scoped_context: SubtaskPromptContext, group
) -> bool:
    """检查候选是否定位于当前调查组的变更范围。"""
    if not group and scoped_context.get("scope_kind") != "unresolved":
        return True
    if str(candidate.file).replace("\\", "/") != str(task.file).replace("\\", "/"):
        return False
    line = int(candidate.line or 0)
    if line == 0:
        # 定位失败的候选保留为文件级问题，并将位置限制交给裁决阶段处理。
        return True
    allowed = set(scoped_context.get("primary_change_lines", ()))
    allowed.update(
        anchor.anchor_line
        for anchor in scoped_context.get("deletion_anchors", ())
    )
    return line in allowed


def build_change_review_node(
    llm,
    tool_client,
    *,
    candidate_factory,
    scope_factory,
    allocate_budgets,
    execute_concurrency=3,
    max_tool_calls=10,
    max_rounds=6,
    timeout_seconds=120,
    task_tool_budget=32,
    max_subtasks=24,
):
    max_tool_calls = min(20, max(0, max_tool_calls))
    max_rounds = min(12, max(1, max_rounds))
    max_subtasks = min(24, max(0, max_subtasks))
    task_tool_budget = max(0, task_tool_budget)

    def node(state) -> dict[str, Any]:
        candidates: list[CandidateIssue] = []
        records, traces = ([], [])
        artifacts, plans, results, outcomes, reasons = ({}, {}, {}, {}, {})
        selection = state.get("task_selection")
        selected = set(selection.selected_task_ids) if selection else None
        scope = scope_factory(state)
        coordinator = DiscoveryToolCoordinator()
        for original in state.get("review_tasks") or []:
            if selected is not None and original.id not in selected:
                continue
            patch = scope.scoped_patch(original.patch)
            task = original.model_copy(
                update={
                    "patch": patch,
                    "patch_complete": original.patch_complete
                    and patch == original.patch,
                }
            )
            context = (state.get("task_symbol_contexts") or {}).get(task.id)
            groups = change_groups(task, context)
            unresolved_lines = _unresolved_change_lines(task, context)
            limit = min(max_subtasks, max(1, task_tool_budget))
            for index in range(limit, len(groups)):
                key = f"{task.id}:{review_group_id(task.id, index)}"
                outcomes[key], reasons[key] = ("omitted", "task_budget_limit")
            groups = groups[:limit]
            has_unresolved_scope = bool(unresolved_lines and len(groups) > 1)
            if has_unresolved_scope:
                traces.append(
                    CouncilTrace(
                        node="controlled_review",
                        event="unresolved_change_scope",
                        detail=f"task={task.id} lines={','.join(map(str, unresolved_lines))}",
                    )
                )
            budgets = allocate_budgets(
                len(groups),
                total_budget=task_tool_budget,
                per_subtask_limit=max_tool_calls,
            )
            instructions = [
                SubtaskInstruction(
                    subtask_id=review_group_id(task.id, index),
                    objective=(_PROMPTS / "change-review-objective.txt").read_text(
                        encoding="utf-8"
                    ),
                    initial_symbol_ids=tuple((s.symbol_id for s in group)),
                    allowed_tools=tuple(
                        (
                            t
                            for t in ("read_symbol", "query_relations")
                            if state.get("enabled_tools") is None
                            or t in state["enabled_tools"]
                        )
                    ),
                    allowed_relations=(
                        "callers",
                        "callees",
                        "field_readers",
                        "field_writers",
                        "implementations",
                        "overrides",
                        "parents",
                        "children",
                        "type_users",
                        "type_references",
                        "entrypoints",
                    ),
                    max_tool_calls=budgets[index],
                    max_rounds=max_rounds,
                )
                for index, group in enumerate(groups)
            ]
            groups_by_subtask = {
                instruction.subtask_id: group
                for group, instruction in zip(groups, instructions)
            }
            plans[task.id] = SubtaskPlan(task_id=task.id, subtasks=tuple(instructions))
            catalog = EvidenceCatalogBuilder().build_initial(
                task=original,
                symbol_context=context,
                reviewer="behavior",
                revision=state.get("evidence_revision", ""),
            )

            def run(item):
                group, instruction = item
                scoped_context = build_subtask_context(
                    task,
                    context,
                    group,
                    unresolved_lines=unresolved_lines
                    if has_unresolved_scope and not group
                    else None,
                )
                client = CoordinatedDiscoveryToolClient(
                    tool_client,
                    coordinator,
                    projection_focus=_group_projection_focus(
                        task, context, group, scoped_context
                    ),
                    lossless_payload=True,
                    canonical_symbol_ids=True,
                    max_tool_calls=instruction.max_tool_calls,
                    max_path_depth=state.get("controlled_max_path_depth", 3),
                    allowed_relations=instruction.allowed_relations,
                    initial_symbol_ids=set(instruction.initial_symbol_ids),
                    symbol_catalog_ids=instruction.initial_symbol_ids,
                    subtask_id=instruction.subtask_id,
                )

                def prepare():
                    if tool_client is None:
                        return ""
                    with client.context_preparation():
                        return prepare_change_context(
                            client,
                            group,
                            instruction,
                            scoped_context=scoped_context,
                        )

                engine = SubtaskReactEngine(
                    client,
                    max_tool_calls=instruction.max_tool_calls,
                    max_rounds=instruction.max_rounds,
                    timeout_seconds=timeout_seconds,
                    prepare_context=prepare,
                    system_prompt=(_PROMPTS / "change-review.txt").read_text(
                        encoding="utf-8"
                    ),
                )
                return (
                    instruction,
                    client,
                    engine.run(
                        llm,
                        task=task,
                        symbol_context=context,
                        instruction=instruction,
                        scoped_context=scoped_context,
                        structured_method=state.get(
                            "structured_method", "function_calling"
                        ),
                        max_retries=state.get("max_retries", 3),
                    ),
                )

            completed = run_bounded_parallel(
                list(zip(groups, instructions)),
                run,
                max_workers=max(1, execute_concurrency),
            )
            for instruction, item in zip(instructions, completed):
                key = f"{task.id}:{instruction.subtask_id}"
                if item is None:
                    outcomes[key], reasons[key] = ("failed", "worker_failed")
                    traces.append(
                        CouncilTrace(
                            node="execute", event="task_review_failed", detail=key
                        )
                    )
                    continue
                _, client, outcome = item
                group = groups_by_subtask.get(instruction.subtask_id, ())
                scoped_context = build_subtask_context(
                    task,
                    context,
                    group,
                    unresolved_lines=unresolved_lines
                    if has_unresolved_scope and not group
                    else None,
                )
                capture = capture_tool_records(catalog, client.trace_records)
                catalog = capture.catalog
                records.extend(capture.trace_refs)
                call_aliases = {
                    a.call_id: alias
                    for alias, aid in catalog.alias_to_artifact_id.items()
                    if (a := catalog.artifacts[aid]).call_id
                }
                local_aliases = {
                    alias: call_aliases.get(call_id, "")
                    for alias, call_id in client.observation_aliases.items()
                }
                outcomes[key] = (
                    outcome.status
                    if outcome.status in {"failed", "inconclusive"}
                    else outcome.result.outcome
                    if outcome.result
                    else outcome.status
                )
                reasons[key] = outcome.reason
                if outcome.result is not None:
                    results[key] = outcome.result
                    if outcome.result.limitations and outcomes[key] != "failed":
                        outcomes[key], reasons[key] = (
                            "inconclusive",
                            outcome.reason or "review_limitations",
                        )
                    if not instruction.initial_symbol_ids and outcomes[key] != "failed":
                        outcomes[key], reasons[key] = (
                            "inconclusive",
                            "symbol_context_unavailable",
                        )
                    if not task.patch_complete and outcomes[key] != "failed":
                        outcomes[key], reasons[key] = (
                            "inconclusive",
                            "task_patch_truncated",
                        )
                    for finding in outcome.result.findings:
                        candidate = candidate_factory(
                            finding,
                            task=task,
                            reviewer="behavior",
                            catalog=catalog,
                            alias_by_call_id=local_aliases,
                            candidate_index=len(candidates) + 1,
                        )
                        if candidate is not None:
                            finding_file = str(finding.location_file).replace(
                                "\\", "/"
                            )
                            task_file = str(task.file).replace("\\", "/")
                            if finding_file != task_file:
                                traces.append(
                                    CouncilTrace(
                                        node="execute",
                                        event="finding_out_of_scope",
                                        detail=(
                                            f"{key} {finding.location_file}:{finding.location_line} "
                                            "uses an external change location"
                                        ),
                                    )
                                )
                                continue
                            if not _candidate_in_group(
                                candidate, task, scoped_context, group
                            ):
                                traces.append(
                                    CouncilTrace(
                                        node="execute",
                                        event="finding_out_of_scope",
                                        detail=(
                                            f"{key} {candidate.file}:{candidate.line} "
                                            "is not anchored to this subtask"
                                        ),
                                    )
                                )
                                continue
                            candidates.append(candidate)
                        else:
                            outcomes[key], reasons[key] = (
                                "failed",
                                "finding_without_bound_evidence",
                            )
                if outcomes[key] == "failed":
                    traces.append(
                        CouncilTrace(
                            node="execute",
                            event="task_review_failed",
                            detail=f"{key} {reasons[key]}",
                        )
                    )
                for event in outcome.events:
                    traces.append(CouncilTrace(node="execute", event=event, detail=key))
            artifacts.update(catalog.artifacts)
        candidates, _ = collapse_candidate_duplicates(candidates)
        for key, status in outcomes.items():
            if status in {"inconclusive", "omitted"}:
                traces.append(
                    CouncilTrace(
                        node="execute",
                        event="investigation_incomplete",
                        detail=f"{key} {status}",
                    )
                )
        traces.append(
            CouncilTrace(
                node="controlled_review",
                event="change_review_completed",
                detail=f"groups={len(outcomes)} candidates={len(candidates)}",
            )
        )
        return dict(
            raw_candidate_issues=candidates,
            candidate_issues=collect_candidate_reducer([], candidates),
            evidence_artifacts=artifacts,
            tool_trace_records=records,
            controlled_subtask_plans=plans,
            controlled_subtask_results=results,
            controlled_subtask_outcomes=outcomes,
            controlled_subtask_reasons=reasons,
            council_trace=traces,
            controlled_candidate_contexts={
                c.id: {
                    k: getattr(c, k, "") for k in ("mechanism", "impact", "claim_type")
                }
                for c in candidates
            },
        )

    return node
