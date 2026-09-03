"""受控取证后的证据评估与候选绑定。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.evidence import EvidenceCatalog
from codeguard_agent.models.schemas import DiscoveredIssue, EvidenceRefSelection, EvidenceRole
from codeguard_agent.models.tasks import (
    CandidateSeed,
    EvidenceAssessment,
    EvidenceAssessmentBatch,
    ProofMatch,
    ProofMatchStatus,
    ReviewerKind,
    WorkItem,
)
from codeguard_agent.pipeline.controlled.executor import ExecutionBatch, StepExecution
from codeguard_agent.pipeline.controlled.proof import match_graph_proof
from codeguard_agent.pipeline.evidence.ledger import bind_discovered_issue

_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts" / "controlled"


def build_evidence_pack(
    *,
    work_item: WorkItem,
    steps: tuple[StepExecution, ...],
    max_chars: int = 12_000,
) -> str:
    """按完整步骤单元渲染候选级 EvidencePack，避免把全量 catalog 给 Judge。"""

    blocks: list[str] = [f'<work_item id="{work_item.work_item_id}">']
    used = len(blocks[0])
    for item in steps:
        if item.alias:
            block = (
                f'<evidence alias="{item.alias}" tool="{item.step.tool}" '
                f'subject_ref="{item.step.subject_ref}" status="{item.status}">\n'
                f"{item.projected_payload or item.raw_payload or item.error}\n"
                "</evidence>"
            )
        else:
            block = (
                f'<step tool="{item.step.tool}" status="{item.status}" '
                f'subject_ref="{item.step.subject_ref}">{item.error}</step>'
            )
        if used + len(block) + 1 > max_chars:
            blocks.append("<limitation>evidence_pack_truncated_at_step_boundary</limitation>")
            break
        blocks.append(block)
        used += len(block) + 1
    blocks.append("</work_item>")
    return "\n".join(blocks)


def match_execution_proof(
    *,
    work_item: WorkItem,
    seed: CandidateSeed,
    steps: tuple[StepExecution, ...],
    subject_symbol_id: str,
) -> ProofMatch:
    """为 WorkItem 选择其声明的图谱步骤并执行确定性证明匹配。"""

    question = seed.graph_question
    if question is None:
        return ProofMatch(work_item_id=work_item.work_item_id, status=ProofMatchStatus.INDETERMINATE, limitations=("graph_question_missing",))
    candidates = [
        item for item in steps
        if item.status in {"complete", "reused"}
        and item.raw_payload
        and item.step.tool in {"inspect_path", "inspect_structure", "inspect_change_impact"}
    ]
    if not candidates:
        return ProofMatch(work_item_id=work_item.work_item_id, status=ProofMatchStatus.INDETERMINATE, limitations=("no_graph_fact",))
    matches = [
        match_graph_proof(
            work_item_id=work_item.work_item_id,
            payload=item.raw_payload,
            question=question,
            subject_symbol_id=subject_symbol_id,
        )
        for item in candidates
    ]
    priority = {
        ProofMatchStatus.PROVED: 0,
        ProofMatchStatus.PARTIAL: 1,
        ProofMatchStatus.INDETERMINATE: 2,
        ProofMatchStatus.NOT_FOUND: 3,
    }
    return min(matches, key=lambda item: priority[item.status])


def visible_symbol_ids(execution: ExecutionBatch) -> set[str]:
    """从已返回的图谱事实中提取可被 DeltaPlan 精确引用的 symbol_id。"""

    ids: set[str] = set()
    for step in execution.steps:
        # Delta may only use symbols that were visible in the same projection
        # shown to the reviewer. Never unlock a raw-but-omitted Gateway symbol.
        # DeltaPlan is allowed to continue only from symbols the reviewer
        # actually saw.  The raw Gateway artifact is intentionally not a
        # visibility source: projection may omit low-priority branches and
        # exposing raw IDs here would silently bypass that boundary.
        payload_text = step.projected_payload
        if not payload_text:
            continue
        try:
            payload = json.loads(payload_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for symbol in payload.get("symbols") or ():
            if isinstance(symbol, dict) and str(symbol.get("id", "")).strip():
                ids.add(str(symbol["id"]))
        for relation in payload.get("relationships") or ():
            if isinstance(relation, dict):
                ids.update(
                    str(relation.get(key, ""))
                    for key in ("sourceId", "targetId")
                    if str(relation.get(key, "")).strip()
                )
    return ids


def _assessment_system() -> str:
    return (_PROMPT_DIR / "assessment.txt").read_text(encoding="utf-8")


def run_evidence_assessment(
    *,
    reviewer: ReviewerKind,
    task_id: str,
    work_items: tuple[WorkItem, ...],
    seeds: dict[str, CandidateSeed],
    execution: ExecutionBatch,
    proof_matches: dict[str, ProofMatch],
    llm: Any,
    max_retries: int,
    structured_method: str,
    pack_max_chars: int = 12_000,
) -> tuple[dict[str, EvidenceAssessment], tuple[str, ...]]:
    """批量评估一个 reviewer/task 的 WorkItem；失败不启动 ReAct/fallback。"""

    if not work_items:
        return {}, ()
    if llm is None:
        return {}, ("assessment_llm_unavailable",)
    steps_by_work: dict[str, tuple[StepExecution, ...]] = {
        item.work_item_id: tuple(step for step in execution.steps if step.work_item_id == item.work_item_id)
        for item in work_items
    }
    blocks = []
    for item in work_items:
        seed = seeds.get(item.seed_id)
        proof = proof_matches.get(item.work_item_id)
        if seed is None or proof is None:
            continue
        blocks.append(
            f"<candidate_seed>{seed.model_dump_json()}</candidate_seed>\n"
            f"<proof_match>{proof.model_dump_json()}</proof_match>\n"
            f"{build_evidence_pack(work_item=item, steps=steps_by_work[item.work_item_id], max_chars=pack_max_chars)}"
        )
    user = (
        f'<assessment reviewer="{reviewer.value}" task_id="{task_id}">\n'
        f"{chr(10).join(blocks)}\n"
        "只评估上述 WorkItem，直接返回 EvidenceAssessmentBatch。"
    )
    diagnostics: list[str] = []
    try:
        raw = invoke_with_retry(
            llm.with_structured_output(EvidenceAssessmentBatch, method=structured_method),
            [("system", _assessment_system()), ("human", user)],
            max_retries=max_retries,
        )
        parsed = EvidenceAssessmentBatch.model_validate(raw) if raw is not None else None
    except Exception as exc:  # noqa: BLE001
        parsed = None
        diagnostics.append(f"assessment_error:{type(exc).__name__}")
    if parsed is None:
        return {}, tuple((*diagnostics, "assessment_failed"))

    valid_ids = {item.work_item_id for item in work_items}
    result: dict[str, EvidenceAssessment] = {}
    for assessment in parsed.assessments:
        if assessment.work_item_id not in valid_ids:
            diagnostics.append(f"assessment_unknown_work_item:{assessment.work_item_id}")
            continue
        if assessment.work_item_id in result:
            diagnostics.append(f"assessment_duplicate_work_item:{assessment.work_item_id}")
            continue
        proof = proof_matches[assessment.work_item_id]
        refs = tuple(
            alias for alias in assessment.supporting_refs
            if alias in {step.alias for step in steps_by_work[assessment.work_item_id] if step.alias}
        )
        if len(refs) != len(assessment.supporting_refs):
            diagnostics.append(f"assessment_unknown_ref:{assessment.work_item_id}")
        if proof.status in {ProofMatchStatus.PROVED, ProofMatchStatus.PARTIAL}:
            # A graph proof must be cited with a graph artifact. The first
            # executed step is often get_file_content (mechanism context),
            # which cannot by itself support a cross-symbol reachability claim.
            graph_aliases = tuple(
                step.alias
                for step in steps_by_work[assessment.work_item_id]
                if (
                    step.alias
                    and step.status in {"complete", "reused"}
                    and step.step.tool
                    in {"inspect_path", "inspect_structure", "inspect_change_impact"}
                )
            )
            if graph_aliases and not any(alias in graph_aliases for alias in refs):
                refs = (graph_aliases[0], *refs)[:3]
                diagnostics.append(
                    f"assessment_ref_bound:{assessment.work_item_id}:{graph_aliases[0]}"
                )
        if not refs and proof.status in {ProofMatchStatus.PROVED, ProofMatchStatus.PARTIAL}:
            fallback_alias = next(
                (
                    step.alias
                    for step in steps_by_work[assessment.work_item_id]
                    if step.alias and step.status in {"complete", "reused"}
                ),
                "",
            )
            if fallback_alias:
                refs = (fallback_alias,)
                diagnostics.append(f"assessment_ref_bound:{assessment.work_item_id}:{fallback_alias}")
        valid_aliases = {
            step.alias
            for step in steps_by_work[assessment.work_item_id]
            if step.alias
        }
        counter_refs = tuple(
            alias for alias in assessment.counter_refs if alias in valid_aliases
        )
        if len(counter_refs) != len(assessment.counter_refs):
            diagnostics.append(f"assessment_unknown_counter_ref:{assessment.work_item_id}")
        overlap = set(refs) & set(counter_refs)
        if overlap:
            counter_refs = tuple(alias for alias in counter_refs if alias not in overlap)
            diagnostics.append(
                f"assessment_ref_overlap:{assessment.work_item_id}:{','.join(sorted(overlap))}"
            )
        result[assessment.work_item_id] = assessment.model_copy(
            update={"supporting_refs": refs, "counter_refs": counter_refs}
        )
    for item in work_items:
        if item.work_item_id not in result:
            diagnostics.append(f"assessment_missing_work_item:{item.work_item_id}")
    return result, tuple(diagnostics)


def candidate_from_seed(
    *,
    seed: CandidateSeed,
    task: Any,
    catalog: EvidenceCatalog,
    reviewer: str,
    candidate_index: int,
    assessment: EvidenceAssessment | None = None,
) -> CandidateIssue:
    """把 DirectTriage seed 或评估后的图谱候选绑定为现有 CandidateIssue。"""

    claim = assessment.claim if assessment is not None and assessment.claim else seed.claim
    suggestion = seed.suggestion
    selections = []
    for alias in (assessment.supporting_refs if assessment is not None else ()):
        artifact_id = catalog.alias_to_artifact_id.get(alias, "")
        artifact = catalog.artifacts.get(artifact_id)
        role = (
            EvidenceRole.MECHANISM
            if artifact is not None and artifact.tool == "get_file_content"
            else EvidenceRole.REACHABILITY
        )
        selections.append(EvidenceRefSelection(alias=alias, role=role))
    discovered = DiscoveredIssue(
        file=seed.location_file,
        line=seed.location_line,
        location_snippet="",
        type=seed.issue_type,
        message=claim,
        suggestion=suggestion,
        confidence=seed.confidence,
        evidence_refs=selections,
    )
    candidate = bind_discovered_issue(
        discovered,
        task=task,
        reviewer=reviewer,
        catalog=catalog,
        candidate_index=candidate_index,
    )
    return candidate.model_copy(update={"id": seed.seed_id})


__all__ = [
    "build_evidence_pack",
    "candidate_from_seed",
    "match_execution_proof",
    "run_evidence_assessment",
    "visible_symbol_ids",
]
