"""EvidenceStrategist：批量提出少量、候选特定的调查问题。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel, Field, ValidationError, field_validator

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.council import (
    CandidateConcern,
    CandidateInvestigationPlan,
    EvidenceFactType,
    EvidenceRequest,
    InvestigationQuestion,
)
from codeguard_agent.pipeline.evidence.capability import capabilities_for_fact_type
from codeguard_agent.pipeline.evidence.planner import CandidateDossier
from codeguard_agent.pipeline.evidence.strategy_types import CAPABILITY_TO_TOOL

logger = logging.getLogger("codeguard")
_PROMPT = (
    Path(__file__).resolve().parents[2] / "prompts" / "evidence-strategist.txt"
)
_BATCH_SIZE = 6


class _StrategyOutput(BaseModel):
    plans: list[CandidateInvestigationPlan | dict[str, Any]] = Field(
        default_factory=list
    )

    @field_validator("plans", mode="before")
    @classmethod
    def parse_stringified_plans(cls, value: object) -> object:
        if isinstance(value, str):
            for candidate in (
                value,
                value[value.find("[") : value.rfind("]") + 1],
            ):
                try:
                    parsed = json.loads(candidate)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(parsed, dict):
                    parsed = parsed.get("plans", parsed)
                if isinstance(parsed, list):
                    return parsed
        return value


@dataclass(frozen=True)
class InvestigationPlanningBatch:
    plans: tuple[CandidateInvestigationPlan, ...]
    fallback_candidate_ids: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    llm_call_count: int = 0


def _candidate_payload(
    concern: CandidateConcern,
    candidate_id: str,
    dossier: CandidateDossier | None,
) -> dict[str, Any]:
    claims = [
        claim.model_dump(mode="json")
        for claim in concern.claims
        if claim.candidate_id == candidate_id
    ]
    return {
        "candidate_id": candidate_id,
        "files": list(concern.files),
        "claims": claims,
        "risk_tags": [
            tag.value for tag in concern.member_risk_tags.get(candidate_id, ())
        ],
        "source_agents": list(concern.source_agents),
        "candidate": (
            {
                "type": dossier.candidate.type,
                "claim": dossier.candidate.claim,
                "suggestion": dossier.candidate.suggestion,
                "confidence": dossier.candidate.confidence,
            }
            if dossier is not None
            else None
        ),
        "task_patch": dossier.task.patch if dossier is not None else "",
        "context": (
            dossier.context_bundle.model_dump(mode="json")
            if dossier is not None and dossier.context_bundle is not None
            else None
        ),
    }


def _fallback_fact_type(concern: CandidateConcern, candidate_id: str) -> EvidenceFactType:
    text = " ".join(
        part
        for claim in concern.claims
        if claim.candidate_id == candidate_id
        for part in (
            claim.root_cause,
            claim.trigger,
            claim.observable_consequence,
        )
    )
    lowered = text.lower()
    if any(
        token in lowered
        for token in ("注入", "injection", "命令", "shell", "sql", "外部输入")
    ):
        return EvidenceFactType.REACHABILITY
    if any(token in lowered for token in ("调用", "call", "传递", "data flow")):
        return EvidenceFactType.DATA_FLOW
    if any(token in lowered for token in ("并发", "race", "顺序", "ordering")):
        return EvidenceFactType.ORDERING
    if any(token in lowered for token in ("事务", "transaction", "rollback")):
        return EvidenceFactType.TRANSACTION_BOUNDARY
    return EvidenceFactType.CHANGED_CONDITION


def _fallback_plan(
    concern: CandidateConcern,
    candidate_id: str,
) -> CandidateInvestigationPlan:
    claim = next(
        (
            item
            for item in concern.claims
            if item.candidate_id == candidate_id
        ),
        None,
    )
    hypothesis = (
        claim.root_cause
        if claim is not None and claim.root_cause.strip()
        else f"候选 {candidate_id} 的错误机制成立"
    )
    trigger = (
        claim.trigger
        if claim is not None and claim.trigger.strip()
        else hypothesis
    )
    fact_type = _fallback_fact_type(concern, candidate_id)
    return CandidateInvestigationPlan(
        candidate_id=candidate_id,
        hypothesis=hypothesis,
        questions=(
            InvestigationQuestion(
                purpose="support",
                question=f"变更代码是否直接支持该错误机制：{hypothesis}",
                why_it_matters="工具或规划模型不可用时仍需验证候选的核心机制",
                expected_fact=fact_type,
            ),
            InvestigationQuestion(
                purpose="counter",
                question=f"是否存在足以阻止该触发条件的保护或不可达事实：{trigger}",
                why_it_matters="避免在降级模式下只寻找支持证据",
                expected_fact=EvidenceFactType.GUARD_PRESENCE,
            ),
        ),
        source="fallback",
    )


def _parse_output(value: Any) -> _StrategyOutput:
    if isinstance(value, _StrategyOutput):
        return value
    if isinstance(value, dict):
        return _StrategyOutput.model_validate(value)
    raise ValueError("strategist returned unsupported output")


def build_investigation_plans(
    concerns: Sequence[CandidateConcern],
    *,
    llm: Any,
    structured_method: str,
    dossiers: Sequence[CandidateDossier] = (),
    batch_size: int = _BATCH_SIZE,
) -> InvestigationPlanningBatch:
    """批量规划候选调查问题；失败只对缺失候选使用小型安全回退。"""
    concern_by_candidate = {
        candidate_id: concern
        for concern in concerns
        for candidate_id in concern.member_candidate_ids
    }
    candidate_ids = list(concern_by_candidate)
    dossier_by_candidate = {
        dossier.candidate.id: dossier for dossier in dossiers
    }
    accepted: dict[str, CandidateInvestigationPlan] = {}
    diagnostics: list[str] = []
    llm_call_count = 0

    if llm is not None and candidate_ids:
        for offset in range(0, len(candidate_ids), max(1, batch_size)):
            batch_ids = candidate_ids[offset : offset + max(1, batch_size)]
            payload = [
                _candidate_payload(
                    concern_by_candidate[cid],
                    cid,
                    dossier_by_candidate.get(cid),
                )
                for cid in batch_ids
            ]
            try:
                structured = llm.with_structured_output(
                    _StrategyOutput,
                    method=structured_method,
                )
                llm_call_count += 1
                raw = invoke_with_retry(
                    structured,
                    [
                        ("system", _PROMPT.read_text(encoding="utf-8")),
                        (
                            "user",
                            json.dumps(
                                {"candidates": payload},
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                        ),
                    ],
                    max_retries=1,
                )
                output = _parse_output(raw)
            except Exception:
                logger.warning(
                    "EvidenceStrategist batch failed; using candidate fallback",
                    exc_info=True,
                )
                diagnostics.append(
                    f"strategist_batch_failed:{','.join(batch_ids)}"
                )
                continue
            allowed = set(batch_ids)
            for raw_plan in output.plans:
                try:
                    plan = (
                        raw_plan
                        if isinstance(raw_plan, CandidateInvestigationPlan)
                        else CandidateInvestigationPlan.model_validate(raw_plan)
                    )
                except (TypeError, ValueError, ValidationError):
                    diagnostics.append("invalid_candidate_plan")
                    continue
                if plan.candidate_id not in allowed:
                    diagnostics.append(
                        f"unknown_candidate_id:{plan.candidate_id}"
                    )
                    continue
                if plan.candidate_id in accepted:
                    diagnostics.append(
                        f"duplicate_candidate_id:{plan.candidate_id}"
                    )
                    continue
                accepted[plan.candidate_id] = plan.model_copy(
                    update={"source": "llm"}
                )

    fallback_ids: list[str] = []
    ordered: list[CandidateInvestigationPlan] = []
    for candidate_id in candidate_ids:
        selected_plan = accepted.get(candidate_id)
        if selected_plan is None:
            selected_plan = _fallback_plan(
                concern_by_candidate[candidate_id],
                candidate_id,
            )
            fallback_ids.append(candidate_id)
        ordered.append(selected_plan)
    return InvestigationPlanningBatch(
        plans=tuple(ordered),
        fallback_candidate_ids=tuple(fallback_ids),
        diagnostics=tuple(diagnostics),
        llm_call_count=llm_call_count,
    )


def _target_file(concern: CandidateConcern, candidate_id: str) -> str:
    default = concern.files[0] if concern.files else "(unknown)"
    claim = next(
        (
            item
            for item in concern.claims
            if item.candidate_id == candidate_id
        ),
        None,
    )
    if claim is None or not claim.fix_location:
        return default
    return next(
        (
            file
            for file in concern.files
            if file
            and (
                claim.fix_location == file
                or claim.fix_location.startswith(
                    (f"{file}:", f"{file}，", f"{file},")
                )
            )
        ),
        default,
    )


def investigation_plans_to_requests(
    plans: Sequence[CandidateInvestigationPlan],
    concerns: Sequence[CandidateConcern],
) -> list[EvidenceRequest]:
    """把动态问题投影到既有 EvidenceRequest/Judge 契约。"""
    concern_by_candidate = {
        candidate_id: concern
        for concern in concerns
        for candidate_id in concern.member_candidate_ids
    }
    requests: list[EvidenceRequest] = []
    for plan in plans:
        concern = concern_by_candidate.get(plan.candidate_id)
        if concern is None:
            continue
        claim_ids = tuple(
            claim.claim_id
            for claim in concern.claims
            if claim.candidate_id == plan.candidate_id
        )
        questions = plan.questions[:3]
        if not plan.actionable:
            questions = (
                InvestigationQuestion(
                    purpose="support",
                    question=(
                        "该候选是否存在可验证的运行时错误机制："
                        f"{plan.hypothesis}"
                    ),
                    why_it_matters=(
                        "Strategist 的 not_actionable 只是建议，"
                        "必须由证据门槛独立验证"
                    ),
                    expected_fact=EvidenceFactType.CHANGED_CONDITION,
                ),
            )
        for question in questions:
            polarity = "impact" if question.purpose == "severity" else question.purpose
            capabilities = capabilities_for_fact_type(question.expected_fact)
            preferred_tools: list[str] = list(
                dict.fromkeys(
                    CAPABILITY_TO_TOOL[capability]
                    for capability in capabilities[:2]
                )
            )
            requests.append(
                EvidenceRequest(
                    candidate_id=plan.candidate_id,
                    strategy_id=(
                        f"claim.{question.expected_fact.value}.{polarity}"
                    ),
                    purpose=question.purpose,
                    target=_target_file(concern, plan.candidate_id),
                    question=question.question,
                    preferred_tools=preferred_tools,
                    goal_id=question.question_id,
                    concern_id=concern.concern_id,
                    claim_ids=claim_ids,
                    fact_type=question.expected_fact,
                )
            )
    return requests


__all__ = [
    "InvestigationPlanningBatch",
    "build_investigation_plans",
    "investigation_plans_to_requests",
]
