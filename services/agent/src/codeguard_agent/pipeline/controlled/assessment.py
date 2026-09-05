"""受控取证后的证据评估与候选绑定。"""

from __future__ import annotations

import json
import re
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
from codeguard_agent.pipeline.controlled.llm_contracts import LlmEvidenceAssessmentBatch
from codeguard_agent.pipeline.controlled.proof import match_graph_proof
from codeguard_agent.pipeline.evidence.ledger import bind_discovered_issue
from codeguard_agent.pipeline.evidence.graph_response import summarize_graph
from codeguard_agent.pipeline.evidence.projection import GRAPH_TOOLS

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
    included_evidence_count = 0
    # A graph fact is the only evidence that can close a cross-symbol claim.
    # Put graph steps first so a large source excerpt cannot consume the pack
    # before the reachability proof is shown to the assessment model.
    ordered_steps = tuple(sorted(steps, key=_evidence_step_priority))
    for step_index, item in enumerate(ordered_steps):
        if item.alias:
            prefix = (
                f'<evidence alias="{item.alias}" tool="{item.step.tool}" '
                f'subject_ref="{item.step.subject_ref}" status="{item.status}">\n'
            )
            suffix = "\n</evidence>"
            payload = item.projected_payload or item.raw_payload or item.error
            block = f"{prefix}{payload}{suffix}"
            available = max_chars - used - 1
            if len(block) > available and item.step.tool in GRAPH_TOOLS:
                # Reviewer/Judge projections deliberately allow a hard 16K
                # safety unit.  The assessment pack is smaller; reproject the
                # already-visible payload to this remaining budget while
                # preserving complete paths.  Never slice serialized JSON.
                # Reserve room for a local source step that follows this
                # graph fact. Without this reserve a large graph projection
                # consumes the entire pack and the direct mechanism evidence
                # is never considered.
                source_reserve = _source_reserve(
                    ordered_steps[step_index + 1 :],
                    available=available,
                )
                compact_budget = max(
                    512,
                    available - source_reserve - len(prefix) - len(suffix),
                )
                compact = summarize_graph(
                    payload,
                    tool=item.step.tool,
                    arguments=_step_arguments(item),
                    max_chars=compact_budget,
                    hard_max_chars=compact_budget,
                )
                compact_block = f"{prefix}{compact}{suffix}"
                if len(compact_block) <= available:
                    block = compact_block
        else:
            block = (
                f'<step tool="{item.step.tool}" status="{item.status}" '
                f'subject_ref="{item.step.subject_ref}">{item.error}</step>'
            )
        if used + len(block) + 1 > max_chars:
            # Keep the historical marker only when no evidence block made it
            # into the bounded pack.  If a graph fact was already included,
            # the remaining source step is merely omitted; calling the whole
            # pack "truncated" makes the assessment model treat the usable
            # graph artifact as unavailable and can drop a valid candidate.
            marker = (
                "evidence_pack_truncated_at_step_boundary"
                if included_evidence_count == 0
                else "evidence_step_omitted"
            )
            blocks.append(f"<limitation>{marker}</limitation>")
            break
        blocks.append(block)
        used += len(block) + 1
        if item.alias and item.status in {"complete", "reused"}:
            included_evidence_count += 1
    blocks.append("</work_item>")
    return "\n".join(blocks)


def _evidence_step_priority(item: StepExecution) -> tuple[int, int, str]:
    """Order graph facts first, then the smallest local source excerpt."""

    if item.step.tool in GRAPH_TOOLS:
        return (0, 0, item.step.subject_ref)
    if item.step.tool == "get_file_content":
        payload = item.projected_payload or item.raw_payload or item.error
        return (1, len(payload), item.step.subject_ref)
    return (2, 0, item.step.subject_ref)


def _source_reserve(
    steps: tuple[StepExecution, ...],
    *,
    available: int,
) -> int:
    """Estimate a bounded slot for the highest-priority source step."""

    for item in steps:
        if (
            item.step.tool == "get_file_content"
            and item.alias
            and item.status in {"complete", "reused"}
        ):
            payload = item.projected_payload or item.raw_payload or item.error
            prefix = (
                f'<evidence alias="{item.alias}" tool="{item.step.tool}" '
                f'subject_ref="{item.step.subject_ref}" status="{item.status}">\n'
            )
            estimate = len(prefix) + len(payload) + len("\n</evidence>") + 1
            # Reserve the complete source block whenever it can fit.  A
            # fixed fraction is unsafe for a 4--8K method excerpt: the graph
            # summary would consume the remaining budget and silently omit
            # the local mechanism that the assessment/Judge needs.
            return min(max(0, available), estimate)
    return 0


def _step_arguments(item: StepExecution) -> dict[str, Any]:
    """Rebuild only the graph arguments needed for a compact re-projection."""

    arguments: dict[str, Any] = {"symbol_id": item.step.subject_ref}
    if item.step.path_kind is not None:
        arguments["path_kind"] = item.step.path_kind
    if item.step.max_depth is not None:
        arguments["max_depth"] = item.step.max_depth
    return arguments


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


def visible_source_symbol_ids(execution: ExecutionBatch) -> set[str]:
    """Return only graph symbols whose response declares a source file.

    Graph relationships may legitimately point at library or unresolved
    symbols.  Those IDs are valid reachability facts but are not valid
    ``get_file_content`` subjects for a project snapshot.
    """

    ids: set[str] = set()
    for step in execution.steps:
        if step.step.tool not in {
            "inspect_path",
            "inspect_structure",
            "inspect_change_impact",
        }:
            continue
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
            if (
                isinstance(symbol, dict)
                and str(symbol.get("id", "")).strip()
                and str(symbol.get("file", "")).strip()
            ):
                ids.add(str(symbol["id"]))
    return ids


def collapse_candidate_duplicates(
    candidates: list[CandidateIssue],
) -> tuple[list[CandidateIssue], int]:
    """Collapse cross-reviewer reports of the same local mechanism.

    The three fixed reviewers intentionally run independently, so their raw
    outputs can describe one changed mechanism more than once. This reducer is
    conservative: candidates must belong to the same task/file, be within a
    three-line location window, and have materially similar claim/type text.
    It never merges unrelated same-line findings and leaves the semantic
    keep/drop decision to CouncilJudge.
    """

    result: list[CandidateIssue] = []
    collapsed = 0
    for candidate in candidates:
        match_index = next(
            (
                index
                for index, existing in enumerate(result)
                if _same_controlled_mechanism(existing, candidate)
            ),
            None,
        )
        if match_index is None:
            result.append(candidate)
            continue
        existing = result[match_index]
        winner, loser = _prefer_candidate(existing, candidate)
        refs = list(winner.evidence_refs)
        ref_ids = {ref.artifact_id for ref in refs}
        for ref in loser.evidence_refs:
            if ref.artifact_id not in ref_ids:
                refs.append(ref)
                ref_ids.add(ref.artifact_id)
        result[match_index] = winner.model_copy(
            update={
                "evidence_refs": refs,
                "confidence": max(existing.confidence, candidate.confidence),
                "evidence_observation": "；".join(
                    dict.fromkeys(
                        item
                        for item in (
                            existing.evidence_observation,
                            candidate.evidence_observation,
                        )
                        if item
                    )
                ),
            }
        )
        collapsed += 1
    return result, collapsed


def _candidate_tokens(candidate: CandidateIssue) -> set[str]:
    text = f"{candidate.claim} {candidate.type}".lower()
    return {
        token
        for token in re.findall(r"[a-z0-9_#]+|[\u4e00-\u9fff]{2,}", text)
        if token not in {
            "candidate",
            "issue",
            "problem",
            "可能",
            "需要",
            "存在",
            "变化",
            "行为",
            "逻辑",
        }
    }


def _same_controlled_mechanism(
    left: CandidateIssue,
    right: CandidateIssue,
) -> bool:
    if left.task_id != right.task_id:
        return False
    if left.file.replace("\\", "/").lower() != right.file.replace("\\", "/").lower():
        return False
    if left.line and right.line and abs(left.line - right.line) > 3:
        return False
    left_tokens = _candidate_tokens(left)
    right_tokens = _candidate_tokens(right)
    shared = left_tokens & right_tokens
    if len(shared) < 2:
        return False
    union = left_tokens | right_tokens
    similarity = len(shared) / len(union) if union else 0.0
    if left.type == right.type or similarity >= 0.35:
        return True
    # Different reviewers may choose different type labels for one exact
    # changed expression.  When they report the same line and their smaller
    # claim shares a substantial set of code/technical tokens, treat it as a
    # duplicate while retaining the higher-priority source agent.  The
    # location and lexical overlap guards keep unrelated same-file findings
    # separate; semantic keep/drop remains Judge's responsibility.
    overlap_coefficient = len(shared) / min(len(left_tokens), len(right_tokens))
    return (
        left.line == right.line
        and len(shared) >= 3
        and overlap_coefficient >= 0.30
    )


def _prefer_candidate(
    left: CandidateIssue,
    right: CandidateIssue,
) -> tuple[CandidateIssue, CandidateIssue]:
    rank = {"behavior": 0, "threat_model": 1, "maintainability": 2}
    if rank.get(right.source_agent, 9) < rank.get(left.source_agent, 9):
        return right, left
    return left, right


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
    def _build_user(selected_items: tuple[WorkItem, ...], *, repair: bool = False) -> str:
        blocks = []
        for item in selected_items:
            seed = seeds.get(item.seed_id)
            proof = proof_matches.get(item.work_item_id)
            if seed is None or proof is None:
                continue
            blocks.append(
                f"<candidate_seed>{seed.model_dump_json(exclude_defaults=True)}</candidate_seed>\n"
                f"<proof_match>{proof.model_dump_json()}</proof_match>\n"
                f"{build_evidence_pack(work_item=item, steps=steps_by_work[item.work_item_id], max_chars=pack_max_chars)}"
            )
        repair_instruction = (
            "这是一次协议补齐请求。只返回下列缺失 WorkItem 的评估，不要重复或改写其它 WorkItem；"
            "即使证据不足也必须为每个 id 返回一个 EvidenceAssessment。\n"
            if repair
            else ""
        )
        return (
            f'<assessment reviewer="{reviewer.value}" task_id="{task_id}">\n'
            f"{repair_instruction}{chr(10).join(blocks)}\n"
            "只评估上述 WorkItem，直接返回 EvidenceAssessmentBatch。"
        )

    user = _build_user(work_items)
    diagnostics: list[str] = []
    try:
        raw = invoke_with_retry(
            llm.with_structured_output(LlmEvidenceAssessmentBatch, method=structured_method),
            [("system", _assessment_system()), ("human", user)],
            max_retries=max_retries,
        )
        parsed = (
            EvidenceAssessmentBatch.model_validate(
                LlmEvidenceAssessmentBatch.model_validate(
                    raw.model_dump() if hasattr(raw, "model_dump") else raw
                ).model_dump()
            )
            if raw is not None
            else None
        )
    except Exception as exc:  # noqa: BLE001
        parsed = None
        diagnostics.append(f"assessment_error:{type(exc).__name__}")
    if parsed is None:
        # Some OpenAI-compatible endpoints return an AIMessage whose
        # ``invalid_tool_calls`` contains the requested function name but no
        # valid structured object (DeepSeek commonly reports finish_reason
        # ``tool_calls`` with an empty content string).  This is a transport
        # failure, not an evidence decision.  Retry once through the plain
        # JSON protocol so valid assessments are not discarded before the
        # deterministic proof/ledger stages see them.  The same typed
        # envelope is still required below; no semantic fields are inferred.
        fallback, fallback_diagnostic = _invoke_text_fallback(
            llm=llm,
            user_prompt=user,
            max_retries=max(1, max_retries),
        )
        if fallback is not None:
            parsed = fallback
            diagnostics.append("assessment_text_fallback_used")
        elif fallback_diagnostic:
            diagnostics.append(fallback_diagnostic)
    if parsed is None:
        return {}, tuple((*diagnostics, "assessment_failed"))

    valid_ids = {item.work_item_id for item in work_items}
    # Providers can legally return a structurally valid batch that silently
    # omits one item.  A missing assessment is not a semantic rejection: it is
    # a bounded protocol failure.  Retry only the missing subset once, then
    # merge it with the first response so a repair cannot erase valid work.
    returned_ids = {item.work_item_id for item in parsed.assessments}
    missing_ids = tuple(
        item.work_item_id
        for item in work_items
        if item.work_item_id not in returned_ids
    )
    parsed_batches = [parsed]
    if missing_ids:
        diagnostics.append(
            "assessment_contract_retry:" + ",".join(missing_ids)
        )
        missing_items = tuple(
            item for item in work_items if item.work_item_id in set(missing_ids)
        )
        try:
            retry_raw = invoke_with_retry(
                llm.with_structured_output(
                    LlmEvidenceAssessmentBatch,
                    method=structured_method,
                ),
                [("system", _assessment_system()), ("human", _build_user(missing_items, repair=True))],
                max_retries=max_retries,
            )
            retry_parsed = (
                EvidenceAssessmentBatch.model_validate(
                    LlmEvidenceAssessmentBatch.model_validate(
                        retry_raw.model_dump() if hasattr(retry_raw, "model_dump") else retry_raw
                    ).model_dump()
                )
                if retry_raw is not None
                else None
            )
            if retry_parsed is not None:
                parsed_batches.append(retry_parsed)
            else:
                diagnostics.append("assessment_contract_retry_failed")
        except Exception as exc:  # noqa: BLE001
            diagnostics.append(f"assessment_contract_retry_error:{type(exc).__name__}")

    result: dict[str, EvidenceAssessment] = {}

    def _normalise_assessment(assessment: EvidenceAssessment) -> EvidenceAssessment | None:
        if assessment.work_item_id not in valid_ids:
            diagnostics.append(f"assessment_unknown_work_item:{assessment.work_item_id}")
            return None
        if assessment.work_item_id in result:
            diagnostics.append(f"assessment_duplicate_work_item:{assessment.work_item_id}")
            return None
        proof = proof_matches[assessment.work_item_id]
        work_steps = steps_by_work[assessment.work_item_id]
        refs = tuple(
            alias for alias in assessment.supporting_refs
            if alias in {step.alias for step in work_steps if step.alias}
        )
        if len(refs) != len(assessment.supporting_refs):
            diagnostics.append(f"assessment_unknown_ref:{assessment.work_item_id}")
        if proof.status in {ProofMatchStatus.PROVED, ProofMatchStatus.PARTIAL}:
            # A graph proof must be cited with a graph artifact. The first
            # executed step is often get_file_content (mechanism context),
            # which cannot by itself support a cross-symbol reachability claim.
            graph_aliases = tuple(
                step.alias
                for step in work_steps
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
            if graph_aliases:
                # Keep one graph artifact, the subject source, and (when a
                # Delta ran) one newly selected endpoint source. This gives
                # Judge both sides of the seam without trusting provider ref
                # ordering or any domain-specific symbol name.
                source_items = tuple(
                    item
                    for item in work_steps
                    if (
                        item.alias
                        and item.status in {"complete", "reused"}
                        and item.step.tool == "get_file_content"
                    )
                )
                source_aliases = tuple(item.alias for item in source_items)
                if source_aliases:
                    work_seed = seeds.get(
                        next(
                            (
                                item.seed_id
                                for item in work_items
                                if item.work_item_id == assessment.work_item_id
                            ),
                            "",
                        )
                    )
                    subject_ref = (
                        work_seed.graph_question.subject_ref
                        if work_seed is not None and work_seed.graph_question
                        else ""
                    )
                    subject_sources = tuple(
                        item.alias
                        for item in source_items
                        if item.step.subject_ref == subject_ref
                    )
                    preferred_sources = list(subject_sources[:1])
                    for alias in reversed(source_aliases):
                        if alias not in preferred_sources:
                            preferred_sources.append(alias)
                        if len(preferred_sources) >= 2:
                            break
                    refs_without_sources = tuple(
                        alias for alias in refs if alias not in set(source_aliases)
                    )
                    refs = tuple(
                        dict.fromkeys(
                            (*graph_aliases[:1], *preferred_sources, *refs_without_sources)
                        )
                    )[:3]
                    for alias in preferred_sources:
                        diagnostics.append(
                            f"assessment_source_bound:{assessment.work_item_id}:{alias}"
                        )
        if not refs and proof.status in {ProofMatchStatus.PROVED, ProofMatchStatus.PARTIAL}:
            fallback_alias = next(
                (
                    step.alias
                    for step in work_steps
                    if step.alias and step.status in {"complete", "reused"}
                ),
                "",
            )
            if fallback_alias:
                refs = (fallback_alias,)
                diagnostics.append(f"assessment_ref_bound:{assessment.work_item_id}:{fallback_alias}")
        valid_aliases = {step.alias for step in work_steps if step.alias}
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
        return assessment.model_copy(
            update={"supporting_refs": refs, "counter_refs": counter_refs}
        )

    for batch in parsed_batches:
        for assessment in batch.assessments:
            normalized = _normalise_assessment(assessment)
            if normalized is not None:
                result[normalized.work_item_id] = normalized
    for item in work_items:
        if item.work_item_id not in result:
            diagnostics.append(f"assessment_missing_work_item:{item.work_item_id}")
    return result, tuple(diagnostics)


def _invoke_text_fallback(
    *,
    llm: Any,
    user_prompt: str,
    max_retries: int,
) -> tuple[EvidenceAssessmentBatch | None, str]:
    """Read one strict JSON assessment after a structured transport failure.

    This fallback is deliberately protocol-only.  It never creates missing
    WorkItems, fills claims, or chooses evidence; the normal validator and
    reference binding below remain the sole owners of those decisions.
    """

    system = (
        f"{_assessment_system()}\n\n"
        "结构化函数调用不可用。只输出一个 JSON 对象，字段必须与 "
        "EvidenceAssessmentBatch 相同；不要输出 Markdown、解释文字、工具调用或代码围栏。"
    )
    try:
        message = invoke_with_retry(
            llm,
            [("system", system), ("human", user_prompt)],
            max_retries=max(1, max_retries),
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"assessment_text_fallback_error:{type(exc).__name__}"
    payload = _message_json_object(message)
    if payload is None:
        return None, "assessment_text_fallback_not_json"
    try:
        return (
            EvidenceAssessmentBatch.model_validate(
                LlmEvidenceAssessmentBatch.model_validate(payload).model_dump()
            ),
            "",
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"assessment_text_fallback_invalid:{type(exc).__name__}"


def _message_json_object(message: Any) -> dict[str, Any] | None:
    """Extract a JSON object from content or a provider invalid-tool payload."""

    if isinstance(message, dict):
        if isinstance(message.get("assessments"), (list, tuple)):
            return message
        content = message.get("content", "")
        invalid_calls = message.get("invalid_tool_calls", ())
    else:
        content = getattr(message, "content", "")
        invalid_calls = getattr(message, "invalid_tool_calls", ())
    if isinstance(content, str) and content.strip():
        parsed = _extract_json_object(content)
        if parsed is not None:
            return parsed
    if isinstance(invalid_calls, (list, tuple)):
        for call in invalid_calls:
            args = call.get("args") if isinstance(call, dict) else getattr(call, "args", None)
            if isinstance(args, dict):
                return args
            if isinstance(args, str):
                parsed = _extract_json_object(args)
                if parsed is not None:
                    return parsed
    return None


def _extract_json_object(content: str) -> dict[str, Any] | None:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


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

    # DirectTriage owns the candidate hypothesis.  EvidenceAssessment owns
    # proof status and references; its duplicated ``claim`` field is only an
    # LLM-side explanation and must not silently replace the original
    # hypothesis with a shorter or newly invented assertion before Judge sees
    # it.  Keeping one canonical claim also makes Judge's "do not rewrite the
    # candidate" contract enforceable and prevents evidence summarization from
    # erasing the concrete diff mechanism.
    claim = seed.claim
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
    impact = seed.impact or (assessment.impact if assessment else "")
    observations: list[str] = []
    observable_note = _observable_return_consequence(
        seed=seed,
        assessment=assessment,
        catalog=catalog,
        artifact_ids=tuple(ref.artifact_id for ref in candidate.evidence_refs),
    )
    if observable_note and observable_note not in impact:
        impact = f"{impact.rstrip('。')}；{observable_note}" if impact else observable_note
        observations.append(observable_note)
    timing_note = _observable_timing_consequence(
        seed=seed,
        assessment=assessment,
        catalog=catalog,
        artifact_ids=tuple(ref.artifact_id for ref in candidate.evidence_refs),
    )
    if timing_note:
        if timing_note not in impact:
            impact = f"{impact.rstrip('。')}；{timing_note}" if impact else timing_note
        observations.append(timing_note)
    return candidate.model_copy(
        update={
            "id": seed.seed_id,
            # DirectTriage remains the owner of the canonical claim and
            # suggestion.  For the internal Judge context, however, a
            # provider may omit explanatory fields from either the seed or
            # the later assessment.  Preserve the non-empty value from
            # whichever typed stage supplied it; this is transport/context
            # repair, not a new fact and does not change the product Issue.
            "mechanism": seed.mechanism or (assessment.mechanism if assessment else ""),
            "impact": impact,
            "impact_locale": seed.impact_locale or (assessment.impact_locale if assessment else ""),
            "claim_type": seed.claim_type or (assessment.claim_type if assessment else ""),
            "evidence_observation": "；".join(dict.fromkeys(observations)),
        }
    )


def _observable_timing_consequence(
    *,
    seed: CandidateSeed,
    assessment: EvidenceAssessment | None,
    catalog: EvidenceCatalog,
    artifact_ids: tuple[str, ...] = (),
) -> str:
    """Name an observed callback-before-state-registration ordering.

    State/timing candidates are often phrased only as a local branch change.
    When the cited artifacts already show both sides of the seam, the final
    Judge needs the concrete observer and ordering to distinguish that
    candidate from an unrelated exceptional path.  This helper is deliberately
    evidence-only: it parses the selected graph/source artifacts, never reads
    the repository, invents a symbol, or decides that the ordering is a bug.
    """

    candidate_text = " ".join(
        value
        for value in (
            seed.claim,
            seed.mechanism,
            seed.impact,
            assessment.claim if assessment else "",
            assessment.mechanism if assessment else "",
            assessment.impact if assessment else "",
        )
        if value
    ).lower()
    state_markers = (
        "register(",
        "registered",
        "registration",
        "注册",
        "同步上下文",
        "synchronization",
        "context",
        "上下文",
    )
    if not any(marker in candidate_text for marker in state_markers):
        return ""

    if artifact_ids:
        selected_artifact_ids = artifact_ids
    else:
        aliases = assessment.supporting_refs if assessment is not None else ()
        selected_artifact_ids = tuple(
            catalog.alias_to_artifact_id.get(alias, "") for alias in aliases
        )
    graph_payloads: list[dict[str, Any]] = []
    source_payloads: list[str] = []
    for artifact_id in selected_artifact_ids:
        artifact = catalog.artifacts.get(artifact_id)
        if artifact is None:
            continue
        if artifact.tool == "get_file_content":
            source_payloads.append(artifact.payload)
            continue
        if artifact.tool not in GRAPH_TOOLS:
            continue
        try:
            payload = json.loads(artifact.payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            graph_payloads.append(payload)
    if not graph_payloads or not source_payloads:
        return ""

    source = "\n".join(source_payloads)
    registration_match = re.search(
        r"(?:RetrySynchronizationManager\.)?register\s*\(",
        source,
        flags=re.IGNORECASE,
    )
    if registration_match is None:
        return ""
    before_registration = source[: registration_match.start()]

    observed_targets: list[str] = []
    for payload in graph_payloads:
        for relation in payload.get("relationships") or ():
            if not isinstance(relation, dict):
                continue
            if str(relation.get("kind", "")).upper() != "CALLS":
                continue
            target = str(relation.get("targetId", "")).strip()
            if not target:
                continue
            qualified = target.split(":", 1)[-1]
            declaring, separator, member = qualified.partition("#")
            class_name = declaring.rsplit(".", 1)[-1]
            member_name = member.split("(", 1)[0] if separator else ""
            simple_name = (
                f"{class_name}.{member_name}" if member_name else class_name
            )
            lowered = target.lower()
            if any(
                token in lowered
                for token in ("listener", "callback", "interceptor", "consumer")
            ):
                observed_targets.append(simple_name)
    observed_targets = list(dict.fromkeys(observed_targets))
    if not observed_targets:
        return ""
    matching_target = next(
        (
            target
            for target in observed_targets
            if any(
                re.search(rf"\b{re.escape(part)}\b", before_registration)
                for part in target.split(".")
                if part
            )
        ),
        "",
    )
    if not matching_target and not re.search(
        r"\b(?:interceptor|listener|callback|consumer)\w*\b",
        before_registration,
        flags=re.IGNORECASE,
    ):
        return ""
    names = "/".join(observed_targets[:2])
    return (
        f"已引用图谱/源码显示先调用下游 {names}，后执行 context 注册；"
        "该观察点发生在新同步上下文注册之前"
    )


def _observable_return_consequence(
    *,
    seed: CandidateSeed,
    assessment: EvidenceAssessment | None,
    catalog: EvidenceCatalog,
    artifact_ids: tuple[str, ...] = (),
) -> str:
    """Derive a bounded Judge hint from source facts already in the ledger.

    A return/factory candidate is often emitted with only ``"return
    responsibility changed"`` even though its selected source excerpt shows
    a cache, state attribute, counter, or recovery branch.  That wording is
    too weak for the Judge's evidence contract.  This helper does not decide
    whether the change is a bug and never changes the public claim; it merely
    names observable constructs literally present in the cited source so the
    Judge can evaluate the consequence.  No symbol or domain fact is guessed.
    """

    claim_text = " ".join(
        value
        for value in (
            seed.claim,
            seed.mechanism,
            seed.impact,
            assessment.claim if assessment else "",
            assessment.mechanism if assessment else "",
            assessment.impact if assessment else "",
        )
        if value
    ).lower()
    return_markers = (
        "return ",
        "return`",
        "返回",
        "factory",
        "create(",
        "open(",
        "再次调用",
    )
    if not any(marker in claim_text for marker in return_markers):
        return ""

    source_payloads: list[str] = []
    if artifact_ids:
        selected_artifact_ids = artifact_ids
    else:
        aliases = assessment.supporting_refs if assessment is not None else ()
        selected_artifact_ids = tuple(
            catalog.alias_to_artifact_id.get(alias, "") for alias in aliases
        )
    for artifact_id in selected_artifact_ids:
        artifact = catalog.artifacts.get(artifact_id)
        if artifact is not None and artifact.tool == "get_file_content":
            source_payloads.append(artifact.payload)
    if not source_payloads:
        return ""
    source = "\n".join(source_payloads)
    observations: list[str] = []
    patterns = (
        (r"\b(?:\w*cache\w*)\b|\bcontainsKey\s*\(", "缓存读写/命中"),
        (r"\b(?:get|set|remove|has)Attribute\s*\(", "状态属性访问"),
        (r"\b(?:retryCount|attemptCount|count)\b", "重试/尝试计数"),
        (r"\b(?:recover|recovery|exhausted|closed|recovered)\b", "异常/恢复状态"),
    )
    for pattern, label in patterns:
        if re.search(pattern, source, flags=re.IGNORECASE):
            observations.append(label)
    if not observations:
        return ""
    note = (
        "源码证据明确出现"
        + "、".join(dict.fromkeys(observations))
        + "；返回表达式变化可能改变这些可观察状态在返回结果上的传播或可见性"
    )
    # A common return/factory seam is more concrete than a generic object
    # identity concern: source shows a cached/local state value being read and
    # then a different internal opener being returned.  Name only the literal
    # ordering present in the cited excerpt; whether the caller depends on the
    # retained state remains a Judge decision.
    cache_read = re.search(
        r"(?:containsKey\s*\(|\b(?:\w*cache\w*)\s*\.\s*get\s*\()",
        source,
        flags=re.IGNORECASE,
    )
    cached_context = re.search(
        r"\bcontext\s*=\s*[^;]*(?:\.get\s*\(|cache)",
        source,
        flags=re.IGNORECASE,
    )
    internal_return = re.search(
        r"\breturn\s+[^;]*(?:open|create|factory|internal)[^;]*;",
        source,
        flags=re.IGNORECASE,
    )
    if cache_read is not None and cached_context is not None and internal_return is not None:
        note += (
            "；源码还显示先读取并清理缓存中的 context，末尾却返回重新调用内部 "
            "open/factory 的结果，缓存 context 未直接作为返回值传出；该路径可能丢失"
            "缓存中的重试状态"
        )
    return note


__all__ = [
    "build_evidence_pack",
    "candidate_from_seed",
    "collapse_candidate_duplicates",
    "match_execution_proof",
    "run_evidence_assessment",
    "visible_symbol_ids",
    "visible_source_symbol_ids",
]
