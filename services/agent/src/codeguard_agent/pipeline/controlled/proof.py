"""图谱结果的确定性证明匹配。"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping

from codeguard_agent.models.tasks import GraphQuestion, ProofMatch, ProofMatchStatus


def match_graph_proof(
    *,
    work_item_id: str,
    payload: str,
    question: GraphQuestion,
    subject_symbol_id: str,
) -> ProofMatch:
    """根据 GraphQuestion 检查已投影的图谱 JSON 是否满足事实条件。"""

    try:
        data = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ProofMatch(work_item_id=work_item_id, status=ProofMatchStatus.INDETERMINATE, limitations=("graph_payload_unparseable",))
    if not isinstance(data, dict) or data.get("schema_version") != 2:
        return ProofMatch(work_item_id=work_item_id, status=ProofMatchStatus.INDETERMINATE, limitations=("graph_schema_invalid",))
    coverage = str(data.get("coverage", "")).lower()
    outcome = str(data.get("outcome", "")).lower()
    if coverage not in {"complete", "partial"} or outcome not in {
        "found", "not_found", "indeterminate", "error"
    }:
        return ProofMatch(
            work_item_id=work_item_id,
            status=ProofMatchStatus.INDETERMINATE,
            limitations=("graph_result_contract_invalid",),
        )
    actual_subject = str(data.get("subject_symbol_id", ""))
    if subject_symbol_id and actual_subject != subject_symbol_id:
        return ProofMatch(work_item_id=work_item_id, status=ProofMatchStatus.INDETERMINATE, limitations=("graph_subject_mismatch",))

    relationships = [
        item for item in data.get("relationships") or [] if isinstance(item, Mapping)
    ]
    edges = [
        (str(item.get("sourceId", "")), str(item.get("targetId", "")), str(item.get("kind", "")).upper())
        for item in relationships
    ]
    directed = edges if question.direction == "downstream" else [
        (target, source, kind) for source, target, kind in edges
    ]
    reached, traversed_kinds = _bounded_reachability(
        directed, root=subject_symbol_id or actual_subject, max_depth=question.max_depth
    )
    expected_targets = {str(item) for item in question.expected_targets if item}
    matched_targets = tuple(sorted(expected_targets & reached))
    required_kinds = {str(item).upper() for item in question.required_relationships if item}
    target_path_kinds = _path_kinds_to_targets(
        directed,
        root=subject_symbol_id or actual_subject,
        targets=expected_targets,
        max_depth=question.max_depth,
    )
    relevant_kinds = (
        set().union(*(target_path_kinds.get(target, set()) for target in expected_targets))
        if expected_targets
        else traversed_kinds
    )
    matched_kinds = tuple(sorted(required_kinds & relevant_kinds))
    condition_met = (
        (expected_targets <= reached if expected_targets else True)
        and (required_kinds <= relevant_kinds if required_kinds else True)
    )
    limitations = [
        str(item) for item in data.get("limitations") or []
        if isinstance(item, str) and item
    ]
    incomplete = (
        str(data.get("coverage", "complete")).lower() != "complete"
        or bool(data.get("unresolved_count"))
        or "projection_truncated" in limitations
        or bool(data.get("omitted_count"))
        or bool(data.get("omitted_path_count"))
    )
    if outcome == "found" and condition_met and not incomplete:
        status = ProofMatchStatus.PROVED
    elif outcome == "found" and condition_met:
        status = ProofMatchStatus.PARTIAL
        limitations.append("proof_condition_found_in_partial_graph")
    elif incomplete:
        status = ProofMatchStatus.INDETERMINATE
        limitations.append("proof_condition_not_decidable_from_partial_graph")
    elif outcome in {"error", "indeterminate"}:
        status = ProofMatchStatus.INDETERMINATE
    else:
        status = ProofMatchStatus.NOT_FOUND
    return ProofMatch(
        work_item_id=work_item_id,
        status=status,
        matched_targets=matched_targets,
        matched_relationships=matched_kinds,
        limitations=tuple(dict.fromkeys(limitations)),
    )


def _bounded_reachability(
    edges: list[tuple[str, str, str]], *, root: str, max_depth: int
) -> tuple[set[str], set[str]]:
    adjacency: dict[str, list[tuple[str, str]]] = {}
    for source, target, kind in edges:
        if source and target:
            adjacency.setdefault(source, []).append((target, kind))
    reached = {root} if root else set()
    kinds: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(root, 0)]) if root else deque()
    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for target, kind in adjacency.get(current, ()):
            kinds.add(kind)
            if target in reached:
                continue
            reached.add(target)
            queue.append((target, depth + 1))
    return reached, kinds


def _path_kinds_to_targets(
    edges: list[tuple[str, str, str]],
    *,
    root: str,
    targets: set[str],
    max_depth: int,
) -> dict[str, set[str]]:
    """收集从 root 到每个目标的完整路径关系类型。

    证明条件不能把一条分支上的目标与另一条分支上的关系类型拼成
    “同一条路径”的假证据；路径长度有界且按节点防环。
    """

    if not root or not targets:
        return {}
    adjacency: dict[str, list[tuple[str, str]]] = {}
    for source, target, kind in edges:
        if source and target:
            adjacency.setdefault(source, []).append((target, kind))
    found: dict[str, set[str]] = {}
    stack: list[tuple[str, int, frozenset[str], frozenset[str]]] = [
        (root, 0, frozenset({root}), frozenset())
    ]
    while stack:
        current, depth, visited, kinds = stack.pop()
        if current in targets and current != root:
            found.setdefault(current, set()).update(kinds)
        if depth >= max_depth:
            continue
        for target, kind in adjacency.get(current, ()):
            if target in visited:
                continue
            stack.append(
                (
                    target,
                    depth + 1,
                    visited | {target},
                    kinds | {kind},
                )
            )
    return found


__all__ = ["match_graph_proof"]
