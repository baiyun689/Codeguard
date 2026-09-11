"""Default discovery: deterministic change coverage, source preparation, one reviewer.

No model decides which changes deserve investigation. Method/type/constructor
declarations are investigated independently, while changed fields share one
field-focused investigation. Java remains the only source reader.
"""

from pathlib import Path
from hashlib import sha256
import json
import re
from typing import Any
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


def prepare_change_context(client, group, instruction) -> str:
    """Bounded first pages, charged to the same budget as subsequent exploration.

    Read roots first, then spread incoming relations across roots before outgoing
    relations. Never expand returned endpoints or follow cursors speculatively.
    """
    budget = instruction.max_tool_calls
    reserve = min(budget, max(2, budget // 3))
    preparation_limit = min(6, budget - reserve)
    reserve = budget - preparation_limit
    prepared = []
    attempted = []
    if "read_symbol" in instruction.allowed_tools:
        for symbol in group[: min(budget // 2, preparation_limit)]:
            response = client.read_symbol(
                symbol.symbol_id,
                start_line=symbol.start_line,
                end_line=min(symbol.end_line, symbol.start_line + 119),
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
    """Unique across file tasks so tool traces cannot attach to another group."""
    return f"change-{sha256(task_id.encode('utf-8')).hexdigest()[:12]}-{index + 1}"


def change_groups(task, context) -> list[tuple]:
    """Cover changed declarations with focused, deterministic investigations.

    Non-field declarations get one subtask each so unrelated methods cannot
    consume one another's investigation context. Fields are the exception:
    field declarations in the same file task share one subtask because their
    read/write relationships and initialization semantics are usually coupled.
    The group order follows the first declaration encountered in source order.
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
        enclosing = [
            s
            for s in symbols
            if s.file == task.file and s.start_line <= line <= s.end_line
        ]
        if enclosing:
            symbol = min(
                enclosing, key=lambda s: (s.end_line - s.start_line, s.symbol_id)
            )
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
    return groups or [()]


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
            limit = min(max_subtasks, max(1, task_tool_budget))
            for index in range(limit, len(groups)):
                key = f"{task.id}:{review_group_id(task.id, index)}"
                outcomes[key], reasons[key] = ("omitted", "task_budget_limit")
            groups = groups[:limit]
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
            plans[task.id] = SubtaskPlan(task_id=task.id, subtasks=tuple(instructions))
            catalog = EvidenceCatalogBuilder().build_initial(
                task=original,
                symbol_context=context,
                reviewer="behavior",
                revision=state.get("evidence_revision", ""),
            )

            def run(item):
                group, instruction = item
                client = CoordinatedDiscoveryToolClient(
                    tool_client,
                    coordinator,
                    projection_focus=graph_projection_focus(task, context),
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
                        return prepare_change_context(client, group, instruction)

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
