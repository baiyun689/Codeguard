"""把无损追踪事件整理为 Dashboard 使用的稳定视图模型。"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from codeguard_agent.observability.models import TraceEvent, TraceReport

REVIEWERS: dict[str, tuple[str, str, str]] = {
    "discover_threat_model": (
        "threat_model",
        "威胁建模审查员",
        "ThreatModelAgent",
    ),
    "discover_behavior": (
        "behavior",
        "行为审查员",
        "BehaviorAgent",
    ),
    "discover_maintainability": (
        "maintainability",
        "可维护性审查员",
        "MaintainabilityAgent",
    ),
}

_NODE_TITLES: dict[str, str] = {
    "summary": "变更摘要",
    "context_provider": "上下文构建",
    "prepare": "准备审查",
    "collect": "汇总候选问题",
    "council_coordinator": "委员会协调",
    "evidence_agent": "证据补充",
    "council_judge": "委员会裁决",
}
_COORDINATION_NODES = {
    "council_coordinator",
    "evidence_agent",
    "challenge_agent",
    "council_judge",
}


def build_trace_view(report: TraceReport) -> dict[str, Any]:
    """构建不复制大字段内容的 Dashboard 视图索引。"""
    events_by_sequence = {
        event.sequence: event
        for event in report.events
    }
    node_steps = _pair_events(report.events, "node_start", "node_end")
    llm_steps = _pair_events(report.events, "llm_start", "llm_end")
    tool_steps = _tool_event_steps(report.events)
    main_placeholders = _missing_main_steps(node_steps)
    node_steps_with_placeholders = node_steps + main_placeholders
    visible_node_steps = [
        step
        for step in node_steps_with_placeholders
        if _is_visible_node_step(step)
    ]
    state_node_steps = _state_only_node_steps(
        node_steps,
        visible_node_steps,
        events_by_sequence,
    )
    review_council_step = _review_council_step(node_steps)
    coordination_loop_step = _coordination_loop_step(node_steps, report.events)
    steps = _index_steps(
        visible_node_steps
        + state_node_steps
        + llm_steps
        + tool_steps
        + [review_council_step]
        + [coordination_loop_step]
    )
    return {
        "main_stages": _main_stages(
            node_steps_with_placeholders,
            review_council_step,
            coordination_loop_step,
        ),
        "reviewer_sections": _reviewer_sections(steps),
        "coordination_steps": _coordination_steps(steps),
        "steps": steps,
        "state_writes": _state_writes(steps, events_by_sequence),
        "integrity": _integrity(report.events),
    }


def _pair_events(
    events: Iterable[TraceEvent],
    start_type: str,
    end_type: str,
) -> list[dict[str, Any]]:
    starts = {
        event.run_id: event
        for event in events
        if event.event_type == start_type
    }
    ends = {
        event.run_id: event
        for event in events
        if event.event_type == end_type
    }
    kind = "llm" if start_type == "llm_start" else "node"
    result: list[dict[str, Any]] = []
    for run_id, start in starts.items():
        end = ends.get(run_id)
        step_id = f"{kind}:{run_id or start.sequence}"
        result.append(_step_from_pair(step_id, kind, start, end))
    for run_id, end in ends.items():
        if run_id not in starts:
            step_id = f"{kind}:orphan-end:{run_id or end.sequence}"
            result.append(_step_from_pair(step_id, kind, None, end))
    return result


def _step_from_pair(
    step_id: str,
    kind: str,
    start: TraceEvent | None,
    end: TraceEvent | None,
) -> dict[str, Any]:
    event = start or end
    assert event is not None
    sequence = start.sequence if start is not None else event.sequence
    duration_ms = (
        max(0.0, end.timestamp_ms - start.timestamp_ms)
        if start is not None and end is not None
        else 0.0
    )
    code_name = event.node_name
    return {
        "id": step_id,
        "sequence": sequence,
        "kind": kind,
        "title": (
            "模型决策"
            if kind == "llm"
            else _NODE_TITLES.get(code_name, code_name)
        ),
        "code_name": code_name,
        "node_path": event.node_path or code_name,
        "invocation_id": event.invocation_id,
        "pair_id": event.run_id,
        "start_sequence": start.sequence if start is not None else None,
        "end_sequence": end.sequence if end is not None else None,
        "duration_ms": duration_ms,
        "status": "complete" if start is not None and end is not None else "missing",
        "summary": end.summary if end is not None else event.summary,
    }


def _tool_event_steps(
    events: Iterable[TraceEvent],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for event in events:
        if event.event_type not in {"tool_start", "tool_end"}:
            continue
        is_start = event.event_type == "tool_start"
        tool_name = str(event.detail.get("tool_name") or event.node_name)
        kind = "tool_call" if is_start else "tool_result"
        result.append({
            "id": f"{kind}:{event.sequence}",
            "sequence": event.sequence,
            "kind": kind,
            "title": "工具调用" if is_start else "工具结果",
            "code_name": tool_name,
            "node_path": event.node_path or event.node_name,
            "invocation_id": event.invocation_id,
            "pair_id": event.run_id,
            "start_sequence": event.sequence if is_start else None,
            "end_sequence": event.sequence if not is_start else None,
            "duration_ms": 0.0,
            "status": "complete",
            "summary": event.summary,
        })
    return result


def _is_visible_node_step(step: dict[str, Any]) -> bool:
    code_name = step["code_name"]
    if code_name in {"summary", "context_provider", "council_judge"}:
        return True
    if code_name in {"review", "model", "tools"}:
        return False
    root = str(step["node_path"]).split("/", 1)[0]
    if root in REVIEWERS:
        return code_name in {"prepare", "collect"}
    return code_name in _COORDINATION_NODES


def _state_only_node_steps(
    node_steps: list[dict[str, Any]],
    visible_node_steps: list[dict[str, Any]],
    events_by_sequence: dict[int, TraceEvent],
) -> list[dict[str, Any]]:
    """保留 hidden node 的状态写入索引,但不把它们塞进流程列表。

    LangGraph 子图的 wrapper 节点（如 discover_*）可能不适合作为用户
    主要流程步骤展示,但它们的 node_end output 仍是真实 State patch。
    状态演进视图必须能索引这些 patch,否则 candidate_issues 等关键字段会消失。
    """
    visible_ids = {step["id"] for step in visible_node_steps}
    result: list[dict[str, Any]] = []
    for step in node_steps:
        if step["id"] in visible_ids or step["end_sequence"] is None:
            continue
        event = events_by_sequence.get(step["end_sequence"])
        output = event.detail.get("output") if event is not None else None
        if not isinstance(output, dict) or not output:
            continue
        state_step = dict(step)
        state_step["hidden"] = True
        result.append(state_step)
    return result


def _index_steps(
    steps: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        step["id"]: step
        for step in sorted(steps, key=lambda item: item["sequence"])
    }


def _main_stages(
    node_steps: list[dict[str, Any]],
    review_council_step: dict[str, Any],
    coordination_loop_step: dict[str, Any],
) -> list[dict[str, Any]]:
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for step in node_steps:
        by_name[step["code_name"]].append(step)

    stages: list[dict[str, Any]] = []
    for code_name, title in (
        ("summary", "变更摘要"),
        ("context_provider", "上下文构建"),
    ):
        stages.append(_main_stage(code_name, title, by_name.get(code_name)))

    stages.append({
        "id": "main:review_council",
        "title": "审查委员会",
        "code_name": "review_council",
        "status": review_council_step["status"],
        "step_id": review_council_step["id"],
        "sequence": review_council_step["sequence"],
        "summary": review_council_step["summary"],
    })
    stages.append({
        "id": "main:coordination_loop",
        "title": "协调与证据",
        "code_name": "coordination_loop",
        "status": coordination_loop_step["status"],
        "step_id": coordination_loop_step["id"],
        "sequence": coordination_loop_step["sequence"],
        "summary": coordination_loop_step["summary"],
    })
    stages.append(_main_stage(
        "council_judge",
        "委员会裁决",
        by_name.get("council_judge"),
    ))
    return stages


def _review_council_step(
    node_steps: list[dict[str, Any]],
) -> dict[str, Any]:
    discoverers = [
        step
        for step in node_steps
        if step["code_name"] in REVIEWERS
    ]
    return {
        "id": "group:review_council",
        "sequence": min(
            (step["sequence"] for step in discoverers),
            default=0,
        ),
        "kind": "group",
        "title": "审查委员会",
        "code_name": "review_council",
        "node_path": "review_council",
        "invocation_id": "",
        "pair_id": "",
        "start_sequence": None,
        "end_sequence": None,
        "duration_ms": 0.0,
        "status": "complete" if discoverers else "missing",
        "summary": f"{len(discoverers)} 名审查员并行执行",
    }


def _coordination_loop_step(
    node_steps: list[dict[str, Any]],
    events: Iterable[TraceEvent],
) -> dict[str, Any]:
    coordination = [
        step
        for step in node_steps
        if step["code_name"] in {"council_coordinator", "evidence_agent"}
    ]
    route_count = sum(
        1
        for event in events
        if event.event_type == "route_decision"
    )
    if route_count == 0:
        route_count = sum(
            1
            for event in events
            if event.event_type == "node_end"
            and isinstance(event.detail.get("output"), dict)
            and event.detail["output"].get("council_route")
        )
    coordinator_count = sum(
        1
        for step in coordination
        if step["code_name"] == "council_coordinator"
    )
    evidence_count = sum(
        1
        for step in coordination
        if step["code_name"] == "evidence_agent"
    )
    return {
        "id": "group:coordination_loop",
        "sequence": min(
            (step["sequence"] for step in coordination),
            default=0,
        ),
        "kind": "group",
        "title": "协调与证据闭环",
        "code_name": "coordination_loop",
        "node_path": "coordination_loop",
        "invocation_id": "",
        "pair_id": "",
        "start_sequence": None,
        "end_sequence": None,
        "duration_ms": sum(step["duration_ms"] for step in coordination),
        "status": "complete" if coordination else "missing",
        "summary": (
            f"协调 {coordinator_count} 次，证据补充 {evidence_count} 次，"
            f"路由 {route_count} 次"
        ),
    }


def _missing_main_steps(
    node_steps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    present = {step["code_name"] for step in node_steps}
    placeholders: list[dict[str, Any]] = []
    for index, code_name in enumerate(
        ("summary", "context_provider", "council_judge"),
        start=1,
    ):
        if code_name in present:
            continue
        placeholders.append({
            "id": f"placeholder:{code_name}",
            "sequence": 1_000_000 + index,
            "kind": "node",
            "title": _NODE_TITLES[code_name],
            "code_name": code_name,
            "node_path": code_name,
            "invocation_id": "",
            "pair_id": "",
            "start_sequence": None,
            "end_sequence": None,
            "duration_ms": 0.0,
            "status": "missing",
            "summary": "当前 Trace 未采集到该节点",
        })
    return placeholders


def _main_stage(
    code_name: str,
    title: str,
    candidates: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    step = candidates[0] if candidates else None
    return {
        "id": f"main:{code_name}",
        "title": title,
        "code_name": code_name,
        "status": step["status"] if step is not None else "missing",
        "step_id": step["id"] if step is not None else None,
        "sequence": step["sequence"] if step is not None else 0,
        "summary": step["summary"] if step is not None else "未采集到该节点",
    }


def _reviewer_sections(
    steps: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for path_root, (key, title, code_name) in REVIEWERS.items():
        owned = [
            step
            for step in steps.values()
            if str(step["node_path"]).split("/", 1)[0] == path_root
            and not step.get("hidden")
        ]
        owned.sort(key=lambda item: item["sequence"])
        round_number = 0
        for step in owned:
            if step["kind"] == "llm":
                round_number += 1
            step["round"] = round_number
            step["reviewer"] = key
        sections.append({
            "key": key,
            "title": title,
            "code_name": code_name,
            "path_root": path_root,
            "step_ids": [step["id"] for step in owned],
        })
    return sections


def _coordination_steps(
    steps: dict[str, dict[str, Any]],
) -> list[str]:
    return [
        step["id"]
        for step in steps.values()
        if step["code_name"] in _COORDINATION_NODES
    ]


def _state_writes(
    steps: dict[str, dict[str, Any]],
    events_by_sequence: dict[int, TraceEvent],
) -> dict[str, list[dict[str, Any]]]:
    writes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for step in steps.values():
        if step["kind"] != "node" or step["end_sequence"] is None:
            continue
        event = events_by_sequence[step["end_sequence"]]
        output = event.detail.get("output")
        if not isinstance(output, dict):
            continue
        for field_name in output:
            writes[str(field_name)].append({
                "step_id": step["id"],
                "sequence": step["sequence"],
                "node_path": step["node_path"],
                "semantics": "state_patch",
            })
    return dict(writes)


def _integrity(events: Iterable[TraceEvent]) -> dict[str, Any]:
    event_list = list(events)
    starts = {
        event.run_id
        for event in event_list
        if event.event_type.endswith("_start")
    }
    ends = {
        event.run_id
        for event in event_list
        if event.event_type.endswith("_end")
    }
    missing_end = starts - ends
    missing_start = ends - starts
    unassociated = [
        event
        for event in event_list
        if (event.node_path or event.node_name) in {"", "unknown"}
    ]
    is_complete = not missing_end and not missing_start and not unassociated
    return {
        "status": "complete" if is_complete else "incomplete",
        "event_count": len(event_list),
        "missing_end_count": len(missing_end),
        "missing_start_count": len(missing_start),
        "unassociated_count": len(unassociated),
    }
