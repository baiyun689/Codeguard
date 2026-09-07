"""Deterministic grouping for bounded subtask React investigations.

DirectTriage runs once per reviewer, so the same change can produce several
worded-but-equivalent investigation seeds.  This module groups only seeds that
share the same concrete navigation anchor and evidence direction.  It never
decides whether a bug exists; it only prevents duplicate investigations from
consuming the subtask and tool budgets.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable, Mapping

from codeguard_agent.models.tasks import EvidenceNeed, InvestigationSeed, ReviewerKind


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
    """Return a semantic navigation key, not merely a tool name.

    ``inspect_path`` serves two different contracts (behavior and security),
    and direction is equally important.  Keeping those dimensions in the key
    prevents a security seed from being executed under a behavior
    representative after cross-reviewer de-duplication.
    """

    if seed.evidence_need is EvidenceNeed.INSPECT_CHANGE_IMPACT:
        direction = seed.direction or "upstream"
        return f"{seed.path_kind or 'unspecified'}:{direction}"
    if seed.evidence_need is EvidenceNeed.INSPECT_PATH:
        direction = seed.direction or "downstream"
        return f"{seed.path_kind or 'unspecified'}:{direction}"
    if seed.evidence_need is EvidenceNeed.INSPECT_STRUCTURE:
        return f"structure:{seed.path_kind or 'unspecified'}"
    # ``DOMAIN_TOOL`` and provider-compatible omissions are not safely
    # interchangeable with a graph direction.  Keep them separate.
    return ",".join(sorted(seed.allowed_tools)) or "unspecified"


def _key(seed: InvestigationSeed) -> tuple[str, str, int, tuple[str, ...], str, str]:
    """Return a conservative, provider-independent merge key.

    Same line + same resolved symbol + same navigation family is deliberately
    required.  This avoids collapsing two independent hypotheses that happen
    to mention the same method but originate from different changed lines.
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

    buckets: dict[tuple[str, str, int, tuple[str, ...], str, str], list[InvestigationSeed]] = {}
    for reviewer in sorted(
        seeds_by_reviewer,
        key=lambda item: _REVIEWER_PRIORITY.get(item, 99),
    ):
        for seed in seeds_by_reviewer.get(reviewer, ()):
            if not seed.seed_id:
                continue
            buckets.setdefault(_key(seed), []).append(seed)

    groups: list[InvestigationSeedGroup] = []
    for key, bucket in buckets.items():
        ordered = sorted(
            bucket,
            key=lambda seed: (
                -seed.confidence,
                _REVIEWER_PRIORITY.get(seed.reviewer, 99),
                seed.seed_id,
            ),
        )
        representative = ordered[0]
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
