"""把无损追踪事件整理为 Dashboard 使用的稳定视图模型。"""

from __future__ import annotations
import json
from collections import defaultdict
from typing import Any, Iterable
from codeguard_agent.observability.models import TraceEvent, TraceReport
from codeguard_agent.observability.serialization import normalize_tool_result

REVIEWERS: dict[str, tuple[str, str, str]] = {}
_NODE_TITLES: dict[str, str] = {
    "classify_mode": "PR 规模判定",
    "file_task_builder": "文件级任务构建",
    "diff_task_builder": "Hunk 级任务构建",
    "task_route": "Task 路由",
    "direct_task_review": "Direct Task 审查",
    "task_selection": "任务选择",
    "symbol_resolution": "符号解析",
    "controlled_review": "受控审查",
    "controlled_diagnostics": "受控审查诊断",
    "discovery_collector": "发现结果汇总",
    "council_coordinator": "候选汇总",
    "evidence_verifier": "证据验证",
    "council_judge": "结果裁决",
    "causal_merge": "语义合并",
    "direct_judge": "直接裁决",
}
_COORDINATION_NODES = {
    "council_coordinator",
    "evidence_verifier",
    "direct_judge",
    "council_judge",
    "causal_merge",
}
_STATE_REF_UNSET = object()


def _event_state_write(event: TraceEvent | None) -> Any:
    """读取节点写入的状态片段，支持 state_write 和输入载荷两种记录形式。"""
    if event is None:
        return None
    return event.detail.get("state_write", event.detail.get("output"))


def build_trace_view(report: TraceReport) -> dict[str, Any]:
    """构建不复制大字段内容的 Dashboard 视图索引。"""
    events_by_sequence = {event.sequence: event for event in report.events}
    node_steps = _pair_events(report.events, "node_start", "node_end")
    routing = _routing_view(report.events)
    llm_steps = _pair_events(report.events, "llm_start", "llm_end")
    tool_steps = _tool_event_steps(report.events, report.artifacts)
    application_tool_steps = _application_tool_steps(
        report.events, tool_steps, report.artifacts
    )
    node_steps_with_placeholders = node_steps
    visible_node_steps = [
        step for step in node_steps_with_placeholders if _is_visible_node_step(step)
    ]
    state_node_steps = _state_only_node_steps(
        node_steps, visible_node_steps, events_by_sequence
    )
    decision_summary = _decision_summary(report.events)
    steps = _index_steps(
        visible_node_steps
        + state_node_steps
        + llm_steps
        + tool_steps
        + application_tool_steps
    )
    controlled_sections = _controlled_sections(steps, report.events, report.artifacts)
    degradation = report.degradation
    return {
        "main_stages": _main_stages(
            node_steps_with_placeholders, decision_summary=decision_summary
        ),
        "routing": routing,
        "reviewer_sections": [],
        "controlled_sections": controlled_sections,
        "decision_summary": decision_summary,
        "steps": steps,
        "state_writes": _state_writes(steps, events_by_sequence),
        "integrity": _integrity(report.events),
        "degradation": {
            "is_clean": degradation.is_clean,
            "total": degradation.total_degradations,
            "items": [
                {
                    "label": "Direct分派",
                    "count": degradation.direct_tier_tasks,
                    "info": True,
                },
                {"label": "发现者失败", "count": degradation.discoverer_failed},
                {"label": "Task失败", "count": degradation.task_review_failed},
                {"label": "Judge失败", "count": degradation.judge_synthesis_failed},
            ],
        },
    }


def _routing_view(events: Iterable[TraceEvent]) -> dict[str, Any]:
    """从结构化 State patch 恢复最终生效的 PR 规模路由。"""
    route: dict[str, Any] = {}
    for event in sorted(events, key=lambda item: item.sequence):
        if event.event_type != "node_end":
            continue
        output = _event_state_write(event)
        if not isinstance(output, dict):
            continue
        candidate = output.get("review_route")
        if isinstance(candidate, dict):
            route.update(candidate)
        if event.node_name == "classify_mode":
            mode = output.get("review_mode")
            if mode in {"normal", "large"}:
                route.setdefault("initial_mode", mode)
                route.setdefault("effective_mode", mode)
                route.setdefault(
                    "selected_node",
                    {
                        "normal": "file_task_builder",
                        "large": "diff_task_builder",
                    }[mode],
                )
                route.setdefault("fallback", False)
    return route


def _pair_events(
    events: Iterable[TraceEvent], start_type: str, end_type: str
) -> list[dict[str, Any]]:
    starts = {event.run_id: event for event in events if event.event_type == start_type}
    ends = {event.run_id: event for event in events if event.event_type == end_type}
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
    step_id: str, kind: str, start: TraceEvent | None, end: TraceEvent | None
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
    metrics = _evidence_batch_metrics(end) if code_name == "evidence_verifier" else {}
    summary = end.summary if end is not None else event.summary
    node_summary = _node_state_summary(code_name, end)
    if node_summary:
        summary = node_summary
    if metrics:
        if "candidates" in metrics and "artifacts_patch" in metrics:
            parts = [
                f"artifacts p{metrics.get('artifacts_patch', 0)}/c{metrics.get('artifacts_context', 0)}/t{metrics.get('artifacts_tool', 0)} · refs {metrics.get('refs_selected', 0)}(v{metrics.get('refs_valid', 0)}/l{metrics.get('refs_limited', 0)}/i{metrics.get('refs_invalid', 0)}) · replay {metrics.get('replay_requested', 0)}(v{metrics.get('replay_valid', 0)}/l{metrics.get('replay_limited', 0)}/fl{metrics.get('replay_failed', 0)}) · gaps {metrics.get('evidence_gaps', 0)} · judge {metrics.get('judge_eligible', 0)}/{metrics.get('judge_rejected', 0)}"
            ]
        else:
            parts = [
                f"{metrics.get('request_count', 0)} 个请求 · {metrics.get('fact_count', 0)} 条事实"
            ]
            if "replay_verified_count" in metrics:
                parts.append(
                    f"(verified {metrics.get('replay_verified_count', 0)} / unverified {metrics.get('replay_unverified_count', 0)} / failed {metrics.get('replay_failed_count', 0)} / recipe {metrics.get('recipe_fact_count', 0)})"
                )
            parts.append(
                f" · {metrics.get('llm_analysis_calls', 0)} 次 LLM · 分析 {float(metrics.get('fact_analysis_ms', 0.0)) / 1000:.3f}s"
            )
            if "chain_used" in metrics:
                parts.append(
                    f" · 链 {metrics.get('chain_used', 0)} / 配方 {metrics.get('recipe_fallback', 0)}"
                )
        summary = "".join(parts)
    return {
        "id": step_id,
        "sequence": sequence,
        "kind": kind,
        "title": "模型决策"
        if kind == "llm"
        else _NODE_TITLES.get(code_name, code_name),
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
        "direct_task_count": _direct_task_count_from_event(end)
        if code_name == "task_route"
        else 0,
    }


def _node_state_summary(code_name: str, event: TraceEvent | None) -> str:
    if event is None:
        return ""
    output = _event_state_write(event)
    if not isinstance(output, dict):
        return ""
    if code_name == "classify_mode":
        route = output.get("review_route")
        if isinstance(route, dict):
            metrics = route.get("metrics")
            metrics = metrics if isinstance(metrics, dict) else {}
            return f"{route.get('initial_mode', 'unknown')} 模式 · {metrics.get('file_count', 0)} 文件 · {metrics.get('hunk_count', 0)} hunks · {metrics.get('diff_chars', 0)} 字符"
    traces = output.get("council_trace")
    if code_name == "controlled_review":
        traces = []
    if isinstance(traces, list):
        for trace in reversed(traces):
            if (
                isinstance(trace, dict)
                and trace.get("node") == code_name
                and str(trace.get("detail") or "").strip()
            ):
                return str(trace["detail"])
    if code_name == "controlled_review":
        return _controlled_review_summary(output)
    return ""


def _controlled_review_summary(output: dict[str, Any]) -> str:
    outcomes = output.get("controlled_subtask_outcomes") or {}
    candidates = output.get("candidate_issues") or []
    return f"变更调查 {len(outcomes)} 组 · 候选 {len(candidates)} · 未完成 {sum((v in {'inconclusive', 'omitted', 'failed'} for v in outcomes.values()))}"


def _evidence_batch_metrics(event: TraceEvent | None) -> dict[str, Any]:
    """从证据验证事件中提取节点摘要，支持两种指标事件的字段结构。"""
    if event is None:
        return {}
    output = _event_state_write(event)
    if not isinstance(output, dict):
        return {}
    traces = output.get("council_trace")
    if not isinstance(traces, list):
        return {}
    for trace in traces:
        if not isinstance(trace, dict) or trace.get("event") not in {
            "evidence_verification_metrics",
            "evidence_batch_metrics",
        }:
            continue
        try:
            detail = json.loads(str(trace.get("detail") or "{}"))
        except (TypeError, json.JSONDecodeError):
            return {}
        return detail if isinstance(detail, dict) else {}
    return {}


def _tool_event_steps(
    events: Iterable[TraceEvent], artifacts: dict[str, Any] | None = None
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
        result.append(_tool_step(event, end, artifacts or {}))
    for event in events:
        if event.event_type not in {"tool_end", "tool_error"} or event.run_id in starts:
            continue
        result.append(_tool_step(None, event, artifacts or {}))
    return result


def _application_tool_steps(
    events: Iterable[TraceEvent],
    native_tool_steps: list[dict[str, Any]],
    artifacts: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """从节点输出恢复未进入 LangChain 事件流的真实工具调用。

    同步嵌套 ReAct 和 EvidenceAgent 直接 HTTP 调用不会总是产生外层
    ``tool_start/tool_end``。它们会把 ``tool_trace_records`` 写入
    节点 output；这里将其提升为与原生工具步骤同构的只读视图记录。
    """
    event_list = list(events)
    events_by_sequence = {event.sequence: event for event in event_list}
    native_keys: set[tuple[str, str, str, str]] = set()
    native_call_ids: set[str] = set()
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
                str(step.get("subtask_id", "")),
            )
        )
        pair_id = str(step.get("pair_id") or "")
        if pair_id:
            native_call_ids.add(pair_id)
    result: list[dict[str, Any]] = []
    for event in event_list:
        if event.event_type != "node_end":
            continue
        output = _event_state_write(event)
        if not isinstance(output, dict):
            continue
        reviewer_root = _reviewer_root_for_event(event)
        council_trace = output.get("council_trace")
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
                subtask_id = str(item.get("subtask_id") or "")
                dedup_key = (
                    reviewer_root,
                    tool_name,
                    json.dumps(arguments, ensure_ascii=False, sort_keys=True),
                    subtask_id,
                )
                if call_id and call_id in native_call_ids:
                    continue
                if (
                    status != "reused"
                    and dedup_key in native_keys
                    and (
                        not item.get("artifact_id")
                        or any(
                            (
                                step.get("artifact_id") == item["artifact_id"]
                                for step in native_tool_steps
                            )
                        )
                    )
                ):
                    continue
                if status != "reused":
                    native_keys.add(dedup_key)
                artifact_id = str(item.get("artifact_id") or "")
                artifact = (artifacts or {}).get(artifact_id)
                preview = (
                    artifact.preview if artifact is not None else item.get("output")
                )
                reused_from_call_id = str(item.get("reused_from_call_id") or "")
                normalized_output = normalize_tool_result(
                    preview, status=status, reused_from_call_id=reused_from_call_id
                )
                result.append(
                    {
                        "id": f"application-tool-record:{event.sequence}:{index}",
                        "sequence": event.sequence,
                        "kind": "tool",
                        "title": "工具调用",
                        "code_name": tool_name,
                        "node_path": f"{event.node_path or event.node_name}/{tool_name}",
                        "reviewer_root": reviewer_root,
                        "subtask_id": str(item.get("subtask_id") or ""),
                        "invocation_id": event.invocation_id,
                        "pair_id": call_id,
                        "start_sequence": None,
                        "end_sequence": None,
                        "duration_ms": max(0.0, float(item.get("duration_ms") or 0.0)),
                        "status": status,
                        "summary": _application_tool_summary(
                            tool_name,
                            normalized_output,
                            status,
                            reused_from_call_id=reused_from_call_id,
                        ),
                        "input": arguments,
                        "output": normalized_output,
                        "artifact_id": artifact_id,
                        "payload_hash": artifact.payload_hash
                        if artifact is not None
                        else "",
                        "reuse_key": str(item.get("reuse_key") or ""),
                        "reused_from_call_id": reused_from_call_id,
                        "reused_from_artifact_id": str(
                            item.get("reused_from_artifact_id") or ""
                        ),
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
            reused_from_call_id = str(detail.get("reused_from_call_id") or "")
            normalized_output = normalize_tool_result(
                detail.get("output"),
                status="reused",
                reused_from_call_id=reused_from_call_id,
            )
            result.append(
                {
                    "id": f"application-tool-reuse:{event.sequence}:{index}",
                    "sequence": event.sequence,
                    "kind": "tool",
                    "title": "工具调用",
                    "code_name": tool_name,
                    "node_path": f"{event.node_path or event.node_name}/{tool_name}",
                    "reviewer_root": reviewer_root,
                    "subtask_id": str(detail.get("subtask_id") or ""),
                    "invocation_id": event.invocation_id,
                    "pair_id": call_id,
                    "start_sequence": None,
                    "end_sequence": None,
                    "duration_ms": 0.0,
                    "status": "reused",
                    "summary": _application_tool_summary(
                        tool_name,
                        normalized_output,
                        "reused",
                        reused_from_call_id=reused_from_call_id,
                    ),
                    "input": detail.get("arguments", {}),
                    "output": normalized_output,
                    "reuse_key": str(detail.get("reuse_key") or ""),
                    "reused_from_call_id": reused_from_call_id,
                    "reused_from_artifact_id": str(
                        detail.get("reused_from_artifact_id") or ""
                    ),
                }
            )
    return result


def _application_tool_summary(
    tool: str, output: Any, status: str, *, reused_from_call_id: str = ""
) -> str:
    if status == "reused":
        if reused_from_call_id == "task_patch":
            return "复用当前 task patch（未执行 Gateway）"
        return "复用已缓存工具结果"
    if tool != "query_relations":
        return f"应用级工具记录 · {status}"
    payload = output
    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        return "图谱协议不兼容"
    relations = payload.get("relationships")
    resolved = len(relations) if isinstance(relations, list) else 0
    return f"图谱查询 · {payload.get('outcome', 'invalid')}/{payload.get('coverage', 'invalid')} · 已解析 {resolved} · 未解析 {int(payload.get('unresolved_count') or 0)}"


def _tool_step(
    start: TraceEvent | None, end: TraceEvent | None, artifacts: dict[str, Any]
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
    artifact_id = str(end.detail.get("artifact_id") or "") if end else ""
    artifact = artifacts.get(artifact_id)
    legacy_output = None
    if end is not None:
        legacy_output = end.detail.get("output", end.detail.get("result"))
    step_status = (
        "failed"
        if failed
        else "complete"
        if start is not None and end is not None
        else "missing"
    )
    return {
        "id": f"tool:{run_id or sequence}",
        "sequence": sequence,
        "kind": "tool",
        "title": "工具调用",
        "code_name": tool_name,
        "node_path": event.node_path or event.node_name,
        "reviewer_root": _reviewer_root_for_event(event),
        "subtask_id": "",
        "invocation_id": event.invocation_id,
        "pair_id": run_id,
        "start_sequence": start.sequence if start is not None else None,
        "end_sequence": end.sequence if end is not None else None,
        "duration_ms": duration_ms,
        "status": step_status,
        "summary": end.summary if end is not None else event.summary,
        "input": start.detail.get("input") if start is not None else None,
        "output": normalize_tool_result(
            artifact.preview if artifact is not None else legacy_output,
            status=step_status,
        ),
        "artifact_id": artifact_id,
        "payload_hash": artifact.payload_hash if artifact is not None else "",
    }


def _reviewer_root_for_event(event: TraceEvent) -> str:
    path_root = str(event.node_path).split("/", 1)[0]
    if path_root == "controlled_review":
        return path_root
    metadata = event.detail.get("metadata")
    if isinstance(metadata, dict):
        namespace = str(metadata.get("langgraph_checkpoint_ns") or "")
        for path_root in ("controlled_review",):
            if path_root in namespace:
                return path_root
    return ""


def _is_visible_node_step(step: dict[str, Any]) -> bool:
    code_name = step["code_name"]
    if code_name in {
        "classify_mode",
        "file_task_builder",
        "diff_task_builder",
        "task_route",
        "direct_task_review",
        "task_selection",
        "symbol_resolution",
        "controlled_review",
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
        output = _event_state_write(event)
        if not isinstance(output, dict) or not output:
            continue
        state_step = dict(step)
        state_step["hidden"] = True
        result.append(state_step)
    return result


def _index_steps(steps: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        step["id"]: step for step in sorted(steps, key=lambda item: item["sequence"])
    }


def _main_stages(
    node_steps: list[dict[str, Any]], *, decision_summary: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    by_name = defaultdict(list)
    for step in node_steps:
        by_name[step["code_name"]].append(step)
    order = (
        "classify_mode",
        "file_task_builder",
        "diff_task_builder",
        "task_route",
        "direct_task_review",
        "task_selection",
        "symbol_resolution",
        "controlled_review",
        "discovery_collector",
        "council_coordinator",
        "evidence_verifier",
        "council_judge",
        "direct_judge",
        "causal_merge",
    )
    stages = [
        _main_stage(name, _NODE_TITLES.get(name, name), by_name[name])
        for name in order
        if name in by_name
    ]
    for stage in stages:
        key = {"council_judge": "judge", "causal_merge": "causal_merge"}.get(
            stage["code_name"]
        )
        data = (decision_summary or {}).get(key or "", {})
        if data and key == "judge":
            stage["summary"] = _judge_summary(data)
        elif data and key == "causal_merge":
            stage["summary"] = _causal_merge_summary(data)
    return stages


def _direct_task_count_from_event(event: TraceEvent | None) -> int:
    output = _event_state_write(event)
    if not isinstance(output, dict):
        return 0
    routes = output.get("task_routes")
    if not isinstance(routes, dict):
        return 0
    return sum(
        (
            isinstance(route, dict) and route.get("route") == "direct"
            for route in routes.values()
        )
    )


def _decision_summary(events: Iterable[TraceEvent]) -> dict[str, Any]:
    """从裁决和因果合并事件提取流程总览统计；详细结论保留在 council_trace 中。"""
    event_list = list(events)
    judge = _judge_summary_data(event_list)
    causal = _causal_merge_summary_data(event_list)
    return {"judge": judge, "causal_merge": causal}


def _latest_node_output(events: Iterable[TraceEvent], node_name: str) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for event in sorted(events, key=lambda item: item.sequence):
        if event.event_type != "node_end" or event.node_name != node_name:
            continue
        candidate = _event_state_write(event)
        if isinstance(candidate, dict):
            output = candidate
    return output


def _council_trace_payloads(output: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    payloads: list[tuple[str, dict[str, Any]]] = []
    traces = output.get("council_trace")
    if not isinstance(traces, list):
        return payloads
    for item in traces:
        if not isinstance(item, dict):
            continue
        event = str(item.get("event") or "")
        detail = item.get("detail")
        if isinstance(detail, dict):
            payloads.append((event, detail))
            continue
        if not isinstance(detail, str):
            continue
        try:
            parsed = json.loads(detail)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            payloads.append((event, parsed))
    return payloads


def _judge_summary_data(events: Iterable[TraceEvent]) -> dict[str, Any]:
    output = _latest_node_output(events, "council_judge")
    if not output:
        output = _latest_node_output(events, "direct_judge")
    payloads = _council_trace_payloads(output)
    verdicts = [
        payload
        for event, payload in payloads
        if event in {"judge_verdict", "direct_judge_verdict"}
    ]
    batch_count = sum(
        (event == "evidence_judge_batch_started" for event, _payload in payloads)
    )
    contract_violations = sum(
        (
            len(payload.get("violations") or [])
            for event, payload in payloads
            if event == "evidence_judge_contract_violations"
        )
    )
    keep_count = sum((payload.get("action") == "keep" for payload in verdicts))
    drop_count = sum((payload.get("action") == "drop" for payload in verdicts))
    insufficient_count = sum(
        (payload.get("reason_code") == "insufficient_evidence" for payload in verdicts)
    )
    failed_count = sum(
        (payload.get("reason_code") == "verification_failed" for payload in verdicts)
    )
    return {
        "candidate_count": len(verdicts),
        "keep_count": keep_count,
        "drop_count": drop_count,
        "insufficient_evidence_count": insufficient_count,
        "verification_failed_count": failed_count,
        "contract_violation_count": contract_violations,
        "batch_count": batch_count,
        "final_issue_count": len(output.get("final_issues") or []),
    }


def _causal_merge_summary_data(events: Iterable[TraceEvent]) -> dict[str, Any]:
    output = _latest_node_output(events, "causal_merge")
    stats = output.get("causal_merge_stats")
    if not isinstance(stats, dict):
        stats = {}
    completed = next(
        (
            payload
            for event, payload in reversed(_council_trace_payloads(output))
            if event == "causal_merge_batch_completed"
        ),
        {},
    )
    judge = _judge_summary_data(events)
    return {
        "survivor_count": judge.get("keep_count", 0),
        "batch_count": int(stats.get("batch_count", 0)),
        "successful_batch_count": int(stats.get("successful_batch_count", 0)),
        "failed_batch_count": int(stats.get("failed_batch_count", 0)),
        "comparison_count": sum(
            (
                int(payload.get("comparisons", 0))
                for event, payload in _council_trace_payloads(output)
                if event == "causal_merge_batch_completed"
            )
        )
        or int(completed.get("comparisons", 0)),
        "merged_group_count": int(stats.get("merged_group_count", 0)),
        "merged_candidate_count": int(stats.get("merged_candidate_count", 0)),
        "final_issue_count": len(output.get("final_issues") or []),
    }


def _judge_summary(data: dict[str, Any]) -> str:
    text = f"Judge {data.get('candidate_count', 0)} 个候选 → 保留 {data.get('keep_count', 0)} / 丢弃 {data.get('drop_count', 0)}"
    reasons: list[str] = []
    if data.get("insufficient_evidence_count"):
        reasons.append(f"证据不足 {data['insufficient_evidence_count']}")
    if data.get("verification_failed_count"):
        reasons.append(f"失败 {data['verification_failed_count']}")
    if data.get("contract_violation_count"):
        reasons.append(f"合同违规 {data['contract_violation_count']}")
    return f"{text}（{'，'.join(reasons)}）" if reasons else text


def _causal_merge_summary(data: dict[str, Any]) -> str:
    return f"因果合并 {data.get('survivor_count', 0)} 个 survivor → 比较 {data.get('comparison_count', 0)} 次 → 合并 {data.get('merged_group_count', 0)} 组 → 最终 {data.get('final_issue_count', 0)} 个 Issue"


def _main_stage(
    code_name: str, title: str, candidates: list[dict[str, Any]] | None
) -> dict[str, Any]:
    step = candidates[0] if candidates else None
    return {
        "id": f"main:{code_name}",
        "title": title,
        "code_name": code_name,
        "status": step["status"] if step is not None else "missing",
        "step_id": step["id"] if step is not None else None,
        "sequence": step["sequence"] if step is not None else 0,
        "duration_ms": step["duration_ms"] if step is not None else 0.0,
        "summary": step["summary"] if step is not None else "未采集到该节点",
    }


def _controlled_sections(
    steps: dict[str, dict[str, Any]],
    events: Iterable[TraceEvent],
    artifacts: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    output = _latest_node_output(events, "controlled_review")
    plans = output.get("controlled_subtask_plans") or {}
    outcomes = output.get("controlled_subtask_outcomes") or {}
    parents = [
        step for step in steps.values() if step.get("code_name") == "controlled_review"
    ]
    if not parents:
        return []
    parent = max(
        parents, key=lambda step: step.get("end_sequence") or step.get("sequence") or 0
    )
    ids = []
    for plan_key, plan in plans.items():
        task_id = plan.get("task_id", plan_key)
        for group in plan.get("subtasks", []):
            key = f"{task_id}:{group['subtask_id']}"
            step_id = f"investigation:{key}"
            steps[step_id] = dict(
                id=step_id,
                sequence=parent.get("sequence", 0),
                kind="controlled",
                task_id=task_id,
                subtask_id=group["subtask_id"],
                title="变更调查",
                code_name="investigation",
                node_path="controlled_review/investigation",
                reviewer_root="controlled_review",
                invocation_id=parent.get("invocation_id", ""),
                pair_id="",
                start_sequence=None,
                end_sequence=None,
                duration_ms=0,
                status=outcomes.get(key, "inconclusive"),
                summary=outcomes.get(key, "inconclusive"),
                metrics={},
                input=group,
                state_refs=[
                    dict(
                        sequence=parent.get("end_sequence"),
                        field="controlled_subtask_results",
                        key=key,
                    ),
                    dict(
                        sequence=parent.get("end_sequence"),
                        field="controlled_subtask_reasons",
                        key=key,
                    ),
                ],
            )
            ids.append(step_id)
    tool_ids = [
        key
        for key, step in steps.items()
        if step.get("kind") == "tool"
        and "controlled_review" in str(step.get("node_path", ""))
    ]
    return [
        dict(
            key="controlled_behavior",
            title="统一审查员",
            code_name="ChangeReviewer",
            path_root="controlled_review",
            mode="controlled",
            step_ids=ids + tool_ids,
            tool_step_ids=tool_ids,
            tool_call_count=len(tool_ids),
            task_count=len(plans),
        )
    ]


def _state_writes(
    steps: dict[str, dict[str, Any]], events_by_sequence: dict[int, TraceEvent]
) -> dict[str, list[dict[str, Any]]]:
    writes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for step in steps.values():
        if step["kind"] != "node" or step["end_sequence"] is None:
            continue
        event = events_by_sequence[step["end_sequence"]]
        output = _event_state_write(event)
        if not isinstance(output, dict):
            continue
        for field_name in output:
            writes[str(field_name)].append(
                {
                    "step_id": step["id"],
                    "sequence": step["sequence"],
                    "node_path": step["node_path"],
                    "semantics": "state_patch",
                }
            )
    return dict(writes)


def _integrity(events: Iterable[TraceEvent]) -> dict[str, Any]:
    event_list = list(events)
    starts = {
        event.run_id for event in event_list if event.event_type.endswith("_start")
    }
    ends = {
        event.run_id
        for event in event_list
        if event.event_type.endswith("_end") or event.event_type == "tool_error"
    }
    missing_end = starts - ends
    missing_start = ends - starts
    unassociated = [
        event
        for event in event_list
        if (event.node_path or event.node_name) in {"", "unknown"}
    ]
    is_complete = not missing_end and (not missing_start) and (not unassociated)
    return {
        "status": "complete" if is_complete else "incomplete",
        "event_count": len(event_list),
        "missing_end_count": len(missing_end),
        "missing_start_count": len(missing_start),
        "unassociated_count": len(unassociated),
    }
