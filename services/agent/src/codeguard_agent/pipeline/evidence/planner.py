"""候选证据主题到 EvidenceRequest 的纯规划层。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from codeguard_agent.models.council import (
    CandidateClaim,
    CandidateConcern,
    CandidateIssue,
    ConcernEvidencePlan,
    EvidenceFactType,
    EvidenceGoal,
    EvidenceNote,
    EvidencePolarity,
    EvidencePurpose,
    EvidenceRequest,
)
from codeguard_agent.models.tasks import ReviewTask, RiskProfile, RiskTag, TaskContextBundle
from codeguard_agent.pipeline.risk import task_prep
from codeguard_agent.pipeline.evidence.capability import capabilities_for_fact_type
from codeguard_agent.pipeline.evidence.rules import (
    CandidateTagResolution,
    EvidenceStrategy,
    resolve_candidate_tags,
    strategies_for,
)
from codeguard_agent.pipeline.evidence.rules.types import (
    CAPABILITY_TO_TOOL,
    EvidenceCapability,
)

if TYPE_CHECKING:
    from codeguard_agent.pipeline.council.dedup import CandidateGroup


MAX_INITIAL_REQUESTS_PER_CANDIDATE = 4


@dataclass(frozen=True)
class CandidateDossier:
    """规划单个候选所需的只读快照，不进入 graph State。"""

    candidate: CandidateIssue
    task: ReviewTask
    risk_profile: RiskProfile | None
    context_bundle: TaskContextBundle | None
    requests: tuple[EvidenceRequest, ...]
    notes: tuple[EvidenceNote, ...]
    candidate_group: CandidateGroup | None = None


@dataclass(frozen=True)
class CandidateBindingFailure:
    """无法安全绑定到唯一 task 的候选。"""

    candidate: CandidateIssue
    reason: str


@dataclass(frozen=True)
class DossierAssembly:
    """按候选稳定顺序组装的有效 dossier 与显式失败。"""

    dossiers: tuple[CandidateDossier, ...]
    failures: tuple[CandidateBindingFailure, ...]
    trace: tuple[tuple[str, str], ...]


@dataclass
class EvidencePlan:
    """本轮新增请求与可机器解析的规划 trace。"""

    requests: list[EvidenceRequest] = field(default_factory=list)
    trace: list[tuple[str, str]] = field(default_factory=list)


def _stable_json(detail: dict[str, object]) -> str:
    return json.dumps(detail, ensure_ascii=False, sort_keys=True)


def assemble_dossiers(
    candidates: Sequence[CandidateIssue],
    tasks: Sequence[ReviewTask],
    profiles: Mapping[str, RiskProfile | None],
    bundles: Mapping[str, TaskContextBundle],
    requests: Sequence[EvidenceRequest],
    notes: Sequence[EvidenceNote],
    candidate_groups: Sequence[CandidateGroup] = (),
) -> DossierAssembly:
    """把 graph state 关联为候选级只读快照，并显式保留绑定失败。"""
    tasks_by_id: dict[str, list[ReviewTask]] = {}
    for task in tasks:
        tasks_by_id.setdefault(task.id, []).append(task)
    requests_by_candidate: dict[str, list[EvidenceRequest]] = {}
    for request in requests:
        requests_by_candidate.setdefault(request.candidate_id, []).append(request)
    notes_by_candidate: dict[str, list[EvidenceNote]] = {}
    for note in notes:
        notes_by_candidate.setdefault(note.candidate_id, []).append(note)
    groups_by_candidate = {
        member.id: group
        for group in candidate_groups
        for member in group.members
    }

    dossiers: list[CandidateDossier] = []
    failures: list[CandidateBindingFailure] = []
    trace: list[tuple[str, str]] = []
    for candidate in candidates:
        matches = tasks_by_id.get(candidate.task_id, [])
        if len(matches) != 1:
            reason = "missing_task" if not matches else "ambiguous_task"
            failures.append(CandidateBindingFailure(candidate, reason))
            trace.append(
                (
                    "candidate_binding_failed",
                    _stable_json(
                        {
                            "candidate_id": candidate.id,
                            "task_id": candidate.task_id,
                            "reason": reason,
                        }
                    ),
                )
            )
            continue
        task = matches[0]
        if not task_prep.file_matches_task(candidate.file, task):
            reason = "file_mismatch"
            failures.append(CandidateBindingFailure(candidate, reason))
            trace.append(
                (
                    "candidate_binding_failed",
                    _stable_json(
                        {
                            "candidate_id": candidate.id,
                            "task_id": candidate.task_id,
                            "reason": reason,
                        }
                    ),
                )
            )
            continue
        dossiers.append(
            CandidateDossier(
                candidate=candidate,
                task=task,
                risk_profile=profiles.get(task.id),
                context_bundle=bundles.get(task.id),
                requests=tuple(requests_by_candidate.get(candidate.id, ())),
                notes=tuple(notes_by_candidate.get(candidate.id, ())),
                candidate_group=groups_by_candidate.get(candidate.id),
            )
        )
    return DossierAssembly(tuple(dossiers), tuple(failures), tuple(trace))


def _trace(plan: EvidencePlan, event: str, detail: dict[str, object]) -> None:
    plan.trace.append((event, _stable_json(detail)))


def _valid_binding(dossier: CandidateDossier) -> bool:
    return (
        dossier.candidate.task_id == dossier.task.id
        and task_prep.file_matches_task(dossier.candidate.file, dossier.task)
    )


def _trace_invalid_binding(plan: EvidencePlan, dossier: CandidateDossier) -> None:
    _trace(
        plan,
        "evidence_plan_skipped",
        {
            "candidate_id": dossier.candidate.id,
            "task_id": dossier.task.id,
            "reason": "invalid_candidate_binding",
        },
    )


def _positive_task_tags(dossier: CandidateDossier) -> list[RiskTag]:
    if dossier.risk_profile is None:
        return []
    return sorted(
        (
            tag
            for tag, score in dossier.risk_profile.tag_scores.items()
            if score > 0
        ),
        key=lambda tag: tag.value,
    )


def _trace_resolution(
    plan: EvidencePlan,
    dossier: CandidateDossier,
    resolution: CandidateTagResolution,
) -> CandidateTagResolution:
    task_tags = _positive_task_tags(dossier)
    _trace(
        plan,
        "candidate_evidence_tag_resolved",
        {
            "candidate_id": dossier.candidate.id,
            "task_id": dossier.task.id,
            "tag": resolution.tag.value,
            "confidence": resolution.confidence,
            "source": resolution.source,
            "reason": resolution.reason,
            "task_tags": [tag.value for tag in task_tags],
            "matches_task_prior": resolution.tag in task_tags,
        },
    )
    return resolution


def _resolve_dossiers(
    dossiers: Sequence[CandidateDossier],
    *,
    classifier_llm: Any,
    structured_method: str,
) -> list[CandidateTagResolution]:
    resolutions = resolve_candidate_tags(
        dossiers,
        classifier_llm=classifier_llm,
        structured_method=structured_method,
    )
    return [
        resolutions.get(dossier.candidate.id, _fallback_resolution())
        for dossier in dossiers
    ]


def _next_strategy(
    tag: RiskTag,
    purpose: EvidencePurpose,
    excluded_strategy_ids: set[str],
) -> EvidenceStrategy | None:
    return next(
        (
            strategy
            for strategy in strategies_for(tag, purpose)
            if strategy.id not in excluded_strategy_ids
        ),
        None,
    )


def _build_request(
    dossier: CandidateDossier,
    strategy: EvidenceStrategy,
) -> EvidenceRequest:
    tool_calls = strategy.build_tool_calls(dossier)
    preferred_tools: list[str] = list(
        dict.fromkeys(call.tool_name for call in tool_calls)
    )
    return EvidenceRequest(
        candidate_id=dossier.candidate.id,
        strategy_id=strategy.id,
        purpose=strategy.purpose,
        target=dossier.task.file,
        question=strategy.question_template,
        preferred_tools=preferred_tools,
    )


def _append_request(
    plan: EvidencePlan,
    dossier: CandidateDossier,
    strategy: EvidenceStrategy,
    *,
    reason: str,
) -> None:
    request = _build_request(dossier, strategy)
    plan.requests.append(request)
    _trace(
        plan,
        "evidence_planned",
        {
            "candidate_id": dossier.candidate.id,
            "task_id": dossier.task.id,
            "strategy_id": request.strategy_id,
            "purpose": request.purpose,
            "target": request.target,
            "preferred_tools": request.preferred_tools,
            "reason": reason,
        },
    )


def _append_cap_skip(
    plan: EvidencePlan,
    dossier: CandidateDossier,
    resolution: CandidateTagResolution,
    purpose: str,
) -> None:
    _trace(
        plan,
        "evidence_plan_skipped",
        {
            "candidate_id": dossier.candidate.id,
            "task_id": dossier.task.id,
            "tag": resolution.tag.value,
            "purpose": purpose,
            "reason": "candidate_request_cap",
        },
    )


def _trace_no_initial_strategy(
    plan: EvidencePlan,
    dossier: CandidateDossier,
    resolution: CandidateTagResolution,
    purpose: EvidencePurpose,
) -> None:
    _trace(
        plan,
        "evidence_plan_skipped",
        {
            "candidate_id": dossier.candidate.id,
            "tag": resolution.tag.value,
            "purpose": purpose,
            "reason": "no_available_strategy",
        },
    )


def _fallback_resolution() -> CandidateTagResolution:
    return CandidateTagResolution(
        tag=RiskTag.GENERAL_REVIEW,
        confidence=0.5,
        source="general",
        reason="候选证据主题缺失",
    )


def _resolved_dossier_tags(
    dossiers: Sequence[CandidateDossier],
    *,
    classifier_llm: Any,
    structured_method: str,
    candidate_tag_resolutions: Mapping[str, CandidateTagResolution] | None,
) -> list[CandidateTagResolution]:
    """组合预解析标签与缺失候选的分类结果。

    对已提供的 candidate_id 直接复用；缺失的走 _resolve_dossiers（保留旧测试的
    monkeypatch 兼容性）。当 Coordinator 已预解析全部标签时，不触发分类器调用。
    """
    supplied = dict(candidate_tag_resolutions or {})
    missing = [
        dossier
        for dossier in dossiers
        if dossier.candidate.id not in supplied
    ]
    if missing:
        missing_resolutions = _resolve_dossiers(
            missing,
            classifier_llm=classifier_llm,
            structured_method=structured_method,
        )
        for dossier, resolution in zip(missing, missing_resolutions, strict=True):
            supplied[dossier.candidate.id] = resolution
    return [
        supplied.get(dossier.candidate.id, _fallback_resolution())
        for dossier in dossiers
    ]


def _plan_initial(
    dossiers: Sequence[CandidateDossier],
    *,
    classifier_llm: Any,
    structured_method: str,
    candidate_tag_resolutions: Mapping[str, CandidateTagResolution] | None = None,
) -> EvidencePlan:
    plan = EvidencePlan()
    resolved: list[
        tuple[CandidateDossier, CandidateTagResolution, set[str]]
    ] = []
    request_counts: dict[str, int] = {}
    valid_dossiers: list[CandidateDossier] = []
    for dossier in dossiers:
        if not _valid_binding(dossier):
            _trace_invalid_binding(plan, dossier)
            continue
        valid_dossiers.append(dossier)

    present_candidate_ids = {
        dossier.candidate.id for dossier in valid_dossiers
    }
    traced_groups: set[str] = set()
    for dossier in valid_dossiers:
        group = dossier.candidate_group
        if group is None or group.id in traced_groups:
            continue
        traced_groups.add(group.id)
        _trace(
            plan,
            "candidate_group_evidence_scope",
            {
                "group_id": group.id,
                "shared_root_cause": group.shared_root_cause,
                "shared_behavior": group.shared_behavior,
                "shared_fix": group.shared_fix,
                "member_claims": {
                    member.id: member.claim for member in group.members
                },
                "missing_member_ids": [
                    member.id
                    for member in group.members
                    if member.id not in present_candidate_ids
                ],
            },
        )

    resolutions = _resolved_dossier_tags(
        valid_dossiers,
        classifier_llm=classifier_llm,
        structured_method=structured_method,
        candidate_tag_resolutions=candidate_tag_resolutions,
    )
    for dossier, resolution in zip(valid_dossiers, resolutions, strict=True):
        _trace_resolution(plan, dossier, resolution)
        resolved.append(
            (
                dossier,
                resolution,
                {request.strategy_id for request in dossier.requests},
            )
        )
        request_counts[dossier.candidate.id] = 0

    # All unqueued counter strategies in priority order (includes upstream).
    for dossier, resolution, excluded in resolved:
        for strategy in strategies_for(resolution.tag, "counter"):
            if strategy.id in excluded:
                continue
            if request_counts[dossier.candidate.id] >= MAX_INITIAL_REQUESTS_PER_CANDIDATE:
                _append_cap_skip(plan, dossier, resolution, "counter")
                break
            _append_request(
                plan, dossier, strategy, reason="initial_counter",
            )
            excluded.add(strategy.id)
            request_counts[dossier.candidate.id] += 1

    # One support strategy (mandatory).
    for dossier, resolution, excluded in resolved:
        if request_counts[dossier.candidate.id] >= MAX_INITIAL_REQUESTS_PER_CANDIDATE:
            _append_cap_skip(plan, dossier, resolution, "support")
            continue
        next_strategy = _next_strategy(resolution.tag, "support", excluded)
        if next_strategy is None:
            _trace_no_initial_strategy(plan, dossier, resolution, "support")
            continue
        _append_request(
            plan, dossier, next_strategy, reason="initial_support",
        )
        request_counts[dossier.candidate.id] += 1

    # One severity strategy (mandatory).
    for dossier, resolution, excluded in resolved:
        if request_counts[dossier.candidate.id] >= MAX_INITIAL_REQUESTS_PER_CANDIDATE:
            _append_cap_skip(plan, dossier, resolution, "severity")
            continue
        next_strategy = _next_strategy(resolution.tag, "severity", excluded)
        if next_strategy is None:
            _trace_no_initial_strategy(plan, dossier, resolution, "severity")
            continue
        _append_request(
            plan, dossier, next_strategy, reason="initial_severity",
        )
        request_counts[dossier.candidate.id] += 1

    return plan


def plan_evidence(
    dossiers: Sequence[CandidateDossier],
    *,
    classifier_llm: Any,
    structured_method: str,
    candidate_tag_resolutions: Mapping[str, CandidateTagResolution] | None = None,
) -> EvidencePlan:
    """One-pass complete evidence plan: all counter + support + severity.

    candidate_tag_resolutions: 预解析的候选标签映射（如来自 Coordinator）。
    已提供的 candidate_id 不会重复分类。
    """
    return _plan_initial(
        dossiers,
        classifier_llm=classifier_llm,
        structured_method=structured_method,
        candidate_tag_resolutions=candidate_tag_resolutions,
    )


def _select_fact_type_for_support(claim_text: str) -> EvidenceFactType:
    """根据 root cause 文本选择最合适的 fact_type。"""
    rc_lower = claim_text.lower()
    if any(kw in rc_lower for kw in ("表达式", "expression", "计算", "compute",
                                       "amount", "quantity", "值", "value", "运算")):
        return EvidenceFactType.VALUE_IDENTITY
    if any(kw in rc_lower for kw in ("调用", "call", "propagate", "传入", "传递",
                                       "data flow", "数据流")):
        return EvidenceFactType.DATA_FLOW
    if any(kw in rc_lower for kw in ("事务", "transaction", "commit", "rollback",
                                       "原子")):
        return EvidenceFactType.TRANSACTION_BOUNDARY
    if any(kw in rc_lower for kw in ("并发", "concurrent", "race", "竞态",
                                       "lock", "同步")):
        return EvidenceFactType.ORDERING
    if any(kw in rc_lower for kw in ("状态", "state", "transition", "转换")):
        return EvidenceFactType.STATE_TRANSITION
    if any(kw in rc_lower for kw in ("注入", "injection", "sqli", "xss",
                                       "命令", "遍历", "穿越")):
        return EvidenceFactType.REACHABILITY
    return EvidenceFactType.CHANGED_CONDITION


def _build_support_goal(
    concern: CandidateConcern, claim: CandidateClaim,
) -> EvidenceGoal | None:
    """构建 support goal：证明 root cause 的关键机制成立。"""
    rc = claim.root_cause[:200]
    if not rc.strip():
        return None
    fact_type = _select_fact_type_for_support(rc)
    return EvidenceGoal(
        concern_id=concern.concern_id,
        claim_ids=(claim.claim_id,),
        fact_type=fact_type,
        polarity=EvidencePolarity.SUPPORT,
        proposition=f"变更引入的机制成立：{rc}",
        why_needed="验证候选声称的错误机制是否真实存在于变更代码中",
        preferred_capabilities=tuple(
            c.value for c in capabilities_for_fact_type(fact_type)
        ),
        required=True,
    )


def _build_counter_goal(
    concern: CandidateConcern, claim: CandidateClaim,
) -> EvidenceGoal | None:
    """构建 counter goal：寻找足以推翻主张的最强反证。"""
    trigger_text = claim.trigger or claim.root_cause[:100]
    return EvidenceGoal(
        concern_id=concern.concern_id,
        claim_ids=(claim.claim_id,),
        fact_type=EvidenceFactType.GUARD_PRESENCE,
        polarity=EvidencePolarity.COUNTER,
        # proposition 始终保持为候选的正向主张；counter 只决定取证方向。
        # 因此发现有效 guard 时 finding.relation=contradicts，能与 Judge gate
        # 的稳定语义保持一致。
        proposition=f"问题的触发条件仍可满足且未被有效保护：{trigger_text}",
        why_needed="寻找调用前 guard、补偿事务、幂等保护或不可达证据",
        preferred_capabilities=tuple(
            c.value for c in capabilities_for_fact_type(EvidenceFactType.GUARD_PRESENCE)
        ),
        required=True,
    )


def _build_impact_goal(
    concern: CandidateConcern, claim: CandidateClaim,
) -> EvidenceGoal | None:
    """构建 impact goal：证明后果和影响范围。"""
    consequence = claim.observable_consequence or claim.root_cause[:200]
    if not consequence.strip():
        return None
    return EvidenceGoal(
        concern_id=concern.concern_id,
        claim_ids=(claim.claim_id,),
        fact_type=EvidenceFactType.OBSERVABLE_CONSEQUENCE,
        polarity=EvidencePolarity.IMPACT,
        proposition=f"变更后果的可达性与影响范围：{consequence}",
        why_needed="确认后果是否可达、跨租户、持久化或需人工修复",
        preferred_capabilities=tuple(
            c.value for c in capabilities_for_fact_type(
                EvidenceFactType.OBSERVABLE_CONSEQUENCE
            )
        ),
        required=False,
    )


def _goals_to_requests(
    concern: CandidateConcern,
    goals: list[EvidenceGoal],
    diagnostics: list[str],
) -> list:
    """将 EvidenceGoal 映射为 EvidenceRequest 列表。"""
    from codeguard_agent.models.council import EvidenceRequest

    _polarity_to_purpose: dict[str, str] = {
        "support": "support",
        "counter": "counter",
        "impact": "severity",
    }

    requests: list[EvidenceRequest] = []
    claim_by_id = {
        claim.claim_id: claim for claim in concern.claims
    }
    for goal in goals:
        tools: list[str] = list(dict.fromkeys(
            CAPABILITY_TO_TOOL[EvidenceCapability(capability)]
            for capability in goal.preferred_capabilities[:2]
        ))
        claim = next(
            (
                claim_by_id[claim_id]
                for claim_id in goal.claim_ids
                if claim_id in claim_by_id
            ),
            None,
        )
        anchor_id = (
            claim.candidate_id
            if claim is not None and claim.candidate_id
            else concern.member_candidate_ids[0]
            if concern.member_candidate_ids
            else ""
        )
        primary_file = concern.files[0] if concern.files else ""
        if claim is not None and claim.fix_location:
            location = claim.fix_location
            head, separator, tail = location.rpartition(":")
            primary_file = head if separator and tail.isdigit() else location
        purpose_value = _polarity_to_purpose.get(goal.polarity.value, "support")
        request = EvidenceRequest(
            candidate_id=anchor_id,
            strategy_id=f"claim.{goal.fact_type.value}.{goal.polarity.value}",
            purpose=purpose_value,  # type: ignore[arg-type]
            target=primary_file,
            question=goal.proposition,
            preferred_tools=tools,
            goal_id=goal.goal_id,
            concern_id=goal.concern_id,
            claim_ids=goal.claim_ids,
            fact_type=goal.fact_type,
        )
        requests.append(request)
    return requests


def plan_claim_evidence(
    concern: CandidateConcern,
    *,
    task_contexts: dict | None = None,
) -> ConcernEvidencePlan:
    """按 concern 的结构化主张生成 EvidenceGoal 并映射为 EvidenceRequest。

    每个 concern 至少生成 support/counter/impact 三类 goal。
    """
    goals: list[EvidenceGoal] = []
    diagnostics: list[str] = []

    if not concern.claims:
        return ConcernEvidencePlan(
            concern_id=concern.concern_id,
            diagnostics=("no claims in concern",),
        )

    # 当前 Judge 仍按 candidate 执行 evidence gate，因此每个成员必须有自己对齐的
    # support/counter/impact 请求。工具层会按规范化参数去重共享事实。
    for claim in concern.claims:
        for builder in (
            _build_support_goal,
            _build_counter_goal,
            _build_impact_goal,
        ):
            goal = builder(concern, claim)
            if goal is not None:
                goals.append(goal)

    uncovered = [g.goal_id for g in goals if not g.preferred_capabilities]
    if uncovered:
        diagnostics.append(f"goals without capabilities: {uncovered}")

    requests = _goals_to_requests(concern, goals, diagnostics)

    return ConcernEvidencePlan(
        concern_id=concern.concern_id,
        goals=tuple(goals),
        requests=tuple(requests),
        uncovered_goals=tuple(uncovered),
        diagnostics=tuple(diagnostics),
    )


__all__ = [
    "CandidateBindingFailure",
    "CandidateDossier",
    "DossierAssembly",
    "EvidencePlan",
    "MAX_INITIAL_REQUESTS_PER_CANDIDATE",
    "assemble_dossiers",
    "plan_claim_evidence",
    "plan_evidence",
]
