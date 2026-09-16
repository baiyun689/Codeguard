"""裁决模块:批量 EvidenceJudge + 组内合并(Evidence Ledger)。

Verifier 只证明证据真实可用,支持/反驳/去留/定级合并为一次批量
EvidenceJudge:每批 ≤8 候选、最多 4 批并行;输出经确定性合同校验,
违规重试/二分拆批,单候选最终失败 fail-closed(不输出 Issue,完整留痕)。
`evidence_mode=off` 消融档走 judge_direct:输入无证据 ID,输出同构。
"""

from __future__ import annotations
import json
import logging
import re
from dataclasses import dataclass, field
from collections import deque
from pathlib import Path
from typing import Any
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
from codeguard_agent.pipeline.execution.concurrency import run_bounded_parallel
from codeguard_agent.pipeline.evidence.projection import (
    GRAPH_TOOLS,
    ProjectionAudience,
    graph_projection_focus,
    project_tool_payload,
)
from codeguard_agent.pipeline.evidence.planner import CandidateDossier, DossierAssembly
from codeguard_agent.pipeline.evidence.presentation import enrich_candidate_for_issue
from codeguard_agent.pipeline.prompting import render_prompt_template

logger = logging.getLogger("codeguard")
_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"
_JUDGE_BATCH_SIZE = 8
_JUDGE_MAX_PARALLEL_BATCHES = 4
_FILE_PAYLOAD_MAX_CHARS = 2000


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


def _evidence_item_payload(
    dossier: CandidateDossier, verification: CandidateVerification
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """将候选的已验证证据投影为裁决模型输入。

    返回证据条目及批内 F 编号到证据标识的映射。patch 选取候选位置对应的变更块，
    位置不明确时附带限制；图谱生成摘要，源码内容限制为 2000 字符。
    """
    items: list[dict[str, Any]] = []
    mapping: list[tuple[str, str]] = []
    for evidence in verification.valid_evidence:
        limitations = list(evidence.limitations)
        if evidence.source_kind is EvidenceSourceKind.TASK_PATCH:
            if dossier.candidate.line <= 0:
                limitations.append("candidate_location_unresolved")
            elif dossier.candidate.line not in dossier.task.changed_lines:
                if dossier.candidate.line in {
                    anchor.anchor_line for anchor in dossier.task.deletion_anchors
                }:
                    limitations.append("candidate_deletion_anchor")
                else:
                    limitations.append("candidate_line_unknown")
            content = evidence.content
        elif evidence.tool in GRAPH_TOOLS:
            content = project_tool_payload(
                evidence.tool,
                evidence.content,
                ProjectionAudience.JUDGE,
                arguments=evidence.arguments,
                focus=graph_projection_focus(dossier.task, dossier.symbol_context),
            ).content
        else:
            content = evidence.content[:_FILE_PAYLOAD_MAX_CHARS]
            if len(evidence.content) > _FILE_PAYLOAD_MAX_CHARS:
                limitations.append("payload_truncated")
        fact_id = f"F{len(items) + 1:03d}"
        mapping.append((fact_id, evidence.artifact_id))
        item: dict[str, Any] = {
            "evidence_id": fact_id,
            "source_kind": evidence.source_kind.value,
            "declared_role": evidence.declared_role.value,
            "tool": evidence.tool,
            "arguments": evidence.arguments,
            "content": content,
            "limitations": limitations,
        }
        if evidence.tool in GRAPH_TOOLS:
            path_facts = _bounded_graph_path_facts(
                content, arguments=evidence.arguments
            )
            if path_facts:
                item["derived_path_facts"] = path_facts
        items.append(item)
    return (items, mapping)


def _bounded_graph_path_facts(
    content: str, *, arguments: dict[str, Any], max_depth: int = 3, max_paths: int = 8
) -> list[dict[str, Any]]:
    """从投影中提取完整且已解析的 CALLS 路径。

    只遍历裁决模型可见的关系，从查询主体出发并遵守深度限制。
    图谱覆盖不完整的限制保留在 limitations 中。
    """
    try:
        payload = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    subject = str(
        arguments.get("symbol_id") or payload.get("subject_symbol_id") or ""
    ).strip()
    if not subject:
        return []
    adjacency: dict[str, list[tuple[str, str]]] = {}
    for relation in payload.get("relationships") or ():
        if not isinstance(relation, dict):
            continue
        if str(relation.get("kind", "")).upper() != "CALLS":
            continue
        source = str(relation.get("sourceId", "")).strip()
        target = str(relation.get("targetId", "")).strip()
        if source and target:
            adjacency.setdefault(source, []).append((target, "CALLS"))
    for values in adjacency.values():
        values.sort()
    paths: list[dict[str, Any]] = []
    queue: deque[tuple[str, tuple[str, ...], tuple[str, ...]]] = deque(
        [(subject, (subject,), ())]
    )
    while queue and len(paths) < max_paths:
        current, nodes, kinds = queue.popleft()
        options = [
            (target, kind)
            for target, kind in adjacency.get(current, ())
            if target not in nodes
        ]
        if not options:
            if len(nodes) > 1:
                paths.append({"symbols": list(nodes), "relationships": list(kinds)})
            continue
        if len(kinds) >= max_depth:
            paths.append({"symbols": list(nodes), "relationships": list(kinds)})
            continue
        for target, kind in options:
            queue.append((target, (*nodes, target), (*kinds, kind)))
    return paths


def _judge_payload(
    dossiers: list[CandidateDossier], verifications: dict[str, CandidateVerification]
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
                "mechanism": dossier.candidate.mechanism,
                "impact": dossier.candidate.impact,
                "impact_locale": dossier.candidate.impact_locale,
                "claim_type": dossier.candidate.claim_type,
                "evidence_observation": dossier.candidate.evidence_observation,
                "confidence": dossier.candidate.confidence,
                "suggestion": dossier.candidate.suggestion,
                "source_agent": dossier.candidate.source_agent,
            },
            "grounding_status": verification.grounding_status,
            "evidence": items,
            "verified_evidence": items,
            "evidence_gaps": [
                {
                    "tool": gap.tool,
                    "arguments": gap.arguments,
                    "declared_role": gap.declared_role.value,
                    "reason": gap.reason,
                    "limitations": list(gap.limitations),
                }
                for gap in verification.evidence_gaps
            ],
        }
        candidates.append(candidate_entry)
        fact_map[dossier.candidate.id] = dict(mapping)
    return (candidates, fact_map)


def _role_of(
    artifact_id: str,
    dossier: CandidateDossier,
    verification: CandidateVerification | None = None,
) -> EvidenceRole:
    for ref in dossier.candidate.evidence_refs:
        if ref.artifact_id == artifact_id:
            return ref.declared_role
    if verification is not None:
        for evidence in verification.valid_evidence:
            if evidence.artifact_id == artifact_id:
                return evidence.declared_role
    return EvidenceRole.MECHANISM


def _validate_assessment(
    item: EvidenceJudgeAssessment,
    *,
    dossier: CandidateDossier,
    fact_map: dict[str, str],
    verification: CandidateVerification,
    violations: list[str],
) -> EvidenceJudgeAssessment | None:
    """单候选裁决合同校验;违约返回 None(该候选 fail-closed)。"""
    visible = set(fact_map.keys())
    evidence_ids = list(item.evidence_ids)
    unknown = [fid for fid in evidence_ids if fid not in visible]
    if unknown:
        violations.append(f"evidence_unknown_id:{','.join(unknown)}")
        return None
    if item.action == "keep":
        if not evidence_ids:
            violations.append("keep_without_evidence")
            return None
        if item.severity is None:
            violations.append("keep_without_severity")
            return None
        artifact_ids = [fact_map[fid] for fid in evidence_ids]
        if artifact_ids and all(
            (
                _role_of(artifact_id, dossier, verification) is EvidenceRole.LOCATION
                for artifact_id in artifact_ids
            )
        ):
            violations.append("evidence_all_location")
            return None
    elif item.severity is not None:
        violations.append("drop_with_severity")
        return None
    return item


def _recover_judge_arguments(raw: Any) -> EvidenceJudgeBatch | None:
    """仅恢复单个工具参数对象末尾的一个多余闭合符，不补字段、不改内容。"""
    calls = list(getattr(raw, "tool_calls", ()) or ()) + list(
        getattr(raw, "invalid_tool_calls", ()) or ()
    )
    if len(calls) != 1 or calls[0].get("name") != "EvidenceJudgeBatch":
        return None
    arguments = calls[0].get("args")
    if not isinstance(arguments, str):
        return None

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate_json_key")
            obj[key] = value
        return obj

    try:
        text = arguments.strip()
        value, end = json.JSONDecoder(object_pairs_hook=unique_object).raw_decode(text)
        if text[end:].strip() not in ("]", "}") or not isinstance(value, dict):
            return None
        if set(value) != {"assessments"} or not isinstance(value["assessments"], list):
            return None
        required = {"candidate_id", "action", "severity", "evidence_ids", "reason"}
        if not value["assessments"] or any(
            not isinstance(item, dict) or set(item) != required
            for item in value["assessments"]
        ):
            return None
        return EvidenceJudgeBatch.model_validate_json(json.dumps(value), strict=True)
    except (ValueError, TypeError):
        return None


def _invoke_batch(
    payload: list[dict[str, Any]],
    *,
    judge_llm: Any,
    structured_method: str,
    max_retries: int,
    prompt_file: str,
    batch: VerdictBatch | None = None,
) -> EvidenceJudgeBatch | None:
    """调用批量 Judge;None/异常重试一次,再失败返回 None(由调用方二分)。"""
    structured = judge_llm.with_structured_output(
        EvidenceJudgeBatch, method=structured_method, include_raw=True
    )
    system_prompt = (_PROMPT_DIR / prompt_file).read_text(encoding="utf-8")
    for attempt in range(2):
        try:
            result = invoke_with_retry(
                structured,
                [
                    ("system", system_prompt),
                    (
                        "user",
                        render_prompt_template(
                            (_PROMPT_DIR / "evidence-judge-user.txt").read_text(
                                encoding="utf-8"
                            ),
                            {"payload": _stable_json({"candidates": payload})},
                        ),
                    ),
                ],
                max_retries=max_retries,
            )
            if isinstance(result, dict) and "raw" in result and "parsed" in result:
                envelope = result
                result = envelope["parsed"]
                if result is None:
                    result = _recover_judge_arguments(envelope["raw"])
                    if result is not None:
                        logger.info("evidence judge recovered one trailing closing bracket")
                        if batch is not None:
                            _trace(batch, "evidence_judge_output_recovered", {
                                "attempt": attempt + 1,
                                "repair": "single_trailing_closing_bracket",
                            })
                    elif batch is not None:
                        _trace(batch, "evidence_judge_output_invalid", {
                            "attempt": attempt + 1,
                            "error_type": type(envelope.get("parsing_error")).__name__,
                        })
            if result is None:
                continue
            if not isinstance(result, EvidenceJudgeBatch):
                result = EvidenceJudgeBatch.model_validate(result)
            return result
        except Exception as exc:
            if batch is not None:
                _trace(batch, "evidence_judge_invoke_error", {
                    "attempt": attempt + 1, "error_type": type(exc).__name__,
                    "cause_type": type(exc.__cause__).__name__ if exc.__cause__ else "",
                    "status_code": getattr(exc, "status_code", None),
                })
            logger.warning(
                "evidence judge batch invoke failed (attempt %d): %s", attempt + 1, exc
            )
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
    contract_retry: bool = True,
) -> list[tuple[CandidateDossier, EvidenceJudgeAssessment | None, str]]:
    """批内裁决:整批 → 输出合同校验 → 失败二分;单候选失败 fail-closed。"""
    if judge_llm is None:
        return [
            (
                dossier,
                EvidenceJudgeAssessment(
                    candidate_id=dossier.candidate.id,
                    action="keep",
                    severity=Severity.WARNING,
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
        batch=batch,
    )
    _trace(
        batch,
        "evidence_judge_batch_started",
        {"candidate_ids": [dossier.candidate.id for dossier in chunk]},
    )
    if result is None:
        if len(chunk) == 1:
            _trace(
                batch,
                "evidence_judge_batch_failed",
                {"candidate_ids": [chunk[0].candidate.id]},
            )
            return [(chunk[0], None, "verification_failed")]
        mid = len(chunk) // 2
        return _judge_chunk(
            chunk[:mid],
            verifications=verifications,
            artifacts=artifacts,
            judge_llm=judge_llm,
            structured_method=structured_method,
            max_retries=max_retries,
            prompt_file=prompt_file,
            batch=batch,
        ) + _judge_chunk(
            chunk[mid:],
            verifications=verifications,
            artifacts=artifacts,
            judge_llm=judge_llm,
            structured_method=structured_method,
            max_retries=max_retries,
            prompt_file=prompt_file,
            batch=batch,
        )
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
    retry_ids: set[str] = set()
    for dossier in chunk:
        item = assessments.get(dossier.candidate.id)
        if item is None:
            violations.append(f"missing_assessment:{dossier.candidate.id}")
            outcomes.append((dossier, None, "verification_failed"))
            retry_ids.add(dossier.candidate.id)
            continue
        validated = _validate_assessment(
            item,
            dossier=dossier,
            fact_map=fact_map[dossier.candidate.id],
            verification=verifications[dossier.candidate.id],
            violations=violations,
        )
        outcomes.append(
            (dossier, validated, "contract_violation" if validated is None else "ok")
        )
        if validated is None:
            retry_ids.add(dossier.candidate.id)
    if violations:
        _trace(batch, "evidence_judge_contract_violations", {"violations": violations})
    if retry_ids and contract_retry:
        retry_chunk = [
            dossier for dossier in chunk if dossier.candidate.id in retry_ids
        ]
        retry_outcomes = _judge_chunk(
            retry_chunk,
            verifications=verifications,
            artifacts=artifacts,
            judge_llm=judge_llm,
            structured_method=structured_method,
            max_retries=max_retries,
            prompt_file=prompt_file,
            batch=batch,
            contract_retry=False,
        )
        retry_by_id = {
            dossier.candidate.id: (dossier, assessment, reason)
            for dossier, assessment, reason in retry_outcomes
        }
        outcomes = [
            retry_by_id.get(outcome[0].candidate.id, outcome) for outcome in outcomes
        ]
    recovered: list[tuple[CandidateDossier, EvidenceJudgeAssessment | None, str]] = []
    for dossier, assessment, reason in outcomes:
        if assessment is not None and assessment.action == "drop":
            replacement = _recover_return_state_assessment(
                dossier,
                verification=verifications[dossier.candidate.id],
                artifacts=artifacts,
                fact_map=fact_map[dossier.candidate.id],
            )
            if replacement is not None:
                assessment = replacement
                reason = "deterministic_evidence_keep"
                _trace(
                    batch,
                    "evidence_judge_deterministic_keep",
                    {
                        "candidate_id": dossier.candidate.id,
                        "reason": "verified_return_state_observation",
                        "evidence_ids": list(replacement.evidence_ids),
                    },
                )
        recovered.append((dossier, assessment, reason))
    outcomes = recovered
    return outcomes


def _recover_return_state_assessment(
    dossier: CandidateDossier,
    *,
    verification: CandidateVerification,
    artifacts: dict[str, EvidenceArtifact],
    fact_map: dict[str, str],
) -> EvidenceJudgeAssessment | None:
    """对具备完整证据链的返回值或状态候选进行补充裁决。

    候选须描述可观察的状态后果，且同一任务的账本同时包含 patch、源码和图谱事实。
    不符合条件的候选保留模型原有的丢弃结论。
    """
    candidate = dossier.candidate
    candidate_text = " ".join(
        (
            value
            for value in (
                candidate.claim,
                candidate.mechanism,
                candidate.impact,
                candidate.evidence_observation,
            )
            if value
        )
    ).lower()
    return_markers = (
        "return ",
        "return`",
        "返回",
        "factory",
        "open(",
        "create(",
        "再次调用",
    )
    consequence_markers = (
        "缓存",
        "cache",
        "状态",
        "state",
        "重试",
        "retry",
        "计数",
        "count",
        "属性",
        "attribute",
        "结果",
        "result",
        "契约",
        "contract",
        "丢失",
        "loss",
    )
    if not any((marker in candidate_text for marker in return_markers)):
        return None
    if not any((marker in candidate_text for marker in consequence_markers)):
        return None
    patch_payloads: list[str] = []
    source_payloads: list[str] = []
    graph_payloads: list[tuple[str, dict[str, Any]]] = []
    for evidence in verification.valid_evidence:
        artifact = artifacts.get(evidence.artifact_id)
        if artifact is None:
            continue
        if evidence.source_kind is EvidenceSourceKind.TASK_PATCH:
            patch_payloads.append(evidence.content)
        elif evidence.tool == "read_symbol":
            source_payloads.append(evidence.content)
        elif evidence.tool in GRAPH_TOOLS:
            try:
                payload = json.loads(evidence.content)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                graph_payloads.append((evidence.artifact_id, payload))
    if not patch_payloads or not source_payloads or (not graph_payloads):
        return None
    patch = "\n".join(patch_payloads)
    source = "\n".join(source_payloads)
    changed_return = re.search(
        "(?m)^\\s*-\\s*return\\s+[^;\\n]*\\bcontext\\b[^;\\n]*;\\s*$.*?^\\s*\\+\\s*return\\s+[^;\\n]*(?:open|create|factory|internal)[^;\\n]*;\\s*$",
        patch,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if changed_return is None:
        return None
    has_cache_or_state_read = re.search(
        "(?:containsKey\\s*\\(|\\b\\w*cache\\w*\\s*\\.\\s*(?:get|remove|containsKey)\\s*\\(|\\bcontext\\s*=\\s*[^;\\n]*(?:cache|get)\\b)",
        source,
        flags=re.IGNORECASE,
    )
    has_state_cleanup = re.search(
        "(?:removeAttribute\\s*\\(|\\b(?:clear|remove)\\s*\\([^)]*\\b(?:context|state|cache)\\b)",
        source,
        flags=re.IGNORECASE,
    )
    has_internal_return = re.search(
        "\\breturn\\s+[^;\\n]*(?:open|create|factory|internal)[^;\\n]*;",
        source,
        flags=re.IGNORECASE,
    )
    if (
        has_cache_or_state_read is None
        or has_state_cleanup is None
        or has_internal_return is None
    ):
        return None
    return_callee = re.search(
        "\\breturn\\s+[^;\\n]*(?:open|create|factory|internal)[^;\\n]*;",
        source,
        flags=re.IGNORECASE,
    )
    callee_tokens: set[str] = set()
    if return_callee is not None:
        callee_tokens.update(
            (
                token.lower()
                for token in re.findall(
                    "[A-Za-z_$][A-Za-z0-9_$]*", return_callee.group(0)
                )
                if token.lower()
                not in {"return", "open", "create", "factory", "internal"}
            )
        )
    graph_facts: list[str] = []
    for artifact_id, payload in graph_payloads:
        subject = ""
        for evidence in verification.valid_evidence:
            if evidence.artifact_id == artifact_id:
                subject = str(evidence.arguments.get("symbol_id", ""))
                break
        for relation in payload.get("relationships") or ():
            if (
                not isinstance(relation, dict)
                or str(relation.get("kind", "")).upper() != "CALLS"
            ):
                continue
            source_id = str(relation.get("sourceId", ""))
            target_id = str(relation.get("targetId", ""))
            if not source_id or not target_id:
                continue
            same_subject = bool(subject and source_id == subject)
            callee_match = bool(
                callee_tokens
                and any((token in target_id.lower() for token in callee_tokens))
            )
            if same_subject or callee_match:
                graph_facts.append(f"{source_id} -> {target_id}")
    if not graph_facts:
        return None
    evidence_ids: list[str] = []
    for fact_id, artifact_id in fact_map.items():
        if fact_id in evidence_ids:
            continue
        matched_evidence = next(
            (
                item
                for item in verification.valid_evidence
                if item.artifact_id == artifact_id
            ),
            None,
        )
        if matched_evidence is None:
            continue
        if (
            matched_evidence.source_kind is EvidenceSourceKind.TASK_PATCH
            or matched_evidence.tool in {"read_symbol", *GRAPH_TOOLS}
        ):
            evidence_ids.append(fact_id)
        if len(evidence_ids) >= 3:
            break
    if not evidence_ids:
        return None
    return EvidenceJudgeAssessment(
        candidate_id=candidate.id,
        action="keep",
        severity=Severity.WARNING,
        evidence_ids=evidence_ids,
        reason="已验证的 patch/source 直接显示局部状态或缓存对象被读取并清理，变更后的 return 调用另一个 opener/factory，且图谱确认该返回调用存在；该局部状态传播后果属于范围受限的 WARNING。",
    )


def _finalize_assessment(
    dossier: CandidateDossier,
    assessment: EvidenceJudgeAssessment | None,
    verdict_reason: str,
    batch: VerdictBatch,
    *,
    event: str,
    verification: CandidateVerification | None = None,
    artifacts: dict[str, EvidenceArtifact] | None = None,
) -> tuple[Verdict, Issue | None]:
    candidate = dossier.candidate
    if assessment is None:
        verdict = Verdict(
            candidate.id,
            "drop",
            "verification_failed",
            "Judge 失败或输出合同违约,按 fail-closed 不输出",
        )
        _trace(
            batch,
            event,
            {
                "candidate_id": candidate.id,
                "action": "drop",
                "reason_code": "verification_failed",
            },
        )
        return (verdict, None)
    if assessment.action == "drop":
        reason_code = (
            "insufficient_evidence" if not assessment.evidence_ids else "judge_drop"
        )
        verdict = Verdict(candidate.id, "drop", reason_code, assessment.reason)
        _trace(
            batch,
            event,
            {
                "candidate_id": candidate.id,
                "action": "drop",
                "reason_code": reason_code,
                "reason": assessment.reason,
                "evidence_ids": list(assessment.evidence_ids),
            },
        )
        return (verdict, None)
    verdict = Verdict(
        candidate.id,
        "keep",
        verdict_reason,
        assessment.reason,
        resolved_severity=assessment.severity,
        supported=bool(assessment.evidence_ids),
    )
    assert assessment.severity is not None
    public_candidate = enrich_candidate_for_issue(
        candidate,
        symbol_context=dossier.symbol_context,
        verification=verification,
        artifacts=artifacts or {},
    )
    issue = public_candidate.to_issue(assessment.severity)
    _trace(
        batch,
        event,
        {
            "candidate_id": candidate.id,
            "action": "keep",
            "reason_code": verdict_reason,
            "resolved_severity": assessment.severity.value
            if assessment.severity
            else None,
            "evidence_ids": list(assessment.evidence_ids),
        },
    )
    return (verdict, issue)


def judge_with_evidence(
    assembly: DossierAssembly,
    verifications: dict[str, CandidateVerification],
    artifacts: dict[str, EvidenceArtifact],
    *,
    judge_llm: Any,
    structured_method: str,
    max_retries: int,
) -> VerdictBatch:
    """完整档裁决:绑定失败/验证淘汰 → 批量 EvidenceJudge。"""
    batch = VerdictBatch()
    for failure in assembly.failures:
        verdict = Verdict(
            failure.candidate.id, "drop", "invalid_candidate_binding", failure.reason
        )
        batch.verdicts.append(verdict)
        _trace(
            batch,
            "judge_verdict",
            {
                "candidate_id": verdict.candidate_id,
                "action": "drop",
                "reason_code": verdict.reason_code,
            },
        )
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
            dossier.candidate.id,
            "drop",
            verification.rejection_reason or "ineligible",
            verification.rejection_reason or "",
        )
        batch.verdicts.append(verdict)
        _trace(
            batch,
            "judge_verdict",
            {
                "candidate_id": verdict.candidate_id,
                "action": "drop",
                "reason_code": verdict.reason_code,
            },
        )
    if not eligible:
        return batch
    chunks = [
        eligible[index : index + _JUDGE_BATCH_SIZE]
        for index in range(0, len(eligible), _JUDGE_BATCH_SIZE)
    ]
    outcomes = run_bounded_parallel(
        chunks,
        lambda chunk: _judge_chunk(
            chunk,
            verifications=verifications,
            artifacts=artifacts,
            judge_llm=judge_llm,
            structured_method=structured_method,
            max_retries=max_retries,
            prompt_file="evidence-judge.txt",
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
                dossier,
                assessment,
                verdict_reason,
                batch,
                event="judge_verdict",
                verification=verifications.get(dossier.candidate.id),
                artifacts=artifacts,
            )
            batch.verdicts.append(verdict)
            if issue is not None:
                supported.append((dossier.candidate.id, issue))
    for candidate_id, issue in supported:
        batch.final_candidate_ids.append(candidate_id)
        batch.final_issues.append(issue)
    return batch


def _direct_payload(dossier: CandidateDossier) -> dict[str, Any]:
    return {
        "candidate_alias": "C001",
        "candidate": {
            "type": dossier.candidate.type,
            "claim": dossier.candidate.claim,
            "file": dossier.candidate.file,
            "line": dossier.candidate.line,
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
            severity=Severity.WARNING,
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
                (
                    "user",
                    render_prompt_template(
                        (_PROMPT_DIR / "direct-judge-user.txt").read_text(
                            encoding="utf-8"
                        ),
                        {"payload": _stable_json(_direct_payload(dossier))},
                    ),
                ),
            ],
            max_retries=max_retries,
        )
        if result is None:
            return None
        if not isinstance(result, EvidenceJudgeAssessment):
            result = EvidenceJudgeAssessment.model_validate(result)
        if result.candidate_id != "C001":
            logger.warning(
                "direct judge returned unexpected candidate_id: %s", result.candidate_id
            )
            return None
        return result.model_copy(update={"candidate_id": dossier.candidate.id})
    except Exception:
        logger.warning("direct judge LLM synthesis failed", exc_info=True)
        return None


def judge_direct(
    assembly: DossierAssembly,
    *,
    judge_llm: Any,
    structured_method: str,
    max_retries: int,
) -> VerdictBatch:
    """无证据链消融档:输入无证据 ID、无门控,输出 keep/drop/severity 同构。"""
    batch = VerdictBatch()
    for failure in assembly.failures:
        verdict = Verdict(
            failure.candidate.id, "drop", "invalid_candidate_binding", failure.reason
        )
        batch.verdicts.append(verdict)
        _trace(
            batch,
            "direct_judge_verdict",
            {
                "candidate_id": verdict.candidate_id,
                "action": "drop",
                "reason_code": verdict.reason_code,
            },
        )
    if assembly.dossiers:
        results = run_bounded_parallel(
            assembly.dossiers,
            lambda dossier: _invoke_direct(
                dossier,
                judge_llm=judge_llm,
                structured_method=structured_method,
                max_retries=max_retries,
            ),
            max_workers=6,
        )
        supported: list[tuple[str, Issue]] = []
        for dossier, assessment in zip(assembly.dossiers, results, strict=True):
            if assessment is None:
                verdict = Verdict(
                    dossier.candidate.id,
                    "drop",
                    "verification_failed",
                    "DirectJudge LLM assessment unavailable; fail-closed",
                )
                batch.verdicts.append(verdict)
                _trace(
                    batch,
                    "direct_judge_verdict",
                    {
                        "candidate_id": dossier.candidate.id,
                        "action": "keep",
                        "reason_code": "verification_failed",
                    },
                )
                continue
            verdict, final_issue = _finalize_assessment(
                dossier,
                assessment,
                "direct_judge_keep",
                batch,
                event="direct_judge_verdict",
                verification=None,
                artifacts={},
            )
            batch.verdicts.append(verdict)
            if final_issue is not None:
                supported.append((dossier.candidate.id, final_issue))
        for candidate_id, issue in supported:
            batch.final_candidate_ids.append(candidate_id)
            batch.final_issues.append(issue)
    return batch
