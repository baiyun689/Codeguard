"""候选证据主题到 EvidenceRequest 的纯规划层。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from codeguard_agent.models.council import (
    CandidateClaim,
    CandidateConcern,
    CandidateIssue,
    ConcernEvidencePlan,
    EvidenceFactType,
    EvidenceGoal,
    EvidenceNote,
    EvidencePolarity,
    EvidenceRequest,
)
from codeguard_agent.models.tasks import ReviewTask, TaskContextBundle
from codeguard_agent.pipeline.risk import task_prep
from codeguard_agent.pipeline.evidence.capability import capabilities_for_fact_type
from codeguard_agent.pipeline.evidence.strategy_types import (
    CAPABILITY_TO_TOOL,
    EvidenceCapability,
)

if TYPE_CHECKING:
    from codeguard_agent.pipeline.council.dedup import CandidateGroup


@dataclass(frozen=True)
class CandidateDossier:
    """规划单个候选所需的只读快照，不进入 graph State。"""

    candidate: CandidateIssue
    task: ReviewTask
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


def _stable_json(detail: dict[str, object]) -> str:
    return json.dumps(detail, ensure_ascii=False, sort_keys=True)


def assemble_dossiers(
    candidates: Sequence[CandidateIssue],
    tasks: Sequence[ReviewTask],
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
                context_bundle=bundles.get(task.id),
                requests=tuple(requests_by_candidate.get(candidate.id, ())),
                notes=tuple(notes_by_candidate.get(candidate.id, ())),
                candidate_group=groups_by_candidate.get(candidate.id),
            )
        )
    return DossierAssembly(tuple(dossiers), tuple(failures), tuple(trace))


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
    "assemble_dossiers",
    "plan_claim_evidence",
]
