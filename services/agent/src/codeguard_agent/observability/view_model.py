"""把无损追踪事件整理为 Dashboard 使用的稳定视图模型。"""

from __future__ import annotations

import json
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
    "classify_mode": "PR 规模判定",
    "direct_review": "整 PR 直接审查",
    "file_task_builder": "文件级任务构建",
    "summary": "变更摘要",
    "diff_task_builder": "Hunk 级任务构建",
    "risk_triage": "风险分诊",
    "task_rank": "任务选择",
    "review_coverage": "审查覆盖规划",
    "context_provider": "上下文构建",
    "discover_threat_model": "安全候选发现",
    "discover_behavior": "行为候选发现",
    "discover_maintainability": "可维护性候选发现",
    "prepare": "准备审查",
    "collect": "汇总候选问题",
    "discovery_collector": "发现结果汇总",
    "council_coordinator": "委员会协调",
    "evidence_verifier": "证据验证",
    "council_judge": "委员会裁决",
    "direct_judge": "直接裁决",
}
_COORDINATION_NODES = {
    "council_coordinator",
    "evidence_verifier",
    "direct_judge",
    "council_judge",
}


def build_trace_view(report: TraceReport) -> dict[str, Any]:
    """构建不复制大字段内容的 Dashboard 视图索引。"""
    events_by_sequence = {
        event.sequence: event
        for event in report.events
    }
    node_steps = _pair_events(report.events, "node_start", "node_end")
    routing = _routing_view(report.events)
    llm_steps = _pair_events(report.events, "llm_start", "llm_end")
    tool_steps = _tool_event_steps(report.events)
    application_tool_steps = _application_tool_steps(report.events, tool_steps)
    main_placeholders = _missing_main_steps(node_steps, routing)
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
    small_complete = (
        routing.get("initial_mode") == "small"
        and not routing.get("fallback", False)
        and routing.get("outcome") == "completed"
    )
    discovery_only = any(
        step["code_name"] == "discovery_collector"
        for step in node_steps
    )
    review_council_step = _review_council_step(
        node_steps,
        skip_reason="small 模式按设计跳过" if small_complete else "",
    )
    coordination_loop_step = _coordination_loop_step(
        node_steps,
        report.events,
        skip_reason=(
            "small 模式按设计跳过"
            if small_complete
            else "discovery_only 模式按设计跳过"
            if discovery_only
            else ""
        ),
    )
    steps = _index_steps(
        visible_node_steps
        + state_node_steps
        + llm_steps
        + tool_steps
        + application_tool_steps
        + [review_council_step]
        + [coordination_loop_step]
    )
    degradation = report.degradation
    return {
        "main_stages": _main_stages(
            node_steps_with_placeholders,
            review_council_step,
            coordination_loop_step,
            routing,
        ),
        "routing": routing,
        "reviewer_sections": _reviewer_sections(steps),
        "coordination_steps": _coordination_steps(steps),
        "steps": steps,
        "state_writes": _state_writes(steps, events_by_sequence),
        "integrity": _integrity(report.events),
        "degradation": {
            "is_clean": degradation.is_clean,
            "total": degradation.total_degradations,
            "items": [
                {"label": "ReAct→直连(递归)", "count": degradation.react_degraded_recursion},
                {"label": "ReAct→直连(空)", "count": degradation.react_degraded_empty},
                {"label": "Direct分派", "count": degradation.direct_tier_tasks, "info": True},
                {"label": "发现者失败", "count": degradation.discoverer_failed},
                {"label": "Task失败", "count": degradation.task_review_failed},
                {"label": "Judge失败", "count": degradation.judge_synthesis_failed},
                {"label": "证据截断", "count": degradation.evidence_plan_skipped},
            ],
        },
    }


def _routing_view(events: Iterable[TraceEvent]) -> dict[str, Any]:
    """从结构化 State patch 恢复最终生效的 PR 规模路由。"""
    route: dict[str, Any] = {}
    for event in sorted(events, key=lambda item: item.sequence):
        if event.event_type != "node_end":
            continue
        output = event.detail.get("output")
        if not isinstance(output, dict):
            continue
        candidate = output.get("review_route")
        if isinstance(candidate, dict):
            route.update(candidate)
        if event.node_name == "classify_mode":
            mode = output.get("review_mode")
            if mode in {"small", "medium", "large"}:
                route.setdefault("initial_mode", mode)
                route.setdefault("effective_mode", mode)
                route.setdefault(
                    "selected_node",
                    {
                        "small": "direct_review",
                        "medium": "file_task_builder",
                        "large": "diff_task_builder",
                    }[mode],
                )
                route.setdefault("fallback", False)
        if event.node_name != "direct_review":
            continue
        status = output.get("direct_review_status")
        if status == "completed":
            route.setdefault("outcome", "completed")
        elif status == "fallback":
            route.update({
                "effective_mode": "medium",
                "selected_node": "file_task_builder",
                "fallback": True,
            })
    return route


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
    metrics = (
        _evidence_batch_metrics(end)
        if code_name == "evidence_verifier"
        else {}
    )
    summary = end.summary if end is not None else event.summary
    node_summary = _node_state_summary(code_name, end)
    if node_summary:
        summary = node_summary
    if metrics:
        summary = (
            f"{metrics.get('request_count', 0)} 个请求 · "
            f"{metrics.get('fact_count', 0)} 条事实 · "
            f"{metrics.get('llm_analysis_calls', 0)} 次 LLM · "
            f"分析 {float(metrics.get('fact_analysis_ms', 0.0)) / 1000:.3f}s"
        )
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
        "summary": summary,
        "metrics": metrics,
    }


def _node_state_summary(code_name: str, event: TraceEvent | None) -> str:
    if event is None:
        return ""
    output = event.detail.get("output")
    if not isinstance(output, dict):
        return ""
    if code_name == "classify_mode":
        route = output.get("review_route")
        if isinstance(route, dict):
            metrics = route.get("metrics")
            metrics = metrics if isinstance(metrics, dict) else {}
            return (
                f"{route.get('initial_mode', 'unknown')} 模式 · "
                f"{metrics.get('file_count', 0)} 文件 · "
                f"{metrics.get('hunk_count', 0)} hunks · "
                f"{metrics.get('diff_chars', 0)} 字符"
            )
    if code_name == "direct_review":
        route = output.get("review_route")
        if isinstance(route, dict) and route.get("fallback"):
            return (
                "Direct 失败，降级到文件级完整管线 · "
                f"{route.get('fallback_reason', 'unknown')}"
            )
        if output.get("direct_review_status") == "completed":
            return f"整 PR 审查完成 · {len(output.get('final_issues') or [])} 个问题"
    traces = output.get("council_trace")
    if isinstance(traces, list):
        for trace in reversed(traces):
            if (
                isinstance(trace, dict)
                and trace.get("node") == code_name
                and str(trace.get("detail") or "").strip()
            ):
                return str(trace["detail"])
    return ""


def _evidence_batch_metrics(event: TraceEvent | None) -> dict[str, Any]:
    if event is None:
        return {}
    output = event.detail.get("output")
    if not isinstance(output, dict):
        return {}
    traces = output.get("council_trace")
    if not isinstance(traces, list):
        return {}
    for trace in traces:
        if (
            not isinstance(trace, dict)
            or trace.get("event") != "evidence_batch_metrics"
        ):
            continue
        try:
            detail = json.loads(str(trace.get("detail") or "{}"))
        except (TypeError, json.JSONDecodeError):
            return {}
        return detail if isinstance(detail, dict) else {}
    return {}


def _tool_event_steps(
    events: Iterable[TraceEvent],
) -> list[dict[str, Any]]:
    starts = {
        event.run_id: event
        for event in events
        if event.event_type == "tool_start" and event.run_id
    }
    ends = {
        event.run_id: event
        for event in events
        if event.event_type in {"tool_end", "tool_error"} and event.run_id
    }
    result: list[dict[str, Any]] = []
    for event in events:
        if event.event_type != "tool_start":
            continue
        end = ends.get(event.run_id)
        result.append(_tool_step(event, end))
    for event in events:
        if (
            event.event_type not in {"tool_end", "tool_error"}
            or event.run_id in starts
        ):
            continue
        result.append(_tool_step(None, event))
    return result


def _application_tool_steps(
    events: Iterable[TraceEvent],
    native_tool_steps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从节点输出恢复未进入 LangChain 事件流的真实工具调用。

    同步嵌套 ReAct 和 EvidenceAgent 直接 HTTP 调用不会总是产生外层
    ``tool_start/tool_end``。它们仍会把规范化的 ``GatheredContext`` 写入
    节点 output；这里将其提升为与原生工具步骤同构的只读视图记录。
    """
    event_list = list(events)
    events_by_sequence = {event.sequence: event for event in event_list}
    native_keys: set[tuple[str, str, str]] = set()
    seen_application_call_ids: set[str] = set()
    for step in native_tool_steps:
        start_sequence = step.get("start_sequence")
        start = (
            events_by_sequence.get(start_sequence)
            if isinstance(start_sequence, int)
            else None
        )
        arguments = start.detail.get("input") if start is not None else None
        native_keys.add(
            (
                str(step.get("reviewer_root", "")),
                str(step.get("code_name", "")),
                json.dumps(arguments, ensure_ascii=False, sort_keys=True),
            )
        )
    result: list[dict[str, Any]] = []
    for event in event_list:
        if event.event_type != "node_end":
            continue
        output = event.detail.get("output")
        if not isinstance(output, dict):
            continue
        reviewer_root = _reviewer_root_for_event(event)
        council_trace = output.get("council_trace")
        evidence_called_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        if isinstance(council_trace, list):
            for trace_item in council_trace:
                if (
                    not isinstance(trace_item, dict)
                    or trace_item.get("event") != "evidence_tool_called"
                ):
                    continue
                try:
                    trace_detail = json.loads(
                        str(trace_item.get("detail") or "{}")
                    )
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(trace_detail, dict):
                    continue
                trace_tool = str(trace_detail.get("tool") or "")
                trace_arguments = trace_detail.get("arguments", {})
                evidence_called_by_key[
                    (
                        trace_tool,
                        json.dumps(
                            trace_arguments,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )
                ] = trace_detail
        records = output.get("tool_trace_records")
        if isinstance(records, list):
            for index, item in enumerate(records):
                if not isinstance(item, dict):
                    continue
                tool_name = str(item.get("tool") or "")
                arguments = item.get("arguments", {})
                if not tool_name:
                    continue
                call_id = str(item.get("call_id") or "")
                if call_id and call_id in seen_application_call_ids:
                    continue
                if call_id:
                    seen_application_call_ids.add(call_id)
                status = str(item.get("status") or "complete")
                dedup_key = (
                    reviewer_root,
                    tool_name,
                    json.dumps(arguments, ensure_ascii=False, sort_keys=True),
                )
                if status != "reused" and dedup_key in native_keys:
                    continue
                if status != "reused":
                    native_keys.add(dedup_key)
                result.append(
                    {
                        "id": f"application-tool-record:{event.sequence}:{index}",
                        "sequence": event.sequence,
                        "kind": "tool",
                        "title": "工具调用",
                        "code_name": tool_name,
                        "node_path": f"{event.node_path or event.node_name}/{tool_name}",
                        "reviewer_root": reviewer_root,
                        "invocation_id": event.invocation_id,
                        "pair_id": call_id,
                        "start_sequence": None,
                        "end_sequence": None,
                        "duration_ms": max(
                            0.0, float(item.get("duration_ms") or 0.0)
                        ),
                        "status": status,
                        "summary": (
                            "复用已缓存工具结果"
                            if status == "reused"
                            else f"应用级工具记录 · {status}"
                        ),
                        "input": arguments,
                        "output": item.get("output"),
                        "reuse_key": str(item.get("reuse_key") or ""),
                        "reused_from_call_id": str(
                            item.get("reused_from_call_id") or ""
                        ),
                    }
                )
        gathered = output.get("gathered_context")
        if not isinstance(gathered, list):
            continue
        for index, item in enumerate(gathered):
            if not isinstance(item, dict):
                continue
            tool_name = str(item.get("tool") or "")
            if not tool_name:
                continue
            raw_args = item.get("args", "")
            try:
                arguments = (
                    json.loads(raw_args)
                    if isinstance(raw_args, str)
                    else raw_args
                )
            except (TypeError, json.JSONDecodeError):
                arguments = raw_args
            called_detail = evidence_called_by_key.get(
                (
                    tool_name,
                    json.dumps(
                        arguments,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
                {},
            )
            call_id = str(called_detail.get("call_id") or "")
            if call_id and call_id in seen_application_call_ids:
                continue
            if call_id:
                seen_application_call_ids.add(call_id)
            dedup_key = (
                reviewer_root,
                tool_name,
                json.dumps(arguments, ensure_ascii=False, sort_keys=True),
            )
            if dedup_key in native_keys:
                continue
            native_keys.add(dedup_key)
            duration_ms = float(item.get("duration_ms") or 0.0)
            status = str(item.get("status") or "complete")
            result.append(
                {
                    "id": f"application-tool:{event.sequence}:{index}",
                    "sequence": event.sequence,
                    "kind": "tool",
                    "title": "工具调用",
                    "code_name": tool_name,
                    "node_path": f"{event.node_path or event.node_name}/{tool_name}",
                    "reviewer_root": reviewer_root,
                    "invocation_id": event.invocation_id,
                    "pair_id": call_id,
                    "start_sequence": None,
                    "end_sequence": None,
                    "duration_ms": max(0.0, duration_ms),
                    "status": status,
                    "summary": f"应用级工具记录 · {status}",
                    "input": arguments,
                    "output": item.get("content"),
                    "reuse_key": str(called_detail.get("reuse_key") or ""),
                    "reused_from_call_id": "",
                }
            )
        if not isinstance(council_trace, list):
            continue
        for index, item in enumerate(council_trace):
            if (
                not isinstance(item, dict)
                or item.get("event") != "evidence_tool_reused"
            ):
                continue
            try:
                detail = json.loads(str(item.get("detail") or "{}"))
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(detail, dict):
                continue
            tool_name = str(detail.get("tool") or "")
            if not tool_name:
                continue
            call_id = str(detail.get("call_id") or "")
            if call_id and call_id in seen_application_call_ids:
                continue
            if call_id:
                seen_application_call_ids.add(call_id)
            result.append(
                {
                    "id": f"application-tool-reuse:{event.sequence}:{index}",
                    "sequence": event.sequence,
                    "kind": "tool",
                    "title": "工具调用",
                    "code_name": tool_name,
                    "node_path": f"{event.node_path or event.node_name}/{tool_name}",
                    "reviewer_root": reviewer_root,
                    "invocation_id": event.invocation_id,
                    "pair_id": call_id,
                    "start_sequence": None,
                    "end_sequence": None,
                    "duration_ms": 0.0,
                    "status": "reused",
                    "summary": "复用已缓存工具结果",
                    "input": detail.get("arguments", {}),
                    "output": detail.get("output", "复用首次调用结果"),
                    "reuse_key": str(detail.get("reuse_key") or ""),
                    "reused_from_call_id": str(
                        detail.get("reused_from_call_id") or ""
                    ),
                }
            )
    return result


def _tool_step(
    start: TraceEvent | None,
    end: TraceEvent | None,
) -> dict[str, Any]:
    event = start or end
    assert event is not None
    sequence = start.sequence if start is not None else event.sequence
    run_id = event.run_id
    failed = end is not None and end.event_type == "tool_error"
    duration_ms = (
        max(0.0, end.timestamp_ms - start.timestamp_ms)
        if start is not None and end is not None
        else 0.0
    )
    tool_name = str(event.detail.get("tool_name") or event.node_name)
    return {
        "id": f"tool:{run_id or sequence}",
        "sequence": sequence,
        "kind": "tool",
        "title": "工具调用",
        "code_name": tool_name,
        "node_path": event.node_path or event.node_name,
        "reviewer_root": _reviewer_root_for_event(event),
        "invocation_id": event.invocation_id,
        "pair_id": run_id,
        "start_sequence": start.sequence if start is not None else None,
        "end_sequence": end.sequence if end is not None else None,
        "duration_ms": duration_ms,
        "status": (
            "failed"
            if failed
            else "complete"
            if start is not None and end is not None
            else "missing"
        ),
        "summary": end.summary if end is not None else event.summary,
    }


def _reviewer_root_for_event(event: TraceEvent) -> str:
    path_root = str(event.node_path).split("/", 1)[0]
    if path_root in REVIEWERS:
        return path_root
    metadata = event.detail.get("metadata")
    if isinstance(metadata, dict):
        namespace = str(metadata.get("langgraph_checkpoint_ns") or "")
        for path_root in REVIEWERS:
            if path_root in namespace:
                return path_root
    return ""


def _is_visible_node_step(step: dict[str, Any]) -> bool:
    code_name = step["code_name"]
    if code_name in {
        "classify_mode",
        "direct_review",
        "file_task_builder",
        "diff_task_builder",
        "risk_triage",
        "task_rank",
        "review_coverage",
        "summary",
        "context_provider",
        "discovery_collector",
        "council_judge",
    }:
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
    routing: dict[str, Any],
) -> list[dict[str, Any]]:
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for step in node_steps:
        by_name[step["code_name"]].append(step)

    stages: list[dict[str, Any]] = []
    if "classify_mode" in by_name:
        stages.append(_main_stage(
            "classify_mode",
            _NODE_TITLES["classify_mode"],
            by_name["classify_mode"],
        ))
        if "direct_review" in by_name:
            stages.append(_main_stage(
                "direct_review",
                _NODE_TITLES["direct_review"],
                by_name["direct_review"],
            ))
        builder = str(routing.get("selected_node") or "")
        if builder not in {"file_task_builder", "diff_task_builder"}:
            if "file_task_builder" in by_name:
                builder = "file_task_builder"
            elif "diff_task_builder" in by_name:
                builder = "diff_task_builder"
        if builder in {"file_task_builder", "diff_task_builder"} and builder in by_name:
            stages.append(_main_stage(builder, _NODE_TITLES[builder], by_name[builder]))
        for code_name in ("risk_triage", "task_rank", "review_coverage"):
            if code_name in by_name:
                stages.append(_main_stage(
                    code_name,
                    _NODE_TITLES[code_name],
                    by_name[code_name],
                ))
    else:
        for code_name, title in (
            ("diff_task_builder", "审查任务构建"),
            ("risk_triage", "风险分诊"),
            ("task_rank", "任务选择"),
        ):
            if code_name in by_name:
                stages.append(_main_stage(code_name, title, by_name[code_name]))
    stages.append(_main_stage("summary", "变更摘要", by_name.get("summary")))
    stages.append(_main_stage(
        "context_provider",
        "上下文构建",
        by_name.get("context_provider"),
    ))

    stages.append({
        "id": "main:review_council",
        "title": "审查委员会",
        "code_name": "review_council",
        "status": review_council_step["status"],
        "step_id": review_council_step["id"],
        "sequence": review_council_step["sequence"],
        "summary": review_council_step["summary"],
    })
    if "discovery_collector" in by_name:
        stages.append(_main_stage(
            "discovery_collector",
            _NODE_TITLES["discovery_collector"],
            by_name["discovery_collector"],
        ))
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
    *,
    skip_reason: str = "",
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
        "status": (
            "complete"
            if discoverers
            else "skipped"
            if skip_reason
            else "missing"
        ),
        "summary": (
            f"{len(discoverers)} 名审查员并行执行"
            if discoverers
            else skip_reason or "未采集到审查员执行"
        ),
    }


def _coordination_loop_step(
    node_steps: list[dict[str, Any]],
    events: Iterable[TraceEvent],
    *,
    skip_reason: str = "",
) -> dict[str, Any]:
    coordination = [
        step
        for step in node_steps
        if step["code_name"] in _COORDINATION_NODES
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
        if step["code_name"] == "evidence_verifier"
    )
    summary_parts = [
        f"协调 {coordinator_count} 次",
        f"证据验证 {evidence_count} 次",
        f"路由 {route_count} 次",
    ]
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
        "status": (
            "complete"
            if coordination
            else "skipped"
            if skip_reason
            else "missing"
        ),
        "summary": "，".join(summary_parts) if coordination else skip_reason,
    }


def _missing_main_steps(
    node_steps: list[dict[str, Any]],
    routing: dict[str, Any],
) -> list[dict[str, Any]]:
    present = {step["code_name"] for step in node_steps}
    placeholders: list[dict[str, Any]] = []
    small_complete = (
        routing.get("initial_mode") == "small"
        and not routing.get("fallback", False)
        and routing.get("outcome") == "completed"
    )
    discovery_only = "discovery_collector" in present
    expected: tuple[str, ...] = (
        (
            "risk_triage",
            "task_rank",
            "review_coverage",
            "summary",
            "context_provider",
            "council_judge",
        )
        if small_complete
        else ("summary", "context_provider", "council_judge")
    )
    if (
        routing.get("initial_mode") == "small"
        and "direct_review" not in present
    ):
        expected = ("direct_review", *expected)
    for index, code_name in enumerate(expected, start=1):
        if code_name in present:
            continue
        configured_skip = (
            code_name == "summary"
            and "context_provider" in present
            and "summary" not in present
        )
        discovery_skip = discovery_only and code_name == "council_judge"
        status = (
            "skipped"
            if small_complete or configured_skip or discovery_skip
            else "missing"
        )
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
            "status": status,
            "summary": (
                "small 模式按设计跳过"
                if small_complete
                else "Summary 未启用，按配置跳过"
                if configured_skip
                else "discovery_only 模式按设计跳过"
                if discovery_skip
                else "当前 Trace 未采集到该节点"
            ),
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
            if (
                str(step["node_path"]).split("/", 1)[0] == path_root
                or step.get("reviewer_root") == path_root
            )
            and not step.get("hidden")
        ]
        if not owned:
            # Phase 4+ 的 task-scoped 发现者把发现流程收敛为单个
            # discover_* 节点，不再产生旧 prepare/review/collect 子节点。
            # 该 wrapper 节点仍有 candidate_issues 等 State patch，必须作为
            # Reviewer 面板的可见锚点，不能因其被 State 视图标为 hidden 而丢失。
            owned = [
                step
                for step in steps.values()
                if step["code_name"] == path_root
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
            "tool_step_ids": [
                step["id"] for step in owned if step["kind"] == "tool"
            ],
            "tool_call_count": sum(
                step["kind"] == "tool" for step in owned
            ),
        })
    return sections


def _coordination_steps(
    steps: dict[str, dict[str, Any]],
) -> list[str]:
    return [
        step["id"]
        for step in steps.values()
        if (
            step["code_name"] in _COORDINATION_NODES
            or str(step["node_path"]).split("/", 1)[0] in _COORDINATION_NODES
        )
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
        if (
            event.event_type.endswith("_end")
            or event.event_type == "tool_error"
        )
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
