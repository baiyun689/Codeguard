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
    # DirectTriage is intentionally allowed to name a target by its stable
    # symbol id *or* by a precise class/method name.  Gateway responses always
    # use stable ids, so resolve the latter aliases against the ids that are
    # actually reachable in this projection.  This keeps the LLM from having
    # to invent a full Java signature while retaining a deterministic proof
    # boundary (an alias must match a reachable symbol, never an omitted/raw
    # symbol).
    target_matches = _resolve_target_aliases(expected_targets, reached)
    matched_targets = tuple(
        sorted(
            actual
            for aliases in target_matches.values()
            for actual in aliases
        )
    )
    matched_target_ids = {
        actual for aliases in target_matches.values() for actual in aliases
    }
    required_kinds = {str(item).upper() for item in question.required_relationships if item}
    target_path_kinds = _path_kinds_to_targets(
        directed,
        root=subject_symbol_id or actual_subject,
        targets=matched_target_ids,
        max_depth=question.max_depth,
    )
    relevant_kinds = (
        set().union(*(target_path_kinds.get(target, set()) for target in matched_target_ids))
        if expected_targets
        else traversed_kinds
    )
    matched_kinds = tuple(sorted(required_kinds & relevant_kinds))
    condition_met = (
        (all(alias in target_matches for alias in expected_targets) if expected_targets else True)
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
    elif outcome == "found" and incomplete and relationships:
        # A partial projection can still carry a real positive relationship
        # even when the provider's expected-target list is incomplete or
        # names an endpoint that was omitted from this bounded view.  Keep
        # that fact as a partial proof so EvidenceAssessment/Judge can weigh
        # it together with the local source; treating it as indeterminate at
        # this boundary loses recall before any semantic decision is possible.
        status = ProofMatchStatus.PARTIAL
        limitations.append("proof_positive_relation_target_unresolved")
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


def _resolve_target_aliases(
    aliases: set[str], reached: set[str]
) -> dict[str, tuple[str, ...]]:
    """Resolve model-friendly class/method names to reached stable symbol ids.

    An alias is accepted only when it denotes the complete method name (the
    part after ``#`` and before the signature), the declaring class name, or a
    fully-qualified id suffix.  Short role/type aliases are also accepted when
    they are the final CamelCase token of a declaring class (for example,
    ``listener`` resolves ``RetryListener``).  This is a token-boundary match,
    not arbitrary substring matching, so names such as ``open`` cannot
    accidentally match unrelated symbols.
    """

    result: dict[str, tuple[str, ...]] = {}
    for alias in aliases:
        wanted = alias.strip()
        if not wanted:
            continue
        wanted_lower = wanted.lower()
        matches: list[str] = []
        for symbol_id in reached:
            actual = symbol_id.strip()
            actual_lower = actual.lower()
            if actual_lower == wanted_lower or actual_lower.endswith(wanted_lower):
                matches.append(actual)
                continue
            qualified = actual.split(":", 1)[-1]
            declaring, separator, member = qualified.partition("#")
            class_name = declaring.rsplit(".", 1)[-1]
            method_name = member.split("(", 1)[0] if separator else ""
            if wanted_lower in {class_name.lower(), method_name.lower()}:
                matches.append(actual)
                continue
            # Providers often use a semantic role (``listener``, ``callback``,
            # ``cache``) instead of inventing a full Java type name.  Resolve
            # only a final CamelCase token, requiring the token to begin at an
            # uppercase boundary and to be at least four characters long.  The
            # minimum keeps generic one/two-letter aliases from becoming broad
            # substring searches while still covering common type roles.
            if (
                len(wanted_lower) >= 4
                and len(class_name) > len(wanted)
                and class_name[-len(wanted)].isupper()
                and class_name[-len(wanted) :].lower() == wanted_lower
            ):
                matches.append(actual)
        if matches:
            result[wanted] = tuple(sorted(set(matches)))
    return result


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
