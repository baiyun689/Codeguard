"""裁决节点：证据门控 → LLM 语义综合 → 严重度策略定级。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.council import (
    CandidateEvidenceAssessment,
    EvidenceFinding,
    EvidenceRequest,
    Verdict,
)
from codeguard_agent.models.schemas import Issue
from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence.agent import (
    BoundEvidence,
    bound_evidence,
    request_strategy_mismatch,
)
from codeguard_agent.pipeline.evidence.planner import CandidateDossier, DossierAssembly
from codeguard_agent.pipeline.evidence.rules import STRATEGIES_BY_ID
from codeguard_agent.pipeline.council.severity import policy_for, resolve_severity

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


def _primary_tag(dossier: CandidateDossier) -> RiskTag:
    tags: set[RiskTag] = set()
    for request in dossier.requests:
        if request_strategy_mismatch(request, dossier) is not None:
            continue
        strategy = STRATEGIES_BY_ID.get(request.strategy_id)
        if strategy is not None:
            tags.update(strategy.tags)
    if len(tags) == 1:
        return next(iter(tags))
    if not tags:
        return RiskTag.GENERAL_REVIEW
    logger.warning(
        "Ambiguous primary tag for candidate %s: %s — using GENERAL_REVIEW",
        dossier.candidate.id,
        tags,
    )
    return RiskTag.GENERAL_REVIEW


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
    batch: JudgeBatch,
) -> list[BoundEvidence]:
    request_by_id: dict[str, EvidenceRequest] = {}
    for request in dossier.requests:
        mismatch = request_strategy_mismatch(request, dossier)
        if mismatch is None:
            request_by_id[request.id] = request
        else:
            _trace(
                batch,
                "invalid_evidence_request_ignored",
                {
                    "candidate_id": dossier.candidate.id,
                    "request_id": request.id,
                    "mismatch": mismatch,
                },
            )
    for note in dossier.notes:
        if note.candidate_id != dossier.candidate.id:
            _trace(
                batch,
                "cross_candidate_evidence_ignored",
                {
                    "candidate_id": dossier.candidate.id,
                    "note_candidate_id": note.candidate_id,
                    "request_id": note.request_id,
                },
            )
            continue
        bound_request = request_by_id.get(note.request_id)
        if bound_request is None:
            _trace(
                batch, "orphan_evidence_ignored",
                {"candidate_id": dossier.candidate.id, "request_id": note.request_id},
            )
            continue
    return bound_evidence(dossier)


# ── synthesis payload ────────────────────────────────────────────────────────


def _synthesis_payload(
    dossier: CandidateDossier,
    evidence: list[BoundEvidence],
) -> str:
    primary = _primary_tag(dossier)
    policy = policy_for(primary)
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
    profile = dossier.risk_profile
    return _stable_json({
        "candidate_alias": "C001",
        "candidate": {
            "type": dossier.candidate.type,
            "claim": dossier.candidate.claim,
            "file": dossier.candidate.file,
            "line": dossier.candidate.line,
        },
        "task_patch": dossier.task.patch,
        "primary_tag": primary.value,
        "task_tags": [
            tag.value
            for tag, score in (profile.tag_scores.items() if profile else ())
            if score > 0
        ],
        "requests": requests_payload,
        "allowed_factors": [
            {"id": factor.id, "description": factor.description}
            for factor in policy.factors
        ],
    })


# ── LLM synthesis ────────────────────────────────────────────────────────────


def _synthesize(
    dossier: CandidateDossier,
    evidence: list[BoundEvidence],
    *,
    judge_llm: Any,
    structured_method: str,
    max_retries: int,
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
                ("user", _synthesis_payload(dossier, evidence)),
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


# ── findings by ID for severity resolution ───────────────────────────────────


def _findings_by_id(
    evidence: list[BoundEvidence],
) -> dict[str, list[EvidenceFinding]]:
    result: dict[str, list[EvidenceFinding]] = {}
    for item in evidence:
        result.setdefault(item.finding.evidence_id, []).append(item.finding)
    return result


# ── main entry ───────────────────────────────────────────────────────────────


def judge_candidates(
    assembly: DossierAssembly,
    *,
    judge_llm: Any,
    structured_method: str,
    max_retries: int,
) -> JudgeBatch:
    batch = JudgeBatch()

    # Binding failures → drop
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

    for dossier in assembly.dossiers:
        findings = _purpose_findings(dossier, batch)

        # Evidence gate
        gate = _gate_candidate(findings)
        if gate is not None:
            reason_code, reason = gate
            verdict = Verdict(dossier.candidate.id, "drop", reason_code, reason)
            batch.verdicts.append(verdict)
            _trace(
                batch, "judge_verdict",
                {"candidate_id": verdict.candidate_id, "action": "drop",
                 "reason_code": reason_code},
            )
            continue

        # LLM synthesis
        primary = _primary_tag(dossier)
        policy = policy_for(primary)
        assessment = _synthesize(
            dossier, findings,
            judge_llm=judge_llm,
            structured_method=structured_method,
            max_retries=max_retries,
        )

        if assessment is None:
            # Synthesis failed → default severity, keep
            resolved_severity = policy.default_severity
            verdict = Verdict(
                dossier.candidate.id, "keep",
                "severity_evidence_incomplete",
                "LLM synthesis failed, using policy default severity",
                resolved_severity=resolved_severity,
            )
            batch.verdicts.append(verdict)
            issue = dossier.candidate.to_issue().model_copy(
                update={"severity": resolved_severity}
            )
            batch.final_issues.append(issue)
            batch.final_candidate_ids.append(dossier.candidate.id)
            _trace(
                batch, "judge_verdict",
                {"candidate_id": verdict.candidate_id, "action": "keep",
                 "reason_code": "severity_evidence_incomplete",
                 "resolved_severity": resolved_severity.value},
            )
            _trace(
                batch, "severity_resolved",
                {"candidate_id": dossier.candidate.id,
                 "matched_rule": f"{primary.value.lower()}.default",
                 "severity": resolved_severity.value},
            )
            continue

        findings_map = _findings_by_id(findings)
        unknown_evidence_ids = sorted({
            evidence_id
            for factor in assessment.severity_factors
            for evidence_id in factor.evidence_ids
            if evidence_id not in findings_map
        })
        if unknown_evidence_ids:
            _trace(
                batch,
                "unknown_evidence_citation_ignored",
                {
                    "candidate_id": dossier.candidate.id,
                    "evidence_ids": unknown_evidence_ids,
                },
            )

        # Post-synthesis adjudication
        if assessment.claim_status == "refuted" or assessment.counter_effect == "complete":
            verdict = Verdict(
                dossier.candidate.id, "drop",
                "synthesized_counter_evidence",
                assessment.reason or "synthesis refuted candidate",
            )
            batch.verdicts.append(verdict)
            _trace(
                batch, "judge_verdict",
                {"candidate_id": verdict.candidate_id, "action": "drop",
                 "reason_code": "synthesized_counter_evidence"},
            )
            continue

        if assessment.claim_status == "unresolved":
            verdict = Verdict(
                dossier.candidate.id, "drop",
                "evidence_conflict_unresolved",
                "; ".join(assessment.conflicts) or "evidence conflicts unresolved",
            )
            batch.verdicts.append(verdict)
            _trace(
                batch, "judge_verdict",
                {"candidate_id": verdict.candidate_id, "action": "drop",
                 "reason_code": "evidence_conflict_unresolved"},
            )
            continue

        # claim_status == "supported" → severity resolution
        resolution = resolve_severity(primary, assessment.severity_factors, findings_map)
        verdict = Verdict(
            dossier.candidate.id, "keep",
            "severity_resolved",
            f"resolved to {resolution.severity.value} via {resolution.matched_rule}",
            resolved_severity=resolution.severity,
        )
        batch.verdicts.append(verdict)
        issue = dossier.candidate.to_issue().model_copy(
            update={"severity": resolution.severity}
        )
        batch.final_issues.append(issue)
        batch.final_candidate_ids.append(dossier.candidate.id)
        _trace(
            batch, "judge_verdict",
            {"candidate_id": verdict.candidate_id, "action": "keep",
             "reason_code": "severity_resolved",
             "resolved_severity": resolution.severity.value},
        )
        _trace(
            batch, "severity_resolved",
            {"candidate_id": dossier.candidate.id,
             "matched_rule": resolution.matched_rule,
             "severity": resolution.severity.value,
             "proven_factors": list(resolution.proven_factors),
             "missing_critical_factors": list(resolution.missing_critical_factors)},
        )

    return batch


__all__ = ["JudgeBatch", "judge_candidates"]
