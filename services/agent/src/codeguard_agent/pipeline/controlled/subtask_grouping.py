"""Deterministic grouping for bounded subtask React investigations.

DirectTriage runs once per reviewer, so the same change can produce several
worded-but-equivalent investigation seeds.  This module groups only seeds that
share the same concrete change anchor and investigation family.  A group may
combine different graph/source preferences because those are capabilities of
one React, not separate investigations.  It never decides whether a bug
exists; it only prevents duplicate investigations from consuming the subtask
and tool budgets.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Iterable, Mapping

from codeguard_agent.models.tasks import EvidenceNeed, InvestigationSeed, ReviewerKind
from codeguard_agent.pipeline.controlled.subtask_capabilities import (
    required_graph_tool,
)


@dataclass(frozen=True)
class InvestigationSeedGroup:
    """One executable investigation plus its triage provenance."""

    seed: InvestigationSeed
    seed_ids: tuple[str, ...]
    reviewers: tuple[ReviewerKind, ...]


_REVIEWER_PRIORITY = {
    ReviewerKind.BEHAVIOR: 0,
    ReviewerKind.THREAT_MODEL: 1,
    ReviewerKind.MAINTAINABILITY: 2,
}


def _path(value: str) -> str:
    return str(PurePosixPath(value.replace("\\", "/"))).lower()


def _tool_family(seed: InvestigationSeed) -> str:
    """Return the investigation family, not the first tool to call.

    A single investigation may need a graph lookup followed by a source read.
    Using the individual ``evidence_need`` or ``allowed_tools`` as the merge
    key therefore creates disconnected ``graph-only`` and ``source-only``
    subtasks for the same change.  Security remains a separate family because
    its path semantics are intentionally different; all other navigation
    variants can share one coherent behavior investigation and combine their
    capabilities during the merge.
    """

    # ``path_kind`` is meaningful only for the security/behavior variants of
    # ``inspect_path``.  Threat-model triage can still emit a neutral
    # structure/source or upstream-impact seed with a copied security hint;
    # treating that hint as a separate family would split one investigation
    # into needless reviewer-specific subtasks.
    if (
        required_graph_tool(seed) == "inspect_path"
        and seed.path_kind == "security"
    ):
        return "security"
    return "behavior"


def _graph_contract(seed: InvestigationSeed) -> tuple[str | None, str | None, str | None]:
    """Return ``(directional_tool, path_kind, direction)`` for compatibility.

    ``inspect_structure`` is neutral and may accompany one directional query;
    ``inspect_path`` and ``inspect_change_impact`` are mutually exclusive.
    A seed with only a source reader is a neutral companion and can join a
    behavior investigation, but never a security path investigation.
    """

    required = required_graph_tool(seed)
    directional = required if required in {
        "inspect_path",
        "inspect_change_impact",
    } else None
    if directional is None and required is None:
        requested = set(seed.allowed_tools)
        candidates = requested & {"inspect_path", "inspect_change_impact"}
        if len(candidates) == 1:
            directional = next(iter(candidates))
    path_kind = seed.path_kind if directional == "inspect_path" else None
    direction = seed.direction if directional is not None else None
    return directional, path_kind, direction


_QUESTION_STOPWORDS = frozenset({
    "是否", "检查", "确认", "当前", "相关", "变更", "影响", "行为", "实现",
    "调用", "方法", "字段", "逻辑", "需要", "可能", "导致", "之后", "情况",
})


def _question_terms(seed: InvestigationSeed) -> frozenset[str]:
    """Extract stable terms for rejecting unrelated same-line questions."""

    text = f"{seed.observed_change} {seed.investigation_question}".lower()
    terms: set[str] = set()
    for token in re.findall(r"[a-z][a-z0-9_.#:-]{1,}|[\u4e00-\u9fff]{2}", text):
        if token not in _QUESTION_STOPWORDS:
            terms.add(token)
    return frozenset(terms)


def _contracts_compatible(
    left: InvestigationSeed,
    right: InvestigationSeed,
) -> bool:
    """Keep only investigations that can share one directional React."""

    left_tool, left_kind, left_direction = _graph_contract(left)
    right_tool, right_kind, right_direction = _graph_contract(right)

    if left_tool and right_tool and left_tool != right_tool:
        return False
    # An explicit security path is never merged with an unspecified or
    # behavior path.  A neutral structure/source seed may join behavior, but
    # keeping it separate from security avoids changing security semantics.
    if left_kind == "security" or right_kind == "security":
        if left_kind != right_kind:
            return False
    elif left_kind and right_kind and left_kind != right_kind:
        return False
    # Both directional queries must point in the same direction.  Neutral
    # structure/source companions do not impose a direction.
    if left_tool and right_tool:
        if left_direction and right_direction and left_direction != right_direction:
            return False
        if (left_kind is None) != (right_kind is None):
            return False
    if (
        left.risk_dimension.strip()
        and right.risk_dimension.strip()
        and left.risk_dimension.strip().lower()
        != right.risk_dimension.strip().lower()
    ):
        return False
    left_terms = _question_terms(left)
    right_terms = _question_terms(right)
    if left_terms and right_terms and not (left_terms & right_terms):
        return False
    return True


def _merged_evidence_need(seeds: tuple[InvestigationSeed, ...]) -> EvidenceNeed:
    """Choose the graph capability retained by a compatible group."""

    required = {
        required_graph_tool(seed)
        for seed in seeds
    }
    if "inspect_change_impact" in required:
        return EvidenceNeed.INSPECT_CHANGE_IMPACT
    if "inspect_path" in required:
        return EvidenceNeed.INSPECT_PATH
    if "inspect_structure" in required:
        return EvidenceNeed.INSPECT_STRUCTURE
    return EvidenceNeed.NONE


def _merged_direction(
    seeds: tuple[InvestigationSeed, ...],
    evidence_need: EvidenceNeed,
) -> str | None:
    """Keep the direction that matches the merged graph capability.

    An omitted or contradictory direction is common in provider output.  For
    structure investigations an upstream direction is the useful default for
    parent/field initialization questions; path and impact investigations use
    their normal downstream/upstream defaults.
    """

    if evidence_need not in {
        EvidenceNeed.INSPECT_PATH,
        EvidenceNeed.INSPECT_CHANGE_IMPACT,
    }:
        return None
    directions = [
        seed.direction
        for seed in seeds
        if _graph_contract(seed)[0] is not None and seed.direction
    ]
    return directions[0] if directions else None


def _key(seed: InvestigationSeed) -> tuple[str, str, int, tuple[str, ...], str, str]:
    """Return a conservative, provider-independent merge key.

    Same line + same resolved symbol + same investigation family is
    deliberately required.  Compatible graph capabilities are merged inside
    that anchor so one React can move between graph lookup and source reading.
    Opposite directions, security/behavior paths, and different changed lines
    remain separate.
    """

    line = seed.location_line if seed.location_line > 0 else 0
    # A zero line means SymbolResolution could not bind a precise changed
    # anchor.  In that case require a stable textual fingerprint as well, so
    # two unrelated questions about the same type are not merged merely because
    # they share the same symbol and tool family.
    text_fingerprint = ""
    if line == 0:
        text_fingerprint = " ".join(
            f"{seed.observed_change} {seed.investigation_question}".split()
        ).lower()[:160]
    return (
        _path(seed.location_file),
        seed.change_unit_id,
        line,
        tuple(sorted(set(seed.initial_symbol_ids))),
        _tool_family(seed),
        text_fingerprint,
    )


def _short_merge_text(values: Iterable[str], *, limit: int = 420) -> str:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value).split()).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    merged = "；".join(unique)
    return merged[:limit]


def group_investigation_seeds(
    seeds_by_reviewer: Mapping[ReviewerKind, Iterable[InvestigationSeed]],
) -> tuple[InvestigationSeedGroup, ...]:
    """Merge duplicate neutral seeds across the three fixed reviewers.

    The representative is selected by confidence, then reviewer priority, and
    finally seed ID.  Its identity remains stable, while the explanatory text
    and allowed symbols/tools are merged conservatively.  Provenance is
    returned separately for trace rendering and does not enter the LLM-facing
    candidate protocol.
    """

    buckets: dict[
        tuple[str, str, int, tuple[str, ...], str, str],
        list[list[InvestigationSeed]],
    ] = {}
    for reviewer in sorted(
        seeds_by_reviewer,
        key=lambda item: _REVIEWER_PRIORITY.get(item, 99),
    ):
        for seed in seeds_by_reviewer.get(reviewer, ()):
            if not seed.seed_id:
                continue
            base = buckets.setdefault(_key(seed), [])
            compatible_group = next(
                (group for group in base if _contracts_compatible(group[0], seed)),
                None,
            )
            if compatible_group is None:
                base.append([seed])
            else:
                compatible_group.append(seed)

    groups: list[InvestigationSeedGroup] = []
    for buckets_for_anchor in buckets.values():
        for bucket in buckets_for_anchor:
            ordered = sorted(
                bucket,
                key=lambda seed: (
                    -seed.confidence,
                    _REVIEWER_PRIORITY.get(seed.reviewer, 99),
                    seed.seed_id,
                ),
            )
            representative = ordered[0]
            merged_need = _merged_evidence_need(tuple(ordered))
            symbol_ids = tuple(dict.fromkeys(
                symbol_id
                for seed in ordered
                for symbol_id in seed.initial_symbol_ids
            ))[:4]
            allowed_tools = tuple(dict.fromkeys(
                tool
                for seed in ordered
                for tool in seed.allowed_tools
            ))[:3]
            merged = representative.model_copy(
                update={
                    "initial_symbol_ids": symbol_ids,
                    "allowed_tools": allowed_tools,
                    "evidence_need": merged_need,
                    "direction": _merged_direction(tuple(ordered), merged_need),
                    "path_kind": (
                        "security"
                        if merged_need is EvidenceNeed.INSPECT_PATH
                        and any(seed.path_kind == "security" for seed in ordered)
                        else "behavior"
                        if merged_need is EvidenceNeed.INSPECT_PATH
                        and any(seed.path_kind == "behavior" for seed in ordered)
                        else None
                    ),
                    "observed_change": _short_merge_text(
                        seed.observed_change for seed in ordered
                    ) or representative.observed_change,
                    "investigation_question": _short_merge_text(
                        seed.investigation_question for seed in ordered
                    ) or representative.investigation_question,
                    "confidence": max(seed.confidence for seed in ordered),
                }
            )
            groups.append(InvestigationSeedGroup(
                seed=merged,
                seed_ids=tuple(seed.seed_id for seed in ordered),
                reviewers=tuple(dict.fromkeys(seed.reviewer for seed in ordered)),
            ))

    groups.sort(key=lambda group: (
        _path(group.seed.location_file),
        group.seed.location_line if group.seed.location_line > 0 else 10**9,
        _REVIEWER_PRIORITY.get(group.seed.reviewer, 99),
        group.seed.seed_id,
    ))
    return tuple(groups)


__all__ = ["InvestigationSeedGroup", "group_investigation_seeds"]
