"""Purpose-aware candidate adjudication for the ReviewCouncil."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.council import (
    EvidenceFinding,
    EvidencePurpose,
    JudgeDecisions,
    Verdict,
)
from codeguard_agent.models.schemas import Issue, Severity
from codeguard_agent.pipeline.evidence_planner import CandidateDossier, DossierAssembly
from codeguard_agent.pipeline.stages.aggregation import (
    _MergePlan,
    _format_issues,
    deduplicate,
)

logger = logging.getLogger("codeguard")
_PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"


@dataclass
class JudgeBatch:
    verdicts: list[Verdict] = field(default_factory=list)
    final_issues: list[Issue] = field(default_factory=list)
    trace: list[tuple[str, str]] = field(default_factory=list)


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _trace_verdict(
    batch: JudgeBatch,
    dossier: CandidateDossier | None,
    verdict: Verdict,
    *,
    task_id: str,
) -> None:
    batch.trace.append(
        (
            "judge_verdict",
            _stable_json(
                {
                    "candidate_id": verdict.candidate_id,
                    "task_id": task_id,
                    "action": verdict.action,
                    "reason_code": verdict.reason_code,
                    "reason": verdict.reason,
                    "requested_purpose": verdict.requested_purpose,
                    "severity_override": (
                        verdict.severity_override.value
                        if verdict.severity_override is not None
                        else None
                    ),
                }
            ),
        )
    )


def _purpose_findings(
    dossier: CandidateDossier,
    batch: JudgeBatch,
) -> list[tuple[EvidencePurpose, EvidenceFinding]]:
    request_by_id = {request.id: request for request in dossier.requests}
    result: list[tuple[EvidencePurpose, EvidenceFinding]] = []
    for note in dossier.notes:
        request = request_by_id.get(note.request_id)
        if request is None:
            batch.trace.append(
                (
                    "orphan_evidence_ignored",
                    _stable_json(
                        {
                            "candidate_id": dossier.candidate.id,
                            "request_id": note.request_id,
                            "finding_count": len(note.findings),
                        }
                    ),
                )
            )
            continue
        result.extend((request.purpose, finding) for finding in note.findings)
    return result


def _fallback_verdict(
    dossier: CandidateDossier,
    findings: list[tuple[EvidencePurpose, EvidenceFinding]],
) -> Verdict:
    candidate = dossier.candidate
    has_severity_contradiction = any(
        purpose == "severity"
        and finding.strength == "direct"
        and finding.relation == "contradicts"
        for purpose, finding in findings
    )
    all_insufficient = bool(findings) and all(
        finding.relation == "insufficient" for _, finding in findings
    )
    if has_severity_contradiction:
        override = _normalized_severity(candidate.severity_proposal)
        if override is None:
            return Verdict(
                candidate_id=candidate.id,
                action="keep",
                reason_code="direct_severity_evidence",
                reason="INFO 已是最低级别，保留候选",
            )
        return Verdict(
            candidate_id=candidate.id,
            action="downgrade",
            reason_code="direct_severity_evidence",
            reason="直接级别反证要求下调候选级别",
            severity_override=override,
        )
    if all_insufficient and candidate.severity_proposal is Severity.CRITICAL:
        return Verdict(
            candidate_id=candidate.id,
            action="downgrade",
            reason_code="critical_insufficient_evidence",
            reason="证据要求对候选级别做保守下调",
            severity_override=Severity.WARNING,
        )
    return Verdict(
        candidate_id=candidate.id,
        action="keep",
        reason_code="conservative_keep",
        reason="无可用终审模型，保守保留候选",
    )


def _judge_payload(
    dossier: CandidateDossier,
    findings: list[tuple[EvidencePurpose, EvidenceFinding]],
    *,
    final_round: bool,
) -> str:
    requests = []
    notes_by_request = {note.request_id: note for note in dossier.notes}
    for request in dossier.requests:
        note = notes_by_request.get(request.id)
        requests.append(
            {
                "strategy_id": request.strategy_id,
                "purpose": request.purpose,
                "question": request.question,
                "findings": (
                    [finding.model_dump(mode="json") for finding in note.findings]
                    if note is not None
                    else []
                ),
            }
        )
    profile = dossier.risk_profile
    return _stable_json(
        {
            "candidate_alias": "C001",
            "candidate": {
                "type": dossier.candidate.type,
                "claim": dossier.candidate.claim,
                "severity": dossier.candidate.severity_proposal.value,
                "file": dossier.candidate.file,
                "line": dossier.candidate.line,
            },
            "task_patch": dossier.task.patch,
            "risk": {
                "tags": [
                    tag.value
                    for tag, score in (profile.tag_scores.items() if profile else ())
                    if score > 0
                ],
                "signals": [
                    signal.model_dump(mode="json")
                    for signal in (profile.signals if profile else ())
                ],
            },
            "task_context": (
                dossier.context_bundle.model_dump(mode="json")
                if dossier.context_bundle is not None
                else None
            ),
            "requests": requests,
            "final_round": final_round,
            "allowed_actions": (
                ["keep", "drop", "downgrade"]
                if final_round
                else ["keep", "drop", "downgrade", "needs_more_evidence"]
            ),
        }
    )


def _normalized_severity(candidate_severity: Severity) -> Severity | None:
    if candidate_severity is Severity.CRITICAL:
        return Severity.WARNING
    if candidate_severity is Severity.WARNING:
        return Severity.INFO
    return None


def _llm_verdict(
    dossier: CandidateDossier,
    findings: list[tuple[EvidencePurpose, EvidenceFinding]],
    *,
    judge_llm: Any,
    structured_method: str,
    evidence_round: int,
    max_evidence_rounds: int,
    max_retries: int,
) -> Verdict | None:
    final_round = evidence_round >= max_evidence_rounds
    try:
        structured = judge_llm.with_structured_output(
            JudgeDecisions,
            method=structured_method,
        )
        system_prompt = (_PROMPT_DIR / "council-judge.txt").read_text(
            encoding="utf-8"
        )
        if not final_round:
            system_prompt += (
                "\n\n非最后一轮还可返回 action=needs_more_evidence；"
                "此时必须只填写 requested_purpose=support|counter|severity，"
                "不得选择工具。"
            )
        result = invoke_with_retry(
            structured,
            [
                ("system", system_prompt),
                ("user", _judge_payload(dossier, findings, final_round=final_round)),
            ],
            max_retries=max_retries,
        )
        if result is None:
            return None
        parsed = result if isinstance(result, JudgeDecisions) else JudgeDecisions.model_validate(result)
    except Exception as exc:  # noqa: BLE001 - 终审失败走确定性矩阵
        logger.warning("CouncilJudge LLM 调用失败，使用确定性矩阵: %s", exc)
        return None
    decisions = [decision for decision in parsed.decisions if decision.candidate_id == "C001"]
    if not decisions:
        return None
    decision = decisions[0]
    has_severity_contradiction = any(
        purpose == "severity"
        and finding.strength == "direct"
        and finding.relation == "contradicts"
        for purpose, finding in findings
    )
    if has_severity_contradiction and decision.action not in {"keep", "downgrade"}:
        return _fallback_verdict(dossier, findings)
    if decision.action == "needs_more_evidence":
        if final_round or decision.requested_purpose is None:
            if dossier.candidate.severity_proposal is Severity.CRITICAL:
                return Verdict(
                    dossier.candidate.id,
                    "downgrade",
                    "last_round_normalized",
                    "最后一轮不再补证，CRITICAL 收口为 WARNING",
                    severity_override=Severity.WARNING,
                )
            return Verdict(
                dossier.candidate.id,
                "keep",
                "last_round_normalized",
                "最后一轮不再补证，保守保留",
            )
        return Verdict(
            dossier.candidate.id,
            "needs_more_evidence",
            "llm_judge",
            decision.reason,
            requested_purpose=decision.requested_purpose,
        )
    severity_override = decision.adjusted_severity
    if decision.action == "downgrade" and severity_override is None:
        severity_override = _normalized_severity(dossier.candidate.severity_proposal)
    return Verdict(
        candidate_id=dossier.candidate.id,
        action=decision.action,
        reason_code="llm_judge",
        reason=decision.reason,
        suggested_target_id=decision.merge_target_id,
        severity_override=severity_override,
    )


def _deduplicate_survivors(
    dossiers: list[CandidateDossier],
    verdicts: list[Verdict],
) -> tuple[list[CandidateDossier], list[Verdict]]:
    if len(dossiers) < 2:
        return dossiers, []
    deduped_issues = deduplicate([dossier.candidate.to_issue() for dossier in dossiers])
    survivors: list[CandidateDossier] = []
    used: set[str] = set()
    merge_verdicts: list[Verdict] = []
    for issue in deduped_issues:
        best = next(
            (
                dossier
                for dossier in dossiers
                if dossier.candidate.id not in used
                and dossier.candidate.to_issue() == issue
            ),
            None,
        )
        if best is None:
            continue
        survivors.append(best)
        used.add(best.candidate.id)
        best_file = best.candidate.file.replace("\\", "/").lower()
        for other in dossiers:
            if other.candidate.id in used:
                continue
            other_file = other.candidate.file.replace("\\", "/").lower()
            if (
                other_file == best_file
                and other.candidate.line == best.candidate.line
                and other.candidate.type.lower() == best.candidate.type.lower()
            ):
                used.add(other.candidate.id)
                merge_verdicts.append(
                    Verdict(
                        other.candidate.id,
                        "merge",
                        "aggregation_merge",
                        f"与 {best.candidate.id} 指向同一底层问题",
                        suggested_target_id=best.candidate.id,
                    )
                )
    for dossier in dossiers:
        if dossier.candidate.id not in used:
            survivors.append(dossier)
            used.add(dossier.candidate.id)
    return survivors, merge_verdicts


def _semantic_merge_survivors(
    dossiers: list[CandidateDossier],
    *,
    judge_llm: Any,
    structured_method: str,
    max_retries: int,
) -> tuple[list[CandidateDossier], list[Verdict]]:
    """复用成熟 aggregation merge plan，在候选裁决后做全局语义合并。"""
    if len(dossiers) < 2 or judge_llm is None:
        return dossiers, []
    issues = [dossier.candidate.to_issue() for dossier in dossiers]
    try:
        structured = judge_llm.with_structured_output(
            _MergePlan,
            method=structured_method,
        )
        result = invoke_with_retry(
            structured,
            [
                (
                    "system",
                    (_PROMPT_DIR / "aggregation-system.txt").read_text(encoding="utf-8"),
                ),
                (
                    "user",
                    "候选级裁决已完成；仅合并确属同一底层问题的条目。\n"
                    + _format_issues(issues),
                ),
            ],
            max_retries=max_retries,
        )
        if result is None:
            return dossiers, []
        plan = result if isinstance(result, _MergePlan) else _MergePlan.model_validate(result)
    except Exception as exc:  # noqa: BLE001 - 语义合并失败保留规则去重结果
        logger.debug("CouncilJudge 语义合并失败，保留规则去重结果: %s", exc)
        return dossiers, []

    severity_rank = {Severity.INFO: 1, Severity.WARNING: 2, Severity.CRITICAL: 3}
    merged_ids: set[str] = set()
    merge_verdicts: list[Verdict] = []
    for group in plan.groups:
        indexes: list[int] = []
        for member in group.members:
            index = member - 1
            if 0 <= index < len(dossiers) and index not in indexes:
                indexes.append(index)
        if len(indexes) < 2 or any(
            dossiers[index].candidate.id in merged_ids for index in indexes
        ):
            continue
        best_index = max(
            indexes,
            key=lambda index: (
                severity_rank[dossiers[index].candidate.severity_proposal],
                dossiers[index].candidate.confidence,
                -index,
            ),
        )
        target = dossiers[best_index].candidate
        for index in indexes:
            candidate = dossiers[index].candidate
            merged_ids.add(candidate.id)
            if index == best_index:
                continue
            merge_verdicts.append(
                Verdict(
                    candidate.id,
                    "merge",
                    "aggregation_merge",
                    f"语义聚合:与 {target.id} 指向同一底层问题",
                    suggested_target_id=target.id,
                )
            )
    dropped_ids = {verdict.candidate_id for verdict in merge_verdicts}
    return (
        [dossier for dossier in dossiers if dossier.candidate.id not in dropped_ids],
        merge_verdicts,
    )


def judge_candidates(
    assembly: DossierAssembly,
    *,
    judge_llm: Any,
    structured_method: str,
    evidence_round: int,
    max_evidence_rounds: int,
    max_retries: int,
) -> JudgeBatch:
    """按 purpose-aware 矩阵逐候选裁决，再执行全局去重与输出转换。"""
    batch = JudgeBatch()
    for failure in assembly.failures:
        verdict = Verdict(
            failure.candidate.id,
            "drop",
            "invalid_candidate_binding",
            failure.reason,
        )
        batch.verdicts.append(verdict)
        _trace_verdict(batch, None, verdict, task_id=failure.candidate.task_id)

    kept_dossiers: list[CandidateDossier] = []
    severity_overrides: dict[str, Severity] = {}
    for dossier in assembly.dossiers:
        findings = _purpose_findings(dossier, batch)
        direct_counter = any(
            purpose == "counter"
            and finding.strength == "direct"
            and finding.relation == "contradicts"
            for purpose, finding in findings
        )
        if direct_counter:
            verdict = Verdict(
                dossier.candidate.id,
                "drop",
                "direct_counter_evidence",
                "直接反证足以排除候选",
            )
        elif judge_llm is None:
            verdict = _fallback_verdict(dossier, findings)
        else:
            verdict = _llm_verdict(
                dossier,
                findings,
                judge_llm=judge_llm,
                structured_method=structured_method,
                evidence_round=evidence_round,
                max_evidence_rounds=max_evidence_rounds,
                max_retries=max_retries,
            ) or _fallback_verdict(dossier, findings)
        batch.verdicts.append(verdict)
        _trace_verdict(batch, dossier, verdict, task_id=dossier.task.id)
        if verdict.action == "needs_more_evidence":
            batch.trace.append(
                (
                    "judge_requested_more_evidence",
                    _stable_json(
                        {
                            "candidate_id": dossier.candidate.id,
                            "requested_purpose": verdict.requested_purpose,
                            "evidence_round": evidence_round,
                        }
                    ),
                )
            )
        if verdict.action not in {"drop", "merge"}:
            kept_dossiers.append(dossier)
        if verdict.action == "downgrade" and verdict.severity_override is not None:
            severity_overrides[dossier.candidate.id] = verdict.severity_override

    kept_dossiers, merge_verdicts = _deduplicate_survivors(
        kept_dossiers,
        batch.verdicts,
    )
    semantic_survivors, semantic_verdicts = _semantic_merge_survivors(
        kept_dossiers,
        judge_llm=judge_llm,
        structured_method=structured_method,
        max_retries=max_retries,
    )
    kept_dossiers = semantic_survivors
    merge_verdicts.extend(semantic_verdicts)
    for verdict in merge_verdicts:
        batch.verdicts.append(verdict)
        dossier = next(
            item for item in assembly.dossiers if item.candidate.id == verdict.candidate_id
        )
        _trace_verdict(batch, dossier, verdict, task_id=dossier.task.id)
    merged_ids = {verdict.candidate_id for verdict in merge_verdicts}
    for dossier in kept_dossiers:
        if dossier.candidate.id in merged_ids:
            continue
        issue = dossier.candidate.to_issue()
        override = severity_overrides.get(dossier.candidate.id)
        if override is not None:
            issue = issue.model_copy(update={"severity": override})
        batch.final_issues.append(issue)
    return batch


__all__ = ["JudgeBatch", "judge_candidates"]
