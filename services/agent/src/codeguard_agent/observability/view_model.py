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
    "self_checker": "最终裁决",
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
    visible_node_steps = [
        step
        for step in node_steps
        if _is_visible_node_step(step)
    ]
    steps = _index_steps(visible_node_steps + llm_steps + tool_steps)
    return {
        "main_stages": _main_stages(node_steps),
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
    if code_name in {"review", "model", "tools"}:
        return False
    root = str(step["node_path"]).split("/", 1)[0]
    if root in REVIEWERS:
        return code_name in {"prepare", "collect"}
    return code_name in _COORDINATION_NODES


def _index_steps(
    steps: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        step["id"]: step
        for step in sorted(steps, key=lambda item: item["sequence"])
    }


def _main_stages(
    node_steps: list[dict[str, Any]],
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

    discoverers = [
        step
        for root in REVIEWERS
        for step in by_name.get(root, [])
    ]
    stages.append({
        "id": "main:review_council",
        "title": "审查委员会",
        "code_name": "review_council",
        "status": "complete" if discoverers else "missing",
        "step_id": None,
        "sequence": min(
            (step["sequence"] for step in discoverers),
            default=0,
        ),
        "summary": f"{len(discoverers)} 名审查员",
    })
    stages.append(_main_stage(
        "self_checker",
        "最终裁决",
        by_name.get("self_checker"),
    ))
    return stages


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
        for field_name, value in output.items():
            writes[str(field_name)].append({
                "step_id": step["id"],
                "sequence": step["sequence"],
                "node_path": step["node_path"],
                "value": value,
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
