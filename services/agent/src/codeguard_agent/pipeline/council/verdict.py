"""裁决模块:确定性门控 + LLM 终审 + 组内合并(ADR-046)。

门控依赖关系分析产出;门控本身零 LLM;终审基于关系三元输出统一裁决;
批量裁决入口(judge_with_evidence/judge_direct)按严格等价组收敛最终 Issue。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.council import CandidateDirectAssessment, FactRelation, Verdict
from codeguard_agent.models.schemas import Issue
from codeguard_agent.pipeline.concurrency import run_bounded_parallel
from codeguard_agent.pipeline.council.dedup import CandidateGroup
from codeguard_agent.pipeline.evidence.planner import CandidateDossier, DossierAssembly

logger = logging.getLogger("codeguard")

_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"


def gate_candidate(relations: Sequence[FactRelation]) -> tuple[str, str] | None:
    """三条确定性证据门控(零 LLM 成本淘汰)。返回 (reason_code, reason) 表示应 drop。"""
    if any(
        item.relation == "contradicts" and item.strength == "direct"
        for item in relations
    ):
        return "direct_counter_evidence", "直接反证足以排除候选"
    if not relations or all(
        item.relation == "insufficient" for item in relations
    ):
        return "evidence_insufficient", "候选没有可用证据"
    if not any(item.relation == "supports" for item in relations):
        return "no_supporting_evidence", "没有 support 证据支持候选主张"
    return None


def synthesize_verdict(
    dossier: CandidateDossier,
    relations: Sequence[FactRelation],
    *,
    judge_llm: Any,
    structured_method: str,
    max_retries: int,
) -> CandidateDirectAssessment | None:
    """终审:基于关系三元输出统一裁决。失败/None 返回 None,由调用方确定性保留。

    裁决模型固定用别名 C001 指向候选;校验通过后重映射回 dossier 真实候选 id,
    调用方拿到的结果可直接落 State。
    """
    if judge_llm is None:
        return None
    try:
        structured = judge_llm.with_structured_output(
            CandidateDirectAssessment,
            method=structured_method,
        )
        system_prompt = (_PROMPT_DIR / "council-judge.txt").read_text(encoding="utf-8")
        result = invoke_with_retry(
            structured,
            [
                ("system", system_prompt),
                ("user", _verdict_payload(dossier, relations)),
            ],
            max_retries=max_retries,
        )
        if result is None:
            return None
        if not isinstance(result, CandidateDirectAssessment):
            result = CandidateDirectAssessment.model_validate(result)
        if result.candidate_id != "C001":
            logger.warning("verdict returned unexpected candidate_id: %s", result.candidate_id)
            return None
        return result.model_copy(update={"candidate_id": dossier.candidate.id})
    except Exception:
        logger.warning("verdict LLM synthesis failed", exc_info=True)
        return None


def _verdict_payload(
    dossier: CandidateDossier,
    relations: Sequence[FactRelation],
) -> str:
    return json.dumps(
        {
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
            "relations": [item.model_dump(mode="json") for item in relations],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass
class VerdictBatch:
    """一批候选的裁决结果与最终 Issue 映射(完整档/消融档共用)。"""

    verdicts: list[Verdict] = field(default_factory=list)
    final_issues: list[Issue] = field(default_factory=list)
    final_candidate_ids: list[str] = field(default_factory=list)
    trace: list[tuple[str, str]] = field(default_factory=list)


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _trace(batch: VerdictBatch, event: str, detail: dict[str, object]) -> None:
    batch.trace.append((event, _stable_json(detail)))


def _unique_text(values: Sequence[str]) -> str:
    return "；".join(dict.fromkeys(value.strip() for value in values if value.strip()))


def consolidate_groups(
    batch: VerdictBatch,
    supported: Sequence[tuple[str, Issue]],
    candidate_groups: Sequence[CandidateGroup],
) -> None:
    """按严格等价组汇总已支持成员；任何未获支持成员都不影响其兄弟。

    组内形状不一致(文件/类型/裁决后严重度)安全拆回多条;合并取最小正行号,
    类型/消息/建议去重拼接、置信度取 min——逐行对齐 judge.py 的 _emit_supported_issues。
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


def _invoke_with_evidence(
    dossier: CandidateDossier,
    relations_by_candidate: dict[str, list[FactRelation]],
    *,
    judge_llm: Any,
    structured_method: str,
    max_retries: int,
) -> tuple[Verdict, Issue | None, str, list[tuple[str, str]]]:
    """单个候选的完整档裁决:门控 → 终审 → (Issue, candidate_id, traces)。"""
    relations = relations_by_candidate.get(dossier.candidate.id, [])
    gate = gate_candidate(relations)
    if gate is not None:
        reason_code, reason = gate
        verdict = Verdict(dossier.candidate.id, "drop", reason_code, reason)
        return verdict, None, "", [
            ("judge_verdict", _stable_json({
                "candidate_id": verdict.candidate_id, "action": "drop",
                "reason_code": reason_code,
            }))
        ]
    assessment = synthesize_verdict(
        dossier, relations, judge_llm=judge_llm,
        structured_method=structured_method, max_retries=max_retries,
    )
    candidate = dossier.candidate
    if assessment is None:
        severity = candidate.severity_proposal
        verdict = Verdict(
            candidate.id, "keep", "severity_evidence_incomplete",
            "verdict LLM unavailable; kept with proposed severity",
            resolved_severity=severity,
        )
        return verdict, candidate.to_issue().model_copy(
            update={"severity": severity}
        ), candidate.id, [
            ("judge_verdict", _stable_json({
                "candidate_id": candidate.id, "action": "keep",
                "reason_code": "severity_evidence_incomplete",
                "resolved_severity": severity.value,
            }))
        ]
    if assessment.action == "drop":
        verdict = Verdict(candidate.id, "drop", "synthesized_evidence_drop", assessment.reason)
        return verdict, None, "", [
            ("judge_verdict", _stable_json({
                "candidate_id": candidate.id, "action": "drop",
                "reason_code": "synthesized_evidence_drop",
            }))
        ]
    verdict = Verdict(
        candidate.id, "keep", "severity_resolved", assessment.reason,
        resolved_severity=assessment.severity,
    )
    issue = candidate.to_issue().model_copy(update={"severity": assessment.severity})
    return verdict, issue, candidate.id, [
        ("judge_verdict", _stable_json({
            "candidate_id": candidate.id, "action": "keep",
            "reason_code": "severity_resolved",
            "resolved_severity": assessment.severity.value,
        })),
        ("severity_resolved", _stable_json({
            "candidate_id": candidate.id,
            "severity": assessment.severity.value,
            "cited_fact_ids": list(assessment.cited_fact_ids),
        })),
    ]


def _invoke_direct(
    dossier: CandidateDossier,
    *,
    judge_llm: Any,
    structured_method: str,
    max_retries: int,
) -> tuple[Verdict, Issue | None, str, list[tuple[str, str]]]:
    """单个候选的消融档裁决:无门控、终审输入无关系。"""
    assessment = synthesize_verdict(
        dossier, [], judge_llm=judge_llm,
        structured_method=structured_method, max_retries=max_retries,
    )
    candidate = dossier.candidate
    if assessment is None:
        severity = candidate.severity_proposal
        verdict = Verdict(
            candidate.id, "keep", "direct_assessment_missing",
            "DirectJudge LLM assessment unavailable; kept with proposed severity",
            resolved_severity=severity,
        )
        return verdict, candidate.to_issue().model_copy(
            update={"severity": severity}
        ), candidate.id, [
            ("direct_judge_verdict", _stable_json({
                "candidate_id": candidate.id, "action": "keep",
                "reason_code": "direct_assessment_missing",
                "resolved_severity": severity.value,
            }))
        ]
    if assessment.action == "drop":
        verdict = Verdict(candidate.id, "drop", "direct_judge_drop", assessment.reason)
        return verdict, None, "", [
            ("direct_judge_verdict", _stable_json({
                "candidate_id": candidate.id, "action": "drop",
                "reason_code": "direct_judge_drop",
            }))
        ]
    verdict = Verdict(
        candidate.id, "keep", "direct_judge_keep", assessment.reason,
        resolved_severity=assessment.severity,
    )
    issue = candidate.to_issue().model_copy(update={"severity": assessment.severity})
    return verdict, issue, candidate.id, [
        ("direct_judge_verdict", _stable_json({
            "candidate_id": candidate.id, "action": "keep",
            "reason_code": "direct_judge_keep",
            "resolved_severity": assessment.severity.value,
        }))
    ]


def judge_with_evidence(
    assembly: DossierAssembly,
    relations_by_candidate: dict[str, list[FactRelation]],
    *,
    judge_llm: Any,
    structured_method: str,
    max_retries: int,
    candidate_groups: Sequence[CandidateGroup] = (),
) -> VerdictBatch:
    """完整档裁决:门控 → 终审 → 组内合并。

    两入口刻意同构——ADR-046 §5.6 要求消融档与完整档唯一差异是输入里有没有证据,勿合并重构。
    """
    batch = VerdictBatch()
    for failure in assembly.failures:
        verdict = Verdict(failure.candidate.id, "drop", "invalid_candidate_binding", failure.reason)
        batch.verdicts.append(verdict)
        _trace(batch, "judge_verdict", {
            "candidate_id": verdict.candidate_id, "action": "drop",
            "reason_code": verdict.reason_code,
        })
    if assembly.dossiers:
        results = run_bounded_parallel(
            assembly.dossiers,
            lambda dossier: _invoke_with_evidence(
                dossier, relations_by_candidate, judge_llm=judge_llm,
                structured_method=structured_method, max_retries=max_retries,
            ),
            max_workers=6,
        )
        supported: list[tuple[str, Issue]] = []
        for result in results:
            if result is None:
                continue
            verdict, issue, candidate_id, traces = result
            batch.verdicts.append(verdict)
            if issue is not None and candidate_id:
                supported.append((candidate_id, issue))
            batch.trace.extend(traces)
        consolidate_groups(batch, supported, candidate_groups)
    return batch


def judge_direct(
    assembly: DossierAssembly,
    *,
    judge_llm: Any,
    structured_method: str,
    max_retries: int,
    candidate_groups: Sequence[CandidateGroup] = (),
) -> VerdictBatch:
    """无证据链消融档:与 judge_with_evidence 同构,唯一差异是无门控、终审输入无关系。

    两入口刻意同构——ADR-046 §5.6 要求消融档与完整档唯一差异是输入里有没有证据,勿合并重构。
    """
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
        for result in results:
            if result is None:
                continue
            verdict, issue, candidate_id, traces = result
            batch.verdicts.append(verdict)
            if issue is not None and candidate_id:
                supported.append((candidate_id, issue))
            batch.trace.extend(traces)
        consolidate_groups(batch, supported, candidate_groups)
    return batch
