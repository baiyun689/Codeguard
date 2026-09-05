"""受控模式的候选路由和计划引用校验。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Literal

from codeguard_agent.models.tasks import (
    CandidateSeed,
    CoverageDecision,
    DirectTriageResult,
    EvidenceNeed,
    GraphQuestion,
    ProofScope,
)

CandidateRoute = Literal["direct_proven", "graph_required", "unresolved"]


def stable_seed_id(seed: CandidateSeed, *, ordinal: int = 1) -> str:
    """为候选生成稳定、与 LLM 编号无关的 ID。"""

    normalized = "\x00".join(
        (
            seed.reviewer.value,
            seed.change_unit_id,
            seed.location_file.replace("\\", "/"),
            str(seed.location_line),
            " ".join(seed.claim.split()),
            str(ordinal),
        )
    )
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"seed-{seed.reviewer.value}-{digest}"


def bind_seed_ids(result: DirectTriageResult) -> DirectTriageResult:
    """给 LLM 结果绑定稳定 ID，并保留输入顺序。"""

    counters: dict[tuple[str, str], int] = {}
    bound: list[CandidateSeed] = []
    for seed in result.issues:
        key = (seed.reviewer.value, seed.change_unit_id)
        counters[key] = counters.get(key, 0) + 1
        bound.append(
            seed.model_copy(
                update={"seed_id": stable_seed_id(seed, ordinal=counters[key])}
            )
        )
    return result.model_copy(update={"issues": tuple(bound)})


def route_seed(seed: CandidateSeed) -> CandidateRoute:
    """按主张范围和证据需求路由，不使用 confidence。"""

    if seed.proof_scope is ProofScope.LOCAL and seed.evidence_need is EvidenceNeed.NONE:
        if set(seed.evidence_basis) & {
            "changed_lines",
            "deletion_patch",
            "local_source",
        }:
            return "direct_proven"
        return "unresolved"
    if seed.graph_question is None:
        return "unresolved"
    return "graph_required"


def validate_graph_question(question: GraphQuestion) -> tuple[str, ...]:
    """校验图谱问题的结构性约束。"""

    diagnostics: list[str] = []
    if question.direction == "downstream" and question.path_kind is None:
        diagnostics.append("downstream_graph_question_requires_path_kind")
    if question.direction == "upstream" and question.path_kind is not None:
        diagnostics.append("upstream_graph_question_does_not_accept_path_kind")
    if not question.expected_targets and not question.required_relationships:
        diagnostics.append("graph_question_requires_target_or_relationship")
    return tuple(diagnostics)


def validate_coverage(
    *,
    change_unit_ids: Iterable[str],
    result: DirectTriageResult,
) -> tuple[str, ...]:
    """确保每个 ChangeUnit 都得到显式覆盖声明。"""

    expected = {item for item in change_unit_ids if item}
    declarations = list(result.coverage)
    diagnostics: list[str] = []
    seen: set[str] = set()
    for declaration in declarations:
        if declaration.change_unit_id in seen:
            diagnostics.append(f"duplicate_coverage:{declaration.change_unit_id}")
        seen.add(declaration.change_unit_id)
        if declaration.change_unit_id not in expected:
            diagnostics.append(f"unknown_change_unit:{declaration.change_unit_id}")
        if declaration.decision is CoverageDecision.GRAPH_NEEDED and not declaration.reason.strip():
            diagnostics.append(f"graph_coverage_missing_reason:{declaration.change_unit_id}")
    for change_unit_id in sorted(expected - seen):
        diagnostics.append(f"missing_coverage:{change_unit_id}")
    return tuple(diagnostics)


def validate_seed(seed: CandidateSeed, *, change_unit_ids: set[str]) -> tuple[str, ...]:
    """校验一个 CandidateSeed 是否可以进入路由。"""

    diagnostics: list[str] = []
    if seed.change_unit_id not in change_unit_ids:
        diagnostics.append(f"unknown_change_unit:{seed.change_unit_id}")
    if seed.proof_scope is ProofScope.LOCAL:
        if seed.graph_question is not None:
            diagnostics.append("local_seed_must_not_have_graph_question")
        if seed.evidence_need is not EvidenceNeed.NONE:
            diagnostics.append("local_seed_must_not_require_graph_evidence")
    if seed.proof_scope is not ProofScope.LOCAL:
        if seed.graph_question is None:
            diagnostics.append("non_local_seed_requires_graph_question")
        else:
            diagnostics.extend(validate_graph_question(seed.graph_question))
    return tuple(diagnostics)


def normalize_claim(value: str) -> str:
    """用于候选去重的轻量规范化，不参与语义判断。"""

    return re.sub(r"\s+", " ", value.strip().lower())


__all__ = [
    "CandidateRoute",
    "bind_seed_ids",
    "normalize_claim",
    "route_seed",
    "stable_seed_id",
    "validate_coverage",
    "validate_graph_question",
    "validate_seed",
]
