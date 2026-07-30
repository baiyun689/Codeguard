"""EvidenceResearcher：快速批量取证，并只对疑难候选做一次受限 ReAct。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Sequence

from pydantic import BaseModel, Field, ValidationError, field_validator

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.council import (
    CandidateConcern,
    CandidateInvestigationPlan,
    EvidenceDossierStatus,
    EvidenceDossierSummary,
    EvidenceNote,
    EvidenceRequest,
)
from codeguard_agent.pipeline.evidence.agent import EvidenceBatch, collect_evidence
from codeguard_agent.pipeline.evidence.planner import CandidateDossier
from codeguard_agent.pipeline.evidence.strategist import (
    investigation_plans_to_requests,
)

logger = logging.getLogger("codeguard")
_PROMPT = (
    Path(__file__).resolve().parents[2] / "prompts" / "evidence-researcher.txt"
)
_SEMANTIC_TOOLS = {
    "get_file_content",
    "find_sensitive_apis",
    "find_callers",
    "get_code_metrics",
    "inspect_security_path",
    "inspect_change_impact",
    "inspect_structure",
}


class _EscalationOutput(BaseModel):
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


@dataclass
class EvidenceResearchBatch:
    requests: list[EvidenceRequest] = field(default_factory=list)
    notes: list[EvidenceNote] = field(default_factory=list)
    trace: list[tuple[str, str]] = field(default_factory=list)
    gathered_context: list[Any] = field(default_factory=list)
    dossier_summaries: list[EvidenceDossierSummary] = field(default_factory=list)


class _CachingToolClient:
    """跨快速路径/ReAct 轮次复用完全相同的 Gateway 调用。"""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self._cache: dict[tuple[str, str], Any] = {}
        self._hit_keys: set[tuple[str, str]] = set()
        self._lock = Lock()

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._delegate, name)
        if not callable(target) or name not in _SEMANTIC_TOOLS:
            return target

        def _cached(*args: Any, **kwargs: Any) -> Any:
            call_arguments = kwargs or {
                f"arg{index}": value for index, value in enumerate(args)
            }
            key = (name, _canonical_arguments(call_arguments))
            with self._lock:
                if key in self._cache:
                    self._hit_keys.add(key)
                    return self._cache[key]
            result = target(*args, **kwargs)
            with self._lock:
                return self._cache.setdefault(key, result)

        return _cached

    def was_cache_hit(self, tool: str, arguments: dict[str, Any]) -> bool:
        return (tool, _canonical_arguments(arguments)) in self._hit_keys


def _canonical_arguments(arguments: dict[str, Any]) -> str:
    return json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )


def _notes_by_request(notes: Sequence[EvidenceNote]) -> dict[str, list[EvidenceNote]]:
    out: dict[str, list[EvidenceNote]] = {}
    for note in notes:
        out.setdefault(note.request_id, []).append(note)
    return out


def _summarize_candidate(
    plan: CandidateInvestigationPlan,
    requests: Sequence[EvidenceRequest],
    notes: Sequence[EvidenceNote],
    trace: Sequence[tuple[str, str]],
    *,
    rounds: int,
    react_used: bool,
    extra_limitations: Sequence[str] = (),
) -> EvidenceDossierSummary:
    candidate_requests = [
        request for request in requests
        if request.candidate_id == plan.candidate_id
    ]
    if not plan.actionable and not candidate_requests:
        return EvidenceDossierSummary(
            candidate_id=plan.candidate_id,
            status=EvidenceDossierStatus.NOT_ACTIONABLE,
            limitations=(plan.skip_reason,),
            rounds=0,
        )

    candidate_notes = [
        note for note in notes
        if note.candidate_id == plan.candidate_id
    ]
    notes_by_request = _notes_by_request(candidate_notes)
    unanswered: list[str] = []
    has_support = False
    has_counter = False
    limitations = list(extra_limitations)
    for request in candidate_requests:
        findings = [
            finding
            for note in notes_by_request.get(request.id, ())
            for finding in note.findings
        ]
        if not findings or all(
            finding.relation == "insufficient" for finding in findings
        ):
            if request.goal_id:
                unanswered.append(request.goal_id)
        for finding in findings:
            if finding.relation == "supports" and request.purpose == "support":
                has_support = True
            if (
                finding.relation == "contradicts"
                and request.purpose == "counter"
            ):
                has_counter = True
            if finding.relation == "insufficient" and finding.limitation:
                limitations.append(finding.limitation)

    if has_support and has_counter:
        status = EvidenceDossierStatus.CONFLICTED
    elif has_counter:
        status = EvidenceDossierStatus.REFUTED
    elif has_support:
        status = EvidenceDossierStatus.SUPPORTED
    else:
        status = EvidenceDossierStatus.INSUFFICIENT

    tool_call_count = 0
    for event, detail in trace:
        if event != "evidence_tool_called":
            continue
        try:
            payload = json.loads(detail)
        except (TypeError, json.JSONDecodeError):
            continue
        if payload.get("candidate_id") == plan.candidate_id:
            tool_call_count += 1
    return EvidenceDossierSummary(
        candidate_id=plan.candidate_id,
        status=status,
        unanswered_question_ids=tuple(dict.fromkeys(unanswered)),
        limitations=tuple(dict.fromkeys(limitations)),
        rounds=rounds,
        tool_call_count=tool_call_count,
        react_used=react_used,
    )


def _can_use_tools(enabled_tools: Sequence[str] | None) -> bool:
    return enabled_tools is None or bool(_SEMANTIC_TOOLS.intersection(enabled_tools))


def _select_difficult(
    plans: Sequence[CandidateInvestigationPlan],
    summaries: Sequence[EvidenceDossierSummary],
    requests: Sequence[EvidenceRequest],
    *,
    tool_client: Any,
    analyst_llm: Any,
    enabled_tools: Sequence[str] | None,
    limit: int,
) -> list[CandidateInvestigationPlan]:
    if (
        tool_client is None
        or analyst_llm is None
        or not _can_use_tools(enabled_tools)
        or limit <= 0
    ):
        return []
    summary_by_candidate = {
        summary.candidate_id: summary for summary in summaries
    }
    request_by_question = {
        (request.candidate_id, request.goal_id): request
        for request in requests
        if request.goal_id
    }
    available_tools = (
        set(_SEMANTIC_TOOLS)
        if enabled_tools is None
        else _SEMANTIC_TOOLS.intersection(enabled_tools)
    )
    selected: list[CandidateInvestigationPlan] = []
    for plan in plans:
        summary = summary_by_candidate.get(plan.candidate_id)
        required_ids = {
            question.question_id
            for question in plan.questions
            if question.required
        }
        required_unanswered = (
            set(summary.unanswered_question_ids).intersection(required_ids)
            if summary is not None
            else set()
        )
        already_used_tools: set[str] = set()
        for question_id in required_unanswered:
            request = request_by_question.get(
                (plan.candidate_id, question_id)
            )
            if request is not None:
                already_used_tools.update(request.preferred_tools)
        has_missing_capability = bool(available_tools - already_used_tools)
        if (
            plan.actionable
            and summary is not None
            and summary.status
            in {
                EvidenceDossierStatus.INSUFFICIENT,
                EvidenceDossierStatus.CONFLICTED,
            }
            and required_unanswered
            and has_missing_capability
        ):
            selected.append(plan)
            if len(selected) >= limit:
                break
    return selected


def _plan_followups(
    selected: Sequence[CandidateInvestigationPlan],
    summaries: Sequence[EvidenceDossierSummary],
    requests: Sequence[EvidenceRequest],
    notes: Sequence[EvidenceNote],
    *,
    llm: Any,
    structured_method: str,
) -> list[CandidateInvestigationPlan]:
    if not selected:
        return []
    selected_ids = {plan.candidate_id for plan in selected}
    summary_by_id = {
        item.candidate_id: item.model_dump(mode="json")
        for item in summaries
        if item.candidate_id in selected_ids
    }
    payload = {
        "candidates": [
            {
                "plan": plan.model_dump(mode="json"),
                "fast_path_summary": summary_by_id.get(plan.candidate_id),
                "requests": [
                    request.model_dump(mode="json")
                    for request in requests
                    if request.candidate_id == plan.candidate_id
                ],
                "notes": [
                    note.model_dump(mode="json")
                    for note in notes
                    if note.candidate_id == plan.candidate_id
                ],
            }
            for plan in selected
        ]
    }
    try:
        structured = llm.with_structured_output(
            _EscalationOutput,
            method=structured_method,
        )
        raw = invoke_with_retry(
            structured,
            [
                ("system", _PROMPT.read_text(encoding="utf-8")),
                (
                    "user",
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            ],
            max_retries=1,
        )
        output = (
            raw
            if isinstance(raw, _EscalationOutput)
            else _EscalationOutput.model_validate(raw)
        )
    except Exception:
        logger.warning("EvidenceResearcher ReAct planning failed", exc_info=True)
        return []

    existing = {
        (
            request.candidate_id,
            request.purpose,
            " ".join(request.question.lower().split()),
        )
        for request in requests
    }
    accepted: list[CandidateInvestigationPlan] = []
    seen: set[str] = set()
    for raw_plan in output.plans:
        try:
            plan = (
                raw_plan
                if isinstance(raw_plan, CandidateInvestigationPlan)
                else CandidateInvestigationPlan.model_validate(raw_plan)
            )
        except (TypeError, ValueError, ValidationError):
            continue
        if (
            plan.candidate_id not in selected_ids
            or plan.candidate_id in seen
            or not plan.actionable
        ):
            continue
        questions = tuple(
            question
            for question in plan.questions[:2]
            if (
                plan.candidate_id,
                question.purpose,
                " ".join(question.question.lower().split()),
            )
            not in existing
        )
        if not questions:
            continue
        accepted.append(plan.model_copy(update={"questions": questions}))
        seen.add(plan.candidate_id)
    return accepted


def research_evidence(
    plans: Sequence[CandidateInvestigationPlan],
    concerns: Sequence[CandidateConcern],
    *,
    dossiers: Sequence[CandidateDossier],
    initial_requests: Sequence[EvidenceRequest],
    tool_client: Any,
    analyst_llm: Any,
    structured_method: str,
    enabled_tools: list[str] | None,
    collect_fn: Callable[..., EvidenceBatch] = collect_evidence,
    max_react_candidates: int = 5,
) -> EvidenceResearchBatch:
    """执行快速路径；仅对仍可通过现有能力补齐事实的疑难候选追加一轮。"""
    cached_client = (
        _CachingToolClient(tool_client)
        if tool_client is not None
        else None
    )
    initial = collect_fn(
        dossiers,
        list(initial_requests),
        tool_client=cached_client,
        analyst_llm=analyst_llm,
        structured_method=structured_method,
        enabled_tools=enabled_tools,
    )
    all_requests = list(initial_requests)
    all_notes = list(initial.notes)
    all_trace = list(initial.trace)
    all_context = list(initial.gathered_context)
    summaries = [
        _summarize_candidate(
            plan,
            all_requests,
            all_notes,
            all_trace,
            rounds=1,
            react_used=False,
            extra_limitations=(
                ("no_tool_client",) if tool_client is None else ()
            ),
        )
        for plan in plans
    ]

    difficult = _select_difficult(
        plans,
        summaries,
        all_requests,
        tool_client=tool_client,
        analyst_llm=analyst_llm,
        enabled_tools=enabled_tools,
        limit=max_react_candidates,
    )
    followup_plans = _plan_followups(
        difficult,
        summaries,
        all_requests,
        all_notes,
        llm=analyst_llm,
        structured_method=structured_method,
    )
    followup_requests = investigation_plans_to_requests(
        followup_plans,
        concerns,
    )
    if followup_requests:
        followup = collect_fn(
            dossiers,
            followup_requests,
            tool_client=cached_client,
            analyst_llm=analyst_llm,
            structured_method=structured_method,
            enabled_tools=enabled_tools,
        )
        followup_trace: list[tuple[str, str]] = []
        for event, detail in followup.trace:
            rewritten_event = event
            if (
                event == "evidence_tool_called"
                and isinstance(cached_client, _CachingToolClient)
            ):
                try:
                    payload = json.loads(detail)
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                arguments = payload.get("arguments")
                if (
                    isinstance(arguments, dict)
                    and cached_client.was_cache_hit(
                        str(payload.get("tool", "")),
                        arguments,
                    )
                ):
                    rewritten_event = "evidence_tool_reused_cross_round"
            followup_trace.append((rewritten_event, detail))
        all_requests.extend(followup_requests)
        all_notes.extend(followup.notes)
        all_trace.extend(followup_trace)
        all_context.extend(followup.gathered_context)

    reacted_ids = {plan.candidate_id for plan in followup_plans}
    summaries = [
        _summarize_candidate(
            plan,
            all_requests,
            all_notes,
            all_trace,
            rounds=2 if plan.candidate_id in reacted_ids else 1,
            react_used=plan.candidate_id in reacted_ids,
            extra_limitations=(
                ("no_tool_client",) if tool_client is None else ()
            ),
        )
        for plan in plans
    ]
    all_trace.append(
        (
            "research_completed",
            json.dumps(
                {
                    "candidates": len(plans),
                    "requests": len(all_requests),
                    "react_candidates": len(reacted_ids),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    )
    return EvidenceResearchBatch(
        requests=all_requests,
        notes=all_notes,
        trace=all_trace,
        gathered_context=all_context,
        dossier_summaries=summaries,
    )


__all__ = ["EvidenceResearchBatch", "research_evidence"]
