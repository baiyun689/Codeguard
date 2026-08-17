"""裁决模块:批量 EvidenceJudge + 组内合并(Evidence Ledger)。

Verifier 只证明证据真实可用,支持/反驳/去留/定级合并为一次批量
EvidenceJudge:每批 ≤8 候选、最多 4 批并行;输出经确定性合同校验,
违规重试/二分拆批,单候选最终失败 fail-closed(不输出 Issue,完整留痕)。
`evidence_mode=off` 消融档走 judge_direct:输入无证据 ID,输出同构。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.council import Verdict
from codeguard_agent.models.evidence import (
    CandidateVerification,
    EvidenceArtifact,
    EvidenceJudgeAssessment,
    EvidenceJudgeBatch,
    EvidenceRole,
    EvidenceSourceKind,
)
from codeguard_agent.models.schemas import Issue, Severity
from codeguard_agent.pipeline.concurrency import run_bounded_parallel
from codeguard_agent.pipeline.council.dedup import CandidateGroup
from codeguard_agent.pipeline.evidence.graph_response import summarize_graph
from codeguard_agent.pipeline.evidence.planner import CandidateDossier, DossierAssembly

logger = logging.getLogger("codeguard")

_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"

_JUDGE_BATCH_SIZE = 8
_JUDGE_MAX_PARALLEL_BATCHES = 4
_FILE_PAYLOAD_MAX_CHARS = 2000
_GRAPH_TOOLS = ("inspect_change_impact", "inspect_security_path", "inspect_structure")


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass
class VerdictBatch:
    """一批候选的裁决结果与最终 Issue 映射(完整档/消融档共用)。"""

    verdicts: list[Verdict] = field(default_factory=list)
    final_issues: list[Issue] = field(default_factory=list)
    final_candidate_ids: list[str] = field(default_factory=list)
    trace: list[tuple[str, str]] = field(default_factory=list)


def _trace(batch: VerdictBatch, event: str, detail: dict[str, object]) -> None:
    batch.trace.append((event, _stable_json(detail)))


def _unique_text(values: Sequence[str]) -> str:
    return "；".join(dict.fromkeys(value.strip() for value in values if value.strip()))


def consolidate_groups(
    batch: VerdictBatch,
    supported: Sequence[tuple[str, Issue]],
    candidate_groups: Sequence[CandidateGroup],
) -> None:
    """按严格等价组汇总已支持成员;任何未获支持成员都不影响其兄弟。

    组内形状不一致(文件/类型/裁决后严重度)安全拆回多条;合并取最小正行号,
    类型/消息/建议去重拼接、置信度取 min。
    """
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

        # 类型、裁决后严重度或文件不同,说明实际影响并不等价,安全拆回多条。
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


# ── Judge 载荷与输出合同 ────────────────────────────────────────────────


def _evidence_item_payload(
    dossier: CandidateDossier,
    verification: CandidateVerification,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """把候选可见的已验证证据渲染为 Judge 输入条目。

    返回 (条目列表, [(批内 F 编号, artifact_id) 映射])。patch 用 candidate
    line 所在 hunk(line 不在 changed lines 时带 candidate_line_unknown
    限制);图 payload 摘要化、文件 payload 截 2000 字符(源文档 §8.2)。
    """
    items: list[dict[str, Any]] = []
    mapping: list[tuple[str, str]] = []
    for evidence in verification.valid_evidence:
        limitations = list(evidence.limitations)
        if evidence.source_kind is EvidenceSourceKind.TASK_PATCH:
            if (
                dossier.candidate.line > 0
                and dossier.candidate.line not in dossier.task.changed_lines
            ):
                limitations.append("candidate_line_unknown")
            content = evidence.content
        elif evidence.tool in _GRAPH_TOOLS:
            content = summarize_graph(evidence.content)
        else:
            content = evidence.content[:_FILE_PAYLOAD_MAX_CHARS]
            if len(evidence.content) > _FILE_PAYLOAD_MAX_CHARS:
                limitations.append("payload_truncated")
        fact_id = f"F{len(items) + 1:03d}"
        mapping.append((fact_id, evidence.artifact_id))
        items.append({
            "evidence_id": fact_id,
            "source_kind": evidence.source_kind.value,
            "tool": evidence.tool,
            "arguments": evidence.arguments,
            "content": content,
            "limitations": limitations,
        })
    return items, mapping


def _judge_payload(
    dossiers: list[CandidateDossier],
    verifications: dict[str, CandidateVerification],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    """整批 Judge 输入:候选 + grounding + 证据条目;维护 F 编号→artifact_id 映射。"""
    candidates: list[dict[str, Any]] = []
    fact_map: dict[str, dict[str, str]] = {}
    for dossier in dossiers:
        verification = verifications[dossier.candidate.id]
        items, mapping = _evidence_item_payload(dossier, verification)
        candidate_entry = {
            "candidate_id": dossier.candidate.id,
            "candidate": {
                "file": dossier.candidate.file,
                "line": dossier.candidate.line,
                "type": dossier.candidate.type,
                "claim": dossier.candidate.claim,
                "severity_proposal": dossier.candidate.severity_proposal.value,
                "confidence": dossier.candidate.confidence,
                "suggestion": dossier.candidate.suggestion,
                "source_agent": dossier.candidate.source_agent,
            },
            "grounding_status": verification.grounding_status,
            "evidence": items,
        }
        candidates.append(candidate_entry)
        fact_map[dossier.candidate.id] = dict(mapping)
    return candidates, fact_map


# ── 输出确定性校验(源文档 §8.4 + 修正⑤) ───────────────────────────────


def _role_of(artifact_id: str, dossier: CandidateDossier) -> EvidenceRole:
    for ref in dossier.candidate.evidence_refs:
        if ref.artifact_id == artifact_id:
            return ref.declared_role
    return EvidenceRole.MECHANISM  # 自动 patch 引用


def _validate_assessment(
    item: EvidenceJudgeAssessment,
    *,
    dossier: CandidateDossier,
    fact_map: dict[str, str],
    violations: list[str],
) -> EvidenceJudgeAssessment | None:
    """单候选裁决合同校验;违约返回 None(该候选 fail-closed)。"""
    visible = set(fact_map.keys())  # 批内 F 编号
    supporting = [fid for fid in item.supporting_evidence_ids]
    counter = [fid for fid in item.counter_evidence_ids]
    if item.action == "keep":
        if not supporting:
            violations.append("keep_without_supporting")
            return None
        if item.severity is None:
            violations.append("keep_without_severity")
            return None
        if item.severity in {Severity.WARNING, Severity.CRITICAL} and not supporting:
            violations.append("severity_without_supporting")
            return None
        if dossier.candidate.source_agent == "maintainability" and item.severity is Severity.CRITICAL:
            violations.append("maintainability_critical")
            return None
        # 修正⑤:LOCATION 只说明位置,不能单独满足 keep 的支持要求。
        artifact_ids = [fact_map.get(fid, "") for fid in supporting]
        if artifact_ids and all(
            _role_of(artifact_id, dossier) is EvidenceRole.LOCATION
            for artifact_id in artifact_ids
        ):
            violations.append("supporting_all_location")
            return None
    else:
        if item.severity is not None:
            violations.append("drop_with_severity")
            return None
    if supporting:
        unknown = [fid for fid in supporting if fid not in visible]
        if unknown:
            violations.append(f"supporting_unknown_id:{','.join(unknown)}")
            return None
    if counter:
        unknown = [fid for fid in counter if fid not in visible]
        if unknown:
            violations.append(f"counter_unknown_id:{','.join(unknown)}")
            return None
    if set(supporting) & set(counter):
        violations.append("supporting_counter_overlap")
        return None
    return item


def _invoke_batch(
    payload: list[dict[str, Any]],
    *,
    judge_llm: Any,
    structured_method: str,
    max_retries: int,
    prompt_file: str,
) -> EvidenceJudgeBatch | None:
    """调用批量 Judge;None/异常重试一次,再失败返回 None(由调用方二分)。"""
    structured = judge_llm.with_structured_output(
        EvidenceJudgeBatch, method=structured_method
    )
    system_prompt = (_PROMPT_DIR / prompt_file).read_text(encoding="utf-8")
    for attempt in range(2):
        try:
            result = invoke_with_retry(
                structured,
                [
                    ("system", system_prompt),
                    ("user", _stable_json({"candidates": payload})),
                ],
                max_retries=max_retries,
            )
            if result is None:
                continue
            if not isinstance(result, EvidenceJudgeBatch):
                result = EvidenceJudgeBatch.model_validate(result)
            return result
        except Exception as exc:  # noqa: BLE001 Judge 失败走 fail-closed,不抛断
            logger.warning("evidence judge batch invoke failed (attempt %d): %s", attempt + 1, exc)
    return None


def _judge_chunk(
    chunk: list[CandidateDossier],
    *,
    verifications: dict[str, CandidateVerification],
    artifacts: dict[str, EvidenceArtifact],
    judge_llm: Any,
    structured_method: str,
    max_retries: int,
    prompt_file: str,
    batch: VerdictBatch,
) -> list[tuple[CandidateDossier, EvidenceJudgeAssessment | None, str]]:
    """批内裁决:整批 → 输出合同校验 → 失败二分;单候选失败 fail-closed。"""
    if judge_llm is None:
        # mock 模式:确定性 keep + 提案严重度(真实 LLM 故障绝不走此路径)。
        return [
            (
                dossier,
                EvidenceJudgeAssessment(
                    candidate_id=dossier.candidate.id,
                    action="keep",
                    severity=dossier.candidate.severity_proposal,
                    reason="mock_deterministic_keep",
                ),
                "mock_deterministic_keep",
            )
            for dossier in chunk
        ]
    payload, fact_map = _judge_payload(chunk, verifications)
    result = _invoke_batch(
        payload,
        judge_llm=judge_llm,
        structured_method=structured_method,
        max_retries=max_retries,
        prompt_file=prompt_file,
    )
    _trace(batch, "evidence_judge_batch_started", {
        "candidate_ids": [dossier.candidate.id for dossier in chunk],
    })
    if result is None:
        if len(chunk) == 1:
            _trace(batch, "evidence_judge_batch_failed", {
                "candidate_ids": [chunk[0].candidate.id],
            })
            return [(chunk[0], None, "verification_failed")]
        mid = len(chunk) // 2
        return _judge_chunk(
            chunk[:mid],
            verifications=verifications, artifacts=artifacts,
            judge_llm=judge_llm, structured_method=structured_method,
            max_retries=max_retries, prompt_file=prompt_file, batch=batch,
        ) + _judge_chunk(
            chunk[mid:],
            verifications=verifications, artifacts=artifacts,
            judge_llm=judge_llm, structured_method=structured_method,
            max_retries=max_retries, prompt_file=prompt_file, batch=batch,
        )
    # 批级合同:每个输入候选必须且只能返回一次,不接受未知候选 ID。
    by_id = {dossier.candidate.id: dossier for dossier in chunk}
    assessments: dict[str, EvidenceJudgeAssessment] = {}
    violations: list[str] = []
    seen: set[str] = set()
    for raw_item in result.assessments:
        if raw_item.candidate_id not in by_id or raw_item.candidate_id in seen:
            violations.append(f"invalid_candidate_id:{raw_item.candidate_id}")
            continue
        seen.add(raw_item.candidate_id)
        assessments[raw_item.candidate_id] = raw_item
    outcomes: list[tuple[CandidateDossier, EvidenceJudgeAssessment | None, str]] = []
    for dossier in chunk:
        item = assessments.get(dossier.candidate.id)
        if item is None:
            violations.append(f"missing_assessment:{dossier.candidate.id}")
            outcomes.append((dossier, None, "verification_failed"))
            continue
        validated = _validate_assessment(
            item,
            dossier=dossier,
            fact_map=fact_map[dossier.candidate.id],
            violations=violations,
        )
        outcomes.append(
            (dossier, validated, "contract_violation" if validated is None else "ok")
        )
    if violations:
        _trace(batch, "evidence_judge_contract_violations", {
            "violations": violations,
        })
    return outcomes


def _finalize_assessment(
    dossier: CandidateDossier,
    assessment: EvidenceJudgeAssessment | None,
    verdict_reason: str,
    batch: VerdictBatch,
    *,
    event: str,
) -> tuple[Verdict, Issue | None]:
    candidate = dossier.candidate
    if assessment is None:
        verdict = Verdict(
            candidate.id, "drop", "verification_failed",
            "Judge 失败或输出合同违约,按 fail-closed 不输出",
        )
        _trace(batch, event, {
            "candidate_id": candidate.id, "action": "drop",
            "reason_code": "verification_failed",
        })
        return verdict, None
    if assessment.action == "drop":
        reason_code = (
            "no_supporting_evidence"
            if not assessment.supporting_evidence_ids
            else "synthesized_evidence_drop"
        )
        verdict = Verdict(candidate.id, "drop", reason_code, assessment.reason)
        _trace(batch, event, {
            "candidate_id": candidate.id, "action": "drop",
            "reason_code": reason_code,
        })
        return verdict, None
    verdict = Verdict(
        candidate.id, "keep", verdict_reason, assessment.reason,
        resolved_severity=assessment.severity,
        supported=bool(assessment.supporting_evidence_ids),
    )
    issue = candidate.to_issue().model_copy(update={"severity": assessment.severity})
    _trace(batch, event, {
        "candidate_id": candidate.id, "action": "keep",
        "reason_code": verdict_reason,
        "resolved_severity": assessment.severity.value if assessment.severity else None,
        "supporting_evidence_ids": list(assessment.supporting_evidence_ids),
    })
    return verdict, issue


def judge_with_evidence(
    assembly: DossierAssembly,
    verifications: dict[str, CandidateVerification],
    artifacts: dict[str, EvidenceArtifact],
    *,
    judge_llm: Any,
    structured_method: str,
    max_retries: int,
    candidate_groups: Sequence[CandidateGroup] = (),
) -> VerdictBatch:
    """完整档裁决:绑定失败/验证淘汰 → 批量 EvidenceJudge → 组内合并。"""
    batch = VerdictBatch()
    for failure in assembly.failures:
        verdict = Verdict(failure.candidate.id, "drop", "invalid_candidate_binding", failure.reason)
        batch.verdicts.append(verdict)
        _trace(batch, "judge_verdict", {
            "candidate_id": verdict.candidate_id, "action": "drop",
            "reason_code": verdict.reason_code,
        })
    eligible = [
        dossier
        for dossier in assembly.dossiers
        if verifications.get(dossier.candidate.id) is not None
        and verifications[dossier.candidate.id].eligible_for_judge
    ]
    for dossier in assembly.dossiers:
        verification = verifications.get(dossier.candidate.id)
        if verification is None or verification.eligible_for_judge:
            continue
        verdict = Verdict(
            dossier.candidate.id, "drop",
            verification.rejection_reason or "ineligible",
            verification.rejection_reason or "",
        )
        batch.verdicts.append(verdict)
        _trace(batch, "judge_verdict", {
            "candidate_id": verdict.candidate_id, "action": "drop",
            "reason_code": verdict.reason_code,
        })
    if not eligible:
        return batch

    chunks = [
        eligible[index:index + _JUDGE_BATCH_SIZE]
        for index in range(0, len(eligible), _JUDGE_BATCH_SIZE)
    ]
    outcomes = run_bounded_parallel(
        chunks,
        lambda chunk: _judge_chunk(
            chunk,
            verifications=verifications, artifacts=artifacts,
            judge_llm=judge_llm, structured_method=structured_method,
            max_retries=max_retries, prompt_file="evidence-judge.txt",
            batch=batch,
        ),
        max_workers=_JUDGE_MAX_PARALLEL_BATCHES,
    )
    supported: list[tuple[str, Issue]] = []
    for chunk_outcomes in outcomes:
        if chunk_outcomes is None:
            continue
        for dossier, assessment, verdict_reason in chunk_outcomes:
            verdict, issue = _finalize_assessment(
                dossier, assessment, verdict_reason, batch, event="judge_verdict"
            )
            batch.verdicts.append(verdict)
            if issue is not None:
                supported.append((dossier.candidate.id, issue))
    consolidate_groups(batch, supported, candidate_groups)
    return batch


def _direct_payload(dossier: CandidateDossier) -> dict[str, Any]:
    return {
        "candidate_alias": "C001",
        "candidate": {
            "type": dossier.candidate.type,
            "claim": dossier.candidate.claim,
            "file": dossier.candidate.file,
            "line": dossier.candidate.line,
            "severity_proposal": dossier.candidate.severity_proposal.value,
            "suggestion": dossier.candidate.suggestion,
            "confidence": dossier.candidate.confidence,
        },
        "task_patch": dossier.task.patch,
    }


def _invoke_direct(
    dossier: CandidateDossier,
    *,
    judge_llm: Any,
    structured_method: str,
    max_retries: int,
) -> EvidenceJudgeAssessment | None:
    """消融档单个候选裁决:输入无证据 ID,输出同构(EvidenceJudgeAssessment,ID 空)。"""
    if judge_llm is None:
        return EvidenceJudgeAssessment(
            candidate_id=dossier.candidate.id,
            action="keep",
            severity=dossier.candidate.severity_proposal,
            reason="mock_deterministic_keep",
        )
    try:
        structured = judge_llm.with_structured_output(
            EvidenceJudgeAssessment, method=structured_method
        )
        system_prompt = (_PROMPT_DIR / "direct-judge.txt").read_text(encoding="utf-8")
        result = invoke_with_retry(
            structured,
            [
                ("system", system_prompt),
                ("user", _stable_json(_direct_payload(dossier))),
            ],
            max_retries=max_retries,
        )
        if result is None:
            return None
        if not isinstance(result, EvidenceJudgeAssessment):
            result = EvidenceJudgeAssessment.model_validate(result)
        if result.candidate_id != "C001":
            logger.warning("direct judge returned unexpected candidate_id: %s", result.candidate_id)
            return None
        return result.model_copy(update={"candidate_id": dossier.candidate.id})
    except Exception:  # noqa: BLE001
        logger.warning("direct judge LLM synthesis failed", exc_info=True)
        return None


def judge_direct(
    assembly: DossierAssembly,
    *,
    judge_llm: Any,
    structured_method: str,
    max_retries: int,
    candidate_groups: Sequence[CandidateGroup] = (),
) -> VerdictBatch:
    """无证据链消融档:输入无证据 ID、无门控,输出 keep/drop/severity 同构。"""
    batch = VerdictBatch()
    for failure in assembly.failures:
        verdict = Verdict(failure.candidate.id, "drop", "invalid_candidate_binding", failure.reason)
        batch.verdicts.append(verdict)
        _trace(batch, "direct_judge_verdict", {
            "candidate_id": verdict.candidate_id, "action": "drop",
            "reason_code": verdict.reason_code,
        })
    if assembly.dossiers:
        results = run_bounded_parallel(
            assembly.dossiers,
            lambda dossier: _invoke_direct(
                dossier, judge_llm=judge_llm,
                structured_method=structured_method, max_retries=max_retries,
            ),
            max_workers=6,
        )
        supported: list[tuple[str, Issue]] = []
        for dossier, assessment in zip(assembly.dossiers, results, strict=True):
            if assessment is None:
                # 消融档基线语义:LLM 不可用时保留提案严重度(与完整档 fail-closed 不同)。
                severity = dossier.candidate.severity_proposal
                verdict = Verdict(
                    dossier.candidate.id, "keep", "direct_assessment_missing",
                    "DirectJudge LLM assessment unavailable; kept with proposed severity",
                    resolved_severity=severity,
                )
                issue = dossier.candidate.to_issue().model_copy(
                    update={"severity": severity}
                )
                batch.verdicts.append(verdict)
                supported.append((dossier.candidate.id, issue))
                _trace(batch, "direct_judge_verdict", {
                    "candidate_id": dossier.candidate.id, "action": "keep",
                    "reason_code": "direct_assessment_missing",
                })
                continue
            verdict, final_issue = _finalize_assessment(
                dossier, assessment, "direct_judge_keep", batch, event="direct_judge_verdict"
            )
            batch.verdicts.append(verdict)
            if final_issue is not None:
                supported.append((dossier.candidate.id, final_issue))
        consolidate_groups(batch, supported, candidate_groups)
    return batch
