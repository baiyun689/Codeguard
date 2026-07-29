"""裁决节点：证据门控 → LLM 语义综合 → 严重度策略定级。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Sequence
from typing import Any

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.council import (
    CandidateConcern,
    CandidateEvidenceAssessment,
    ConcernAnalysis,
    EvidenceFinding,
    EvidenceRequest,
    Verdict,
)
from codeguard_agent.models.schemas import Issue
from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.concurrency import run_bounded_parallel
from codeguard_agent.pipeline.council.dedup import CandidateGroup
from codeguard_agent.pipeline.evidence.agent import (
    BoundEvidence,
    bound_evidence,
    request_strategy_mismatch,
)
from codeguard_agent.pipeline.evidence.planner import CandidateDossier, DossierAssembly
from codeguard_agent.pipeline.evidence.rules import resolve_candidate_evidence_tag
from codeguard_agent.pipeline.council.impact import (
    assess_impact,
    assess_impact_fallback,
)
from codeguard_agent.pipeline.council.severity import (
    resolve_severity as resolve_severity_new,
    resolve_severity_fallback,
    rubric_for,
)

logger = logging.getLogger("codeguard")
_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"


@dataclass
class JudgeBatch:
    verdicts: list[Verdict] = field(default_factory=list)
    final_issues: list[Issue] = field(default_factory=list)
    final_candidate_ids: list[str] = field(default_factory=list)
    trace: list[tuple[str, str]] = field(default_factory=list)


# ── helpers ──────────────────────────────────────────────────────────────────


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _trace(batch: JudgeBatch, event: str, detail: dict[str, object]) -> None:
    batch.trace.append((event, _stable_json(detail)))


def _unique_text(values: Sequence[str]) -> str:
    return "；".join(dict.fromkeys(value.strip() for value in values if value.strip()))


def _emit_supported_issues(
    batch: JudgeBatch,
    supported: Sequence[tuple[str, Issue]],
    candidate_groups: Sequence[CandidateGroup],
) -> None:
    """按严格等价组汇总已支持成员；任何未获支持成员都不影响其兄弟。"""
    issue_by_id = dict(supported)
    group_by_member = {
        member.id: group
        for group in candidate_groups
        for member in group.members
    }
    emitted_groups: set[str] = set()

    for candidate_id, issue in supported:
        group = group_by_member.get(candidate_id)
        if group is None:
            batch.final_candidate_ids.append(candidate_id)
            batch.final_issues.append(issue)
            continue
        if group.id in emitted_groups:
            continue
        emitted_groups.add(group.id)
        kept = [
            (member.id, issue_by_id[member.id])
            for member in group.members
            if member.id in issue_by_id
        ]
        if not kept:
            continue

        # 类型、裁决后严重度或文件不同，说明实际影响并不等价，安全拆回多条。
        output_shapes = {
            (
                member_issue.file,
                member_issue.type.strip().casefold(),
                member_issue.severity,
            )
            for _, member_issue in kept
        }
        if len(output_shapes) != 1:
            for kept_id, kept_issue in kept:
                batch.final_candidate_ids.append(kept_id)
                batch.final_issues.append(kept_issue)
            _trace(
                batch,
                "candidate_group_split",
                {"group_id": group.id, "member_ids": [item[0] for item in kept]},
            )
            continue

        anchor_id, anchor = kept[0]
        positive_lines = [item.line for _, item in kept if item.line > 0]
        combined = anchor.model_copy(
            update={
                "line": min(positive_lines) if positive_lines else 0,
                "type": _unique_text([item.type for _, item in kept]),
                "message": _unique_text([item.message for _, item in kept]),
                "suggestion": _unique_text([item.suggestion for _, item in kept]),
                "confidence": min(item.confidence for _, item in kept),
            }
        )
        batch.final_candidate_ids.append(anchor_id)
        batch.final_issues.append(combined)
        _trace(
            batch,
            "candidate_group_consolidated",
            {"group_id": group.id, "member_ids": [item[0] for item in kept]},
        )


def _primary_tag(
    dossier: CandidateDossier,
    concern: CandidateConcern | None = None,
) -> RiskTag:
    if concern is not None and concern.tags.primary_tag is not None:
        return concern.tags.primary_tag
    resolution = resolve_candidate_evidence_tag(
        dossier,
        None,
        structured_method="function_calling",
    )
    return resolution.tag


def _rubric_tags(
    dossier: CandidateDossier,
    concern: CandidateConcern | None,
) -> tuple[RiskTag, ...]:
    if concern is not None:
        return tuple(
            tag
            for tag in (
                concern.tags.primary_tag,
                *concern.tags.secondary_tags,
            )
            if tag is not None and tag is not RiskTag.GENERAL_REVIEW
        )
    inferred = _primary_tag(dossier)
    return () if inferred is RiskTag.GENERAL_REVIEW else (inferred,)


# ── evidence gate (deterministic, runs before LLM) ───────────────────────────


def _gate_candidate(
    evidence: list[BoundEvidence],
) -> tuple[str, str] | None:
    """Return (reason_code, reason) if the candidate should be dropped, else None."""
    if any(
        item.request.purpose == "counter"
        and item.finding.relation == "contradicts"
        and item.finding.strength == "direct"
        for item in evidence
    ):
        return "direct_counter_evidence", "直接反证足以排除候选"
    if not evidence or all(
        item.finding.relation == "insufficient" for item in evidence
    ):
        return "evidence_insufficient", "候选没有可用证据"
    if not any(
        item.request.purpose == "support" and item.finding.relation == "supports"
        for item in evidence
    ):
        return "no_supporting_evidence", "没有 support 证据支持候选主张"
    return None


# ── purpose-labelled findings ────────────────────────────────────────────────


def _purpose_findings(
    dossier: CandidateDossier,
) -> tuple[list[BoundEvidence], list[tuple[str, str]]]:
    """返回 (有效findings, trace_events)。trace 改为返回而非副作用，以支持并行。"""
    traces: list[tuple[str, str]] = []
    def _add_trace(event: str, detail: dict[str, object]) -> None:
        traces.append((event, _stable_json(detail)))

    request_by_id: dict[str, EvidenceRequest] = {}
    for request in dossier.requests:
        mismatch = request_strategy_mismatch(request, dossier)
        if mismatch is None:
            request_by_id[request.id] = request
        else:
            _add_trace("invalid_evidence_request_ignored", {
                "candidate_id": dossier.candidate.id,
                "request_id": request.id,
                "mismatch": mismatch,
            })
    for note in dossier.notes:
        if note.candidate_id != dossier.candidate.id:
            _add_trace("cross_candidate_evidence_ignored", {
                "candidate_id": dossier.candidate.id,
                "note_candidate_id": note.candidate_id,
                "request_id": note.request_id,
            })
            continue
        bound_request = request_by_id.get(note.request_id)
        if bound_request is None:
            _add_trace("orphan_evidence_ignored", {
                "candidate_id": dossier.candidate.id,
                "request_id": note.request_id,
            })
            continue
    return bound_evidence(dossier), traces


# ── synthesis payload ────────────────────────────────────────────────────────


def _synthesis_payload(
    dossier: CandidateDossier,
    evidence: list[BoundEvidence],
    concern: CandidateConcern | None,
) -> str:
    primary = _primary_tag(dossier, concern)
    findings_by_request: dict[str, list[EvidenceFinding]] = {}
    for item in evidence:
        findings_by_request.setdefault(item.request.id, []).append(item.finding)
    requests_payload = []
    for request in dossier.requests:
        request_findings = findings_by_request.get(request.id)
        if request_findings is None:
            continue
        requests_payload.append({
            "strategy_id": request.strategy_id,
            "purpose": request.purpose,
            "question": request.question,
            "findings": [f.model_dump(mode="json") for f in request_findings],
        })
    return _stable_json({
        "candidate_alias": "C001",
        "candidate": {
            "type": dossier.candidate.type,
            "claim": dossier.candidate.claim,
            "file": dossier.candidate.file,
            "line": dossier.candidate.line,
        },
        "concern": (
            {
                "concern_id": concern.concern_id,
                "member_candidate_ids": concern.member_candidate_ids,
                "claims": [
                    claim.model_dump(mode="json")
                    for claim in concern.claims
                    if claim.candidate_id in ("", dossier.candidate.id)
                ],
                "primary_tag": (
                    concern.tags.primary_tag.value
                    if concern.tags.primary_tag is not None
                    else None
                ),
                "secondary_tags": [
                    tag.value for tag in concern.tags.secondary_tags
                ],
            }
            if concern is not None
            else None
        ),
        "task_patch": dossier.task.patch,
        "primary_tag": primary.value,
        "requests": requests_payload,
    })


# ── LLM synthesis ────────────────────────────────────────────────────────────


def _synthesize(
    dossier: CandidateDossier,
    evidence: list[BoundEvidence],
    *,
    judge_llm: Any,
    structured_method: str,
    max_retries: int,
    concern: CandidateConcern | None = None,
) -> CandidateEvidenceAssessment | None:
    try:
        structured = judge_llm.with_structured_output(
            CandidateEvidenceAssessment,
            method=structured_method,
        )
        system_prompt = (_PROMPT_DIR / "council-judge.txt").read_text(encoding="utf-8")
        result = invoke_with_retry(
            structured,
            [
                ("system", system_prompt),
                ("user", _synthesis_payload(dossier, evidence, concern)),
            ],
            max_retries=max_retries,
        )
        if result is None:
            return None
        if not isinstance(result, CandidateEvidenceAssessment):
            result = CandidateEvidenceAssessment.model_validate(result)
        if result.candidate_id != "C001":
            logger.warning("Synthesis returned unexpected candidate_id: %s", result.candidate_id)
            return None
        return result
    except Exception:
        logger.warning("CouncilJudge LLM synthesis failed", exc_info=True)
        return None


# ── main entry ───────────────────────────────────────────────────────────────


def _judge_one_candidate(
    dossier: CandidateDossier,
    *,
    judge_llm: Any,
    structured_method: str,
    max_retries: int,
    concern: CandidateConcern | None = None,
) -> tuple[Verdict, Issue | None, str, list[tuple[str, str]]]:
    """对单个候选执行证据门控 → LLM 综合 → 裁决，返回 (verdict, issue, candidate_id, traces)。"""
    traces: list[tuple[str, str]] = []
    def _add_trace(event: str, detail: dict[str, object]) -> None:
        traces.append((event, _stable_json(detail)))

    findings, purpose_traces = _purpose_findings(dossier)
    traces.extend(purpose_traces)

    # Evidence gate
    gate = _gate_candidate(findings)
    if gate is not None:
        reason_code, reason = gate
        verdict = Verdict(dossier.candidate.id, "drop", reason_code, reason)
        _add_trace("judge_verdict", {
            "candidate_id": verdict.candidate_id, "action": "drop",
            "reason_code": reason_code,
        })
        return verdict, None, "", traces

    # LLM synthesis
    assessment = _synthesize(
        dossier, findings,
        judge_llm=judge_llm,
        structured_method=structured_method,
        max_retries=max_retries,
        concern=concern,
    )

    if assessment is None:
        fallback = resolve_severity_fallback()
        resolved_severity = fallback.severity
        verdict = Verdict(
            dossier.candidate.id, "keep",
            "severity_evidence_incomplete",
            "LLM synthesis failed, using conservative severity fallback",
            resolved_severity=resolved_severity,
        )
        issue = dossier.candidate.to_issue().model_copy(
            update={"severity": resolved_severity}
        )
        _add_trace("judge_verdict", {
            "candidate_id": verdict.candidate_id, "action": "keep",
            "reason_code": "severity_evidence_incomplete",
            "resolved_severity": resolved_severity.value,
        })
        _add_trace("severity_resolved", {
            "candidate_id": dossier.candidate.id,
            "matched_rule": fallback.rule_id,
            "severity": resolved_severity.value,
            "fallback_used": True,
        })
        return verdict, issue, dossier.candidate.id, traces

    # Post-synthesis adjudication
    if assessment.claim_status == "refuted" or assessment.counter_effect == "complete":
        verdict = Verdict(
            dossier.candidate.id, "drop",
            "synthesized_counter_evidence",
            assessment.reason or "synthesis refuted candidate",
        )
        _add_trace("judge_verdict", {
            "candidate_id": verdict.candidate_id, "action": "drop",
            "reason_code": "synthesized_counter_evidence",
        })
        return verdict, None, "", traces

    if assessment.claim_status == "unresolved":
        verdict = Verdict(
            dossier.candidate.id, "drop",
            "evidence_conflict_unresolved",
            "; ".join(assessment.conflicts) or "evidence conflicts unresolved",
        )
        _add_trace("judge_verdict", {
            "candidate_id": verdict.candidate_id, "action": "drop",
            "reason_code": "evidence_conflict_unresolved",
        })
        return verdict, None, "", traces

    # claim_status == "supported" → new severity resolution
    try:
        tags = _rubric_tags(dossier, concern)
        rubric = rubric_for(tags=tags)
        impact_findings = [
            item.finding
            for item in findings
            if item.request.purpose == "severity"
            and (
                concern is None
                or item.finding.concern_id in (None, concern.concern_id)
            )
        ]
        impact = assess_impact(
            concern_id=(
                concern.concern_id if concern is not None
                else dossier.candidate.id
            ),
            findings=impact_findings,
            rubric=rubric,
            llm=judge_llm,
        )
        resolution = resolve_severity_new(impact, rubric)
    except Exception:
        logger.warning("New severity resolution failed, using fallback", exc_info=True)
        impact = assess_impact_fallback(
            concern.concern_id if concern is not None else dossier.candidate.id
        )
        resolution = resolve_severity_fallback()

    verdict = Verdict(
        dossier.candidate.id, "keep",
        f"severity_resolved:{resolution.rule_id}",
        resolution.rationale,
        resolved_severity=resolution.severity,
    )
    issue = dossier.candidate.to_issue().model_copy(
        update={"severity": resolution.severity}
    )
    _add_trace("judge_verdict", {
        "candidate_id": verdict.candidate_id, "action": "keep",
        "reason_code": f"severity_resolved:{resolution.rule_id}",
        "resolved_severity": resolution.severity.value,
    })
    _add_trace("severity_resolved", {
        "candidate_id": dossier.candidate.id,
        "concern_id": impact.concern_id,
        "impact_class": impact.impact_class.value,
        "matched_rule": resolution.rule_id,
        "severity": resolution.severity.value,
        "proven_factors": [f.value for f in resolution.proven_factors],
        "limiting_factors": [f.value for f in resolution.limiting_factors],
        "evidence_ids": list(resolution.evidence_ids),
        "impact_diagnostics": list(impact.diagnostics),
        "fallback_used": resolution.fallback_used,
    })
    return verdict, issue, dossier.candidate.id, traces


def judge_candidates(
    assembly: DossierAssembly,
    *,
    judge_llm: Any,
    structured_method: str,
    max_retries: int,
    candidate_groups: Sequence[CandidateGroup] = (),
    concern_analysis: ConcernAnalysis | None = None,
) -> JudgeBatch:
    batch = JudgeBatch()
    concern_by_candidate = {
        candidate_id: concern
        for concern in (
            concern_analysis.concerns if concern_analysis is not None else ()
        )
        for candidate_id in concern.member_candidate_ids
    }

    # Binding failures → drop（确定性，无需并行）
    for failure in assembly.failures:
        verdict = Verdict(
            failure.candidate.id,
            "drop",
            "invalid_candidate_binding",
            failure.reason,
        )
        batch.verdicts.append(verdict)
        _trace(
            batch, "judge_verdict",
            {"candidate_id": verdict.candidate_id, "action": "drop",
             "reason_code": verdict.reason_code},
        )

    if not assembly.dossiers:
        return batch

    # 候选并行裁决：每个候选独立执行证据门控 → LLM 综合 → 定级
    def _invoke(dossier: CandidateDossier):
        return _judge_one_candidate(
            dossier,
            judge_llm=judge_llm,
            structured_method=structured_method,
            max_retries=max_retries,
            concern=concern_by_candidate.get(dossier.candidate.id),
        )

    results = run_bounded_parallel(assembly.dossiers, _invoke, max_workers=6)

    supported: list[tuple[str, Issue]] = []
    for result in results:
        if result is None:
            continue
        verdict, issue, candidate_id, traces = result
        batch.verdicts.append(verdict)
        if issue is not None and candidate_id:
            supported.append((candidate_id, issue))
        batch.trace.extend(traces)

    _emit_supported_issues(batch, supported, candidate_groups)
    return batch


__all__ = ["JudgeBatch", "judge_candidates"]
