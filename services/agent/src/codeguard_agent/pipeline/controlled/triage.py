"""受控 DirectTriage：三个固定领域 reviewer 的无工具初筛。"""

from __future__ import annotations

import logging
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.tasks import (
    CandidateSeed,
    CoverageDeclaration,
    CoverageDecision,
    DirectTriageResult,
    EvidenceNeed,
    InvestigationSeed,
    ProofScope,
    ReviewTask,
    ReviewerKind,
    TaskSymbolContext,
)
from codeguard_agent.pipeline.controlled.routing import (
    bind_seed_ids,
    route_seed,
    validate_coverage,
    validate_seed,
)
from codeguard_agent.pipeline.controlled.subtask_capabilities import (
    coherent_tool_bundle,
    normalize_investigation_seed,
)
from codeguard_agent.pipeline.controlled.llm_contracts import (
    LlmCandidateSeed,
    LlmCoverageDeclaration,
    LlmDirectTriageResult,
)

logger = logging.getLogger("codeguard")
_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts" / "controlled"

_DOMAIN_PROMPTS = {
    ReviewerKind.BEHAVIOR: "direct-triage-behavior.txt",
    ReviewerKind.THREAT_MODEL: "direct-triage-threat.txt",
    ReviewerKind.MAINTAINABILITY: "direct-triage-maintainability.txt",
}

# Relationship kinds emitted by Gateway schema v2.  DirectTriage is allowed to
# describe a relationship in natural language, but the deterministic proof
# matcher can only evaluate actual graph kinds.  Unknown labels are removed
# during normalization and replaced with the conservative CALLS baseline so a
# useful partial path still reaches EvidenceAssessment/Judge.
_GRAPH_RELATIONSHIP_KINDS = frozenset(
    {
        "CALLS",
        "READS",
        "WRITES",
        "READS_FIELD",
        "WRITES_FIELD",
        "DECLARES",
        "IMPLEMENTS",
        "EXTENDS",
        "USES_CONTEXT",
        "STATE_CONSUMER",
    }
)


def _prompt_for(reviewer: ReviewerKind) -> str:
    common = (_PROMPT_DIR / "direct-triage-common.txt").read_text(encoding="utf-8")
    domain = (_PROMPT_DIR / _DOMAIN_PROMPTS[reviewer]).read_text(encoding="utf-8")
    return f"{common.strip()}\n\n{domain.strip()}"


def prompt_hash(reviewer: ReviewerKind) -> str:
    """返回 DirectTriage system prompt 的内容哈希，供 Trace 审计。"""

    return hashlib.sha256(_prompt_for(reviewer).encode("utf-8")).hexdigest()[:16]


def build_triage_user_prompt(
    *,
    task: ReviewTask,
    symbol_context: TaskSymbolContext | None,
    diff_summary: str = "",
    task_knowledge: str = "",
) -> str:
    """渲染 DirectTriage 的有界输入；不包含工具目录或历史 few-shot。"""

    symbols = []
    if symbol_context is not None:
        symbols = [symbol.model_dump_json() for symbol in symbol_context.symbols]
    symbol_text = "\n".join(symbols) if symbols else "(没有解析到 symbol；只能依据 diff 做局部判断)"
    knowledge = task_knowledge.strip() or "(无专项知识；使用领域基础方法)"
    summary = diff_summary.strip() or "(无变更摘要；直接阅读 task patch)"
    parts = [
        f'<task id="{task.id}" file="{task.file}" changed_lines="{",".join(map(str, task.changed_lines))}">\n'
        f"<task_patch>\n{task.patch}\n</task_patch>\n"
        f"<diff_summary>{summary}</diff_summary>\n"
        f"<symbol_context>\n{symbol_text}\n</symbol_context>\n"
    ]
    if task.deletion_anchors:
        parts.extend([
            "<deletion_anchors>",
            "删除行只存在于 patch 旧侧；候选 location_line 必须使用这里给出的当前版本锚点。",
        ])
        for anchor in task.deletion_anchors:
            parts.extend([
                f'  <anchor line="{anchor.anchor_line}" kind="{anchor.anchor_kind}">',
                "    <deleted_fragment>",
                anchor.deleted_snippet,
                "    </deleted_fragment>",
                "  </anchor>",
            ])
        parts.append("</deletion_anchors>\n")
    parts.extend([
        f"<change_units>\n  <change_unit id=\"CU-{task.id}\">"
        "当前 task 的全部 diff 变更；请声明该单元是否需要图谱事实。"
        "</change_unit>\n</change_units>\n",
        f"<knowledge_bundle>\n{knowledge}\n</knowledge_bundle>\n",
        "返回严格的 DirectTriageResult。",
    ])
    return "".join(parts)


def _invoke_once(
    *,
    llm: Any,
    system_prompt: str,
    user_prompt: str,
    max_retries: int,
    structured_method: str,
    reviewer: ReviewerKind | None = None,
    change_unit_id: str = "",
    location_file: str = "",
) -> tuple[DirectTriageResult | None, str]:
    structured_diagnostic = ""
    provider_payload: Any = None
    try:
        raw = invoke_with_retry(
            llm.with_structured_output(LlmDirectTriageResult, method=structured_method),
            [("system", system_prompt), ("human", user_prompt)],
            max_retries=max_retries,
        )
    except Exception as exc:  # noqa: BLE001
        # A provider/network failure after the structured retries is a
        # transport problem, not evidence that this reviewer found nothing.
        # Give the same bounded request one plain-JSON attempt before the
        # caller records a reviewer failure.  The fallback still validates the
        # exact DirectTriageResult contract and cannot synthesize a seed.
        error_diagnostic = f"triage_llm_error:{type(exc).__name__}"
        text_result, text_diagnostic = _invoke_text_fallback(
            llm=llm,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_retries=1,
            reviewer=reviewer,
            change_unit_id=change_unit_id,
            location_file=location_file,
        )
        if text_result is not None:
            return text_result, "triage_text_fallback_after_error"
        return None, ";".join(
            item for item in (error_diagnostic, text_diagnostic) if item
        )
    if raw is None:
        text_result, text_diagnostic = _invoke_text_fallback(
            llm=llm,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_retries=max_retries,
            reviewer=reviewer,
            change_unit_id=change_unit_id,
            location_file=location_file,
        )
        if text_result is not None:
            return text_result, "triage_text_fallback_used"
        return None, ";".join(
            item
            for item in ("triage_structured_output_missing", text_diagnostic)
            if item
        )
    try:
        # Validate once through the tolerant provider envelope, then convert
        # to the strict runtime contract after all unknown metadata is gone.
        provider_payload = raw.model_dump() if hasattr(raw, "model_dump") else raw
        provider_result = LlmDirectTriageResult.model_validate(provider_payload)
        payload = provider_result.model_dump()
        # Compatible providers occasionally append an empty object to the
        # issues array after a valid item.  It carries no claim, location,
        # mechanism, or graph question and cannot become a CandidateSeed.  Do
        # not let that display-only shell invalidate the complete response;
        # any non-empty malformed item still reaches strict validation and
        # fails closed.
        payload["issues"] = [
            item
            for item in payload.get("issues", ())
            if not _is_empty_provider_candidate(item)
        ]
        _fill_provider_envelope(
            payload,
            reviewer=reviewer,
            change_unit_id=change_unit_id,
            location_file=location_file,
        )
        for item in payload["issues"]:
            question = item.get("graph_question") if isinstance(item, dict) else None
            if not isinstance(question, dict):
                continue
            if not str(question.get("path_kind", "")).strip():
                question["path_kind"] = None
            if not str(question.get("direction", "")).strip():
                question["direction"] = "downstream"
        return DirectTriageResult.model_validate(payload), ""
    except Exception as exc:  # noqa: BLE001
        structured_diagnostic = f"triage_schema_invalid:{type(exc).__name__}"

    if provider_payload is not None:
        salvaged, salvage_diagnostic = _coerce_provider_result(
            provider_payload,
            reviewer=reviewer,
            change_unit_id=change_unit_id,
            location_file=location_file,
        )
        if salvaged is not None and salvaged.issues:
            return salvaged, salvage_diagnostic or "triage_provider_rows_salvaged"

    # Some OpenAI-compatible gateways occasionally return a normal assistant
    # message instead of the requested function call (LangChain exposes this
    # as ``None``).  A single text-protocol retry keeps controlled triage from
    # silently dropping an entire reviewer, while still requiring the exact
    # same typed envelope before anything can enter routing.  This is a
    # provider-agnostic transport fallback; it does not infer candidates or
    # read repository files locally.
    text_result, text_diagnostic = _invoke_text_fallback(
        llm=llm,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_retries=max_retries,
        reviewer=reviewer,
        change_unit_id=change_unit_id,
        location_file=location_file,
    )
    if text_result is not None:
        return text_result, "triage_text_fallback_used"
    return None, ";".join(
        item for item in (structured_diagnostic or "triage_structured_output_missing", text_diagnostic) if item
    )


def _invoke_text_fallback(
    *,
    llm: Any,
    system_prompt: str,
    user_prompt: str,
    max_retries: int,
    reviewer: ReviewerKind | None = None,
    change_unit_id: str = "",
    location_file: str = "",
) -> tuple[DirectTriageResult | None, str]:
    """Parse one bounded JSON-text response when structured output is absent."""

    fallback_system = (
        f"{system_prompt}\n\n"
        "结构化函数调用不可用。只输出一个 JSON 对象，字段必须与 DirectTriageResult 相同；"
        "不要输出 Markdown、解释文字、工具调用或代码围栏。"
    )
    try:
        message = invoke_with_retry(
            llm,
            [("system", fallback_system), ("human", user_prompt)],
            max_retries=max(1, max_retries),
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"triage_text_fallback_error:{type(exc).__name__}"
    content = _message_text(message)
    if not content:
        return None, "triage_text_fallback_empty"
    payload = _extract_json_object(content)
    if payload is None:
        return None, "triage_text_fallback_not_json"
    result, diagnostic = _coerce_provider_result(
        payload,
        reviewer=reviewer,
        change_unit_id=change_unit_id,
        location_file=location_file,
    )
    if result is not None:
        return result, ""
    return None, diagnostic or "triage_text_fallback_invalid"


def _coerce_provider_result(
    payload: Any,
    *,
    reviewer: ReviewerKind | None,
    change_unit_id: str,
    location_file: str,
) -> tuple[DirectTriageResult | None, str]:
    """Salvage independently valid issue rows from a malformed envelope.

    Compatible providers sometimes make one candidate row invalid while
    returning other usable rows.  Rejecting the whole envelope turns that
    transport defect into a recall failure.  Each row still passes the same
    strict ``CandidateSeed`` contract; only task-owned blank fields and a
    missing proof scope (derived from the presence of a graph question) are
    repaired.  No claim, mechanism, target, or evidence fact is synthesized.
    """

    if hasattr(payload, "model_dump"):
        payload = payload.model_dump()
    if not isinstance(payload, dict):
        return None, "triage_provider_payload_not_object"

    normalized: dict[str, Any] = {
        "coverage": [],
        "issues": [],
        "investigation_seeds": [],
        "limitations": payload.get("limitations", ()),
    }
    coverage_rows = payload.get("coverage", ())
    if isinstance(coverage_rows, (list, tuple)):
        for row in coverage_rows:
            try:
                normalized["coverage"].append(
                    LlmCoverageDeclaration.model_validate(row).model_dump()
                )
            except Exception:  # noqa: BLE001 - malformed metadata is ignored
                continue

    issue_rows = payload.get("issues", ())
    if not isinstance(issue_rows, (list, tuple)):
        issue_rows = ()
    invalid_rows: list[str] = []
    for index, row in enumerate(issue_rows):
        if hasattr(row, "model_dump"):
            row = row.model_dump()
        if not isinstance(row, dict) or _is_empty_provider_candidate(row):
            continue
        candidate_payload = dict(row)
        _drop_null_provider_defaults(candidate_payload)
        _fill_provider_envelope(
            {"issues": [candidate_payload]},
            reviewer=reviewer,
            change_unit_id=change_unit_id,
            location_file=location_file,
        )
        question = candidate_payload.get("graph_question")
        if isinstance(question, dict):
            _drop_null_provider_defaults(question, graph_question=True)
            if _blank_provider_value(question.get("path_kind")):
                question["path_kind"] = None
            if _blank_provider_value(question.get("direction")):
                question["direction"] = "downstream"
        if _blank_provider_value(candidate_payload.get("proof_scope")):
            candidate_payload["proof_scope"] = (
                ProofScope.CROSS_FILE.value
                if isinstance(question, dict)
                else ProofScope.LOCAL.value
            )
        if _blank_provider_value(candidate_payload.get("evidence_need")):
            candidate_payload["evidence_need"] = EvidenceNeed.NONE.value
        try:
            candidate = LlmCandidateSeed.model_validate(candidate_payload)
            normalized["issues"].append(candidate.model_dump())
        except Exception as exc:  # noqa: BLE001 - reject only this row
            invalid_rows.append(f"{index}:{type(exc).__name__}")

    seed_rows = payload.get("investigation_seeds", ())
    if isinstance(seed_rows, (list, tuple)):
        for index, row in enumerate(seed_rows):
            if hasattr(row, "model_dump"):
                row = row.model_dump()
            if not isinstance(row, dict):
                continue
            seed_payload = dict(row)
            if _blank_provider_value(seed_payload.get("reviewer")) and reviewer is not None:
                seed_payload["reviewer"] = reviewer.value
            if _blank_provider_value(seed_payload.get("change_unit_id")):
                seed_payload["change_unit_id"] = change_unit_id
            if _blank_provider_value(seed_payload.get("location_file")):
                seed_payload["location_file"] = location_file
            try:
                normalized["investigation_seeds"].append(
                    InvestigationSeed.model_validate(seed_payload).model_dump()
                )
            except Exception as exc:  # noqa: BLE001 - reject only this row
                invalid_rows.append(f"investigation:{index}:{type(exc).__name__}")

    try:
        result = DirectTriageResult.model_validate(normalized)
    except Exception as exc:  # noqa: BLE001
        return None, f"triage_provider_payload_invalid:{type(exc).__name__}"
    diagnostic = (
        f"triage_provider_rows_skipped:{','.join(invalid_rows)}"
        if invalid_rows
        else ""
    )
    return result, diagnostic


_CANDIDATE_DEFAULT_FIELDS = frozenset(
    {
        "seed_id",
        "issue_type",
        "mechanism",
        "mechanism_note",
        "mechanism_note2",
        "graph_question_note",
        "confidence_note",
        "confidence_note2",
        "evidence_note",
        "evidence_basis_note",
        "evidence_need_note",
        "location_line_alt",
        "location_line_note",
        "overlaps_with",
        "claim_type",
        "limitations",
        "impact",
        "impact_locale",
        "suggestion",
        "confidence",
    }
)
_GRAPH_QUESTION_DEFAULT_FIELDS = frozenset(
    {
        "subject_ref",
        "direction",
        "path_kind",
        "expected_targets",
        "required_relationships",
        "max_depth",
        "question",
        "confidence_note",
        "evidence",
        "direction2",
    }
)


def _drop_null_provider_defaults(
    payload: dict[str, Any],
    *,
    graph_question: bool = False,
) -> None:
    """Treat explicit JSON null as an omitted schema-default field.

    Several compatible endpoints serialize optional display fields as
    ``null`` even though the contract declares string/tuple defaults.  Null
    is not a candidate fact here; deleting only fields that already have a
    model default lets Pydantic apply that default while required semantic
    fields (claim, reviewer, proof_scope, and evidence_need) still fail
    closed and are never invented by the runtime.
    """

    default_fields = (
        _GRAPH_QUESTION_DEFAULT_FIELDS
        if graph_question
        else _CANDIDATE_DEFAULT_FIELDS
    )
    for key in tuple(payload):
        if key in default_fields and payload[key] is None:
            payload.pop(key)


def _fill_provider_envelope(
    payload: dict[str, Any],
    *,
    reviewer: ReviewerKind | None,
    change_unit_id: str,
    location_file: str,
) -> None:
    """Fill caller-owned task envelope fields at the provider boundary.

    DirectTriage lets the model leave routing and location discriminators
    blank because they belong to the current task.  Filling only blank values
    makes the provider's ``system binds`` convention executable without
    inventing a claim, proof scope, or evidence requirement.
    """

    for item in payload.get("issues", ()):
        if not isinstance(item, dict):
            continue
        if reviewer is not None and _blank_provider_value(item.get("reviewer")):
            item["reviewer"] = reviewer.value
        if change_unit_id and _blank_provider_value(item.get("change_unit_id")):
            item["change_unit_id"] = change_unit_id
        if location_file and _blank_provider_value(item.get("location_file")):
            item["location_file"] = location_file


def _blank_provider_value(value: Any) -> bool:
    """Treat null and whitespace as the provider's omitted envelope value."""

    return value is None or not str(value).strip()


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts).strip()
    return ""


def _extract_json_object(content: str) -> dict[str, Any] | None:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        # Permit a short provider preamble, but only parse the first complete
        # JSON object.  No natural-language fields are interpreted as facts.
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None


def _is_empty_provider_candidate(value: Any) -> bool:
    """Return whether a provider issue object is an empty serialization shell."""

    if isinstance(value, LlmCandidateSeed):
        payload = value.model_dump()
    elif isinstance(value, dict):
        payload = value
    else:
        return False
    return not any(
        str(payload.get(field, "")).strip()
        for field in ("claim", "mechanism", "location_file", "change_unit_id")
    ) and payload.get("graph_question") is None


def run_direct_triage(
    *,
    reviewer: ReviewerKind,
    task: ReviewTask,
    symbol_context: TaskSymbolContext | None,
    llm: Any,
    diff_summary: str,
    task_knowledge: str,
    max_retries: int,
    structured_method: str,
    max_seeds_per_change_unit: int = 4,
    max_seeds_per_reviewer: int = 4,
) -> tuple[DirectTriageResult | None, tuple[str, ...]]:
    """执行一次 reviewer DirectTriage；非法结果只允许一次修复重试。"""

    if llm is None:
        return None, ("triage_llm_unavailable",)
    system = _prompt_for(reviewer)
    user = build_triage_user_prompt(
        task=task,
        symbol_context=symbol_context,
        diff_summary=diff_summary,
        task_knowledge=task_knowledge,
    )
    result, diagnostic = _invoke_once(
        llm=llm,
        system_prompt=system,
        user_prompt=user,
        max_retries=max_retries,
        structured_method=structured_method,
        reviewer=reviewer,
        change_unit_id=f"CU-{task.id}",
        location_file=task.file,
    )
    diagnostics: list[str] = [f"triage_prompt_hash:{prompt_hash(reviewer)}"]
    if result is None:
        diagnostics.append(diagnostic)
        repair_user = (
            f"{user}\n\n上一次输出未通过受控协议校验（{diagnostic}）。"
            "请修复为严格 DirectTriageResult；不要添加字段，不要调用工具。"
        )
        result, repair_diagnostic = _invoke_once(
            llm=llm,
            system_prompt=system,
            user_prompt=repair_user,
            max_retries=1,
            structured_method=structured_method,
            reviewer=reviewer,
            change_unit_id=f"CU-{task.id}",
            location_file=task.file,
        )
        if result is None:
            diagnostics.append(repair_diagnostic)
            return None, tuple(diagnostics)
        diagnostics.append("triage_protocol_repaired")

    expected_ids = (f"CU-{task.id}",)
    coverage_errors = validate_coverage(change_unit_ids=expected_ids, result=result)
    coverage_was_repaired = bool(coverage_errors)
    if coverage_errors:
        diagnostics.extend(coverage_errors)
        normalized_coverage = _normalize_coverage(
            result.coverage,
            expected_ids=expected_ids,
            task=task,
        )
        if normalized_coverage is None:
            return None, tuple(diagnostics)
        result = result.model_copy(update={"coverage": normalized_coverage})
        diagnostics.append("coverage_normalized")
    coverage_by_unit = {
        declaration.change_unit_id: declaration.decision
        for declaration in result.coverage
    }
    graph_seed_units = {
        seed.change_unit_id
        for seed in result.issues
        if seed.graph_question is not None and seed.change_unit_id
    }
    for change_unit_id in graph_seed_units:
        if coverage_by_unit.get(change_unit_id) is CoverageDecision.LOCAL_ONLY:
            # A provider may classify the unit as local_only while emitting a
            # valid candidate that explicitly requests graph evidence.  The
            # per-candidate executable request is the more specific contract;
            # dropping it here loses recall before Judge can inspect the
            # evidence.  Promote only that unit to graph_needed (no symbols,
            # facts, or candidate text are synthesized).
            coverage_by_unit[change_unit_id] = CoverageDecision.GRAPH_NEEDED
            diagnostics.append(
                f"coverage_promoted_for_graph_seed:{change_unit_id}"
            )
    promoted_units = {
        change_unit_id
        for change_unit_id in graph_seed_units
        if coverage_by_unit.get(change_unit_id) is CoverageDecision.GRAPH_NEEDED
        and any(
            declaration.change_unit_id == change_unit_id
            and declaration.decision is CoverageDecision.LOCAL_ONLY
            for declaration in result.coverage
        )
    }
    if promoted_units:
        result = result.model_copy(
            update={
                "coverage": tuple(
                    declaration.model_copy(
                        update={
                            "decision": CoverageDecision.GRAPH_NEEDED,
                            "reason": (
                                declaration.reason.strip()
                                + "；候选显式请求图谱证据，已提升为 graph_needed"
                            ).strip("；"),
                        }
                    )
                    if declaration.change_unit_id in promoted_units
                    else declaration
                    for declaration in result.coverage
                )
            }
        )

    # OpenAI-compatible providers occasionally place candidate objects inside
    # a coverage row's explanatory ``issues`` field instead of the top-level
    # DirectTriageResult.  Recover only objects that can be validated as the
    # declared CandidateSeed contract; arbitrary coverage prose is ignored.
    result, recovered_count = _recover_embedded_candidates(
        result,
        reviewer=reviewer,
        task=task,
    )
    if recovered_count:
        diagnostics.append(f"coverage_embedded_candidates_recovered:{recovered_count}")
    if not result.issues and (
        coverage_was_repaired
        or any(
            declaration.decision is CoverageDecision.GRAPH_NEEDED
            for declaration in result.coverage
        )
    ):
        # An empty issue list is only trusted when the provider also returned
        # a complete coverage envelope.  Missing/invalid coverage is a common
        # compatible-provider shape failure, not evidence that every changed
        # region was reviewed.  Give the same triage model one bounded,
        # explicit enumeration pass; this remains generic and does not
        # synthesize a candidate locally.
        recheck_user = (
            f"{user}\n\n"
            "上一次响应的 coverage 不完整或声明需要进一步核验，但 issues 数组为空。"
            "请重新检查本 task 的所有变更，逐条输出需要图谱核验的 CandidateSeed；"
            "局部问题也要输出为 CandidateSeed；若确认没有候选，才返回 issues=[]。"
            "候选必须放在 DirectTriageResult.issues 顶层，"
            "不要把候选对象放入 coverage.issues 或其它解释字段。"
        )
        rechecked, recheck_diagnostic = _invoke_once(
            llm=llm,
            system_prompt=system,
            user_prompt=recheck_user,
            max_retries=1,
            structured_method=structured_method,
            reviewer=reviewer,
            change_unit_id=f"CU-{task.id}",
            location_file=task.file,
        )
        if rechecked is not None:
            rechecked, recovered_on_recheck = _recover_embedded_candidates(
                rechecked,
                reviewer=reviewer,
                task=task,
            )
            if rechecked.issues:
                result = result.model_copy(
                    update={
                        "issues": rechecked.issues,
                        "coverage": _merge_recheck_coverage(
                            result.coverage,
                            rechecked.coverage,
                            expected_ids=expected_ids,
                            task=task,
                        ),
                    }
                )
                diagnostics.append(
                    f"empty_graph_recheck_recovered:{len(rechecked.issues)}"
                )
                if recovered_on_recheck:
                    diagnostics.append(
                        f"empty_graph_recheck_embedded_recovered:{recovered_on_recheck}"
                    )
            else:
                diagnostics.append("empty_graph_recheck_confirmed_no_candidates")
        else:
            diagnostics.append(recheck_diagnostic or "empty_graph_recheck_failed")

    if (
        result.issues
        and (
            coverage_was_repaired
            or any(
                declaration.decision is CoverageDecision.GRAPH_NEEDED
                for declaration in result.coverage
            )
        )
        and _has_uncovered_change_cluster(result.issues, task)
    ):
        # A non-empty response can still cover only one of several independent
        # changed regions.  Ask for one bounded completeness pass rather than
        # relying on the model's first sampling order.  This is based solely
        # on task line coverage; it does not identify or synthesize a bug.
        uncovered = _uncovered_change_clusters(result.issues, task)
        uncovered_text = ", ".join(
            f"[{cluster[0]}-{cluster[-1]}]" for cluster in uncovered
        )
        coverage_recheck_user = (
            f"{user}\n\n"
            f"当前 issues 只覆盖了部分变更行，但 coverage 声明为 graph_needed。未覆盖区间：{uncovered_text}。"
            "请按每个相互独立的变更行区间重新枚举候选：保留已有候选并补充这些区间遗漏的"
            "具体机制；每个候选必须使用陈述句 claim、当前 task 文件位置，并为"
            "跨 symbol 主张填写最短 GraphQuestion。若某个区间确实没有问题，"
            "可以不补候选，但不要只返回疑问句或把对象放入 coverage。"
        )
        rechecked, recheck_diagnostic = _invoke_once(
            llm=llm,
            system_prompt=system,
            user_prompt=coverage_recheck_user,
            max_retries=1,
            structured_method=structured_method,
            reviewer=reviewer,
            change_unit_id=f"CU-{task.id}",
            location_file=task.file,
        )
        if rechecked is not None:
            rechecked, recovered_on_recheck = _recover_embedded_candidates(
                rechecked,
                reviewer=reviewer,
                task=task,
            )
            merged = list(result.issues)
            fingerprints = {
                (
                    seed.claim.strip(),
                    seed.location_file.replace("\\", "/").lower(),
                    seed.location_line,
                )
                for seed in merged
            }
            for seed in rechecked.issues:
                fingerprint = (
                    seed.claim.strip(),
                    seed.location_file.replace("\\", "/").lower(),
                    seed.location_line,
                )
                if fingerprint not in fingerprints:
                    fingerprints.add(fingerprint)
                    merged.append(seed)
            result = result.model_copy(
                update={
                    "issues": tuple(merged),
                    "coverage": _merge_recheck_coverage(
                        result.coverage,
                        rechecked.coverage,
                        expected_ids=expected_ids,
                        task=task,
                    ),
                }
            )
            diagnostics.append(
                f"coverage_gap_recheck_completed:{len(rechecked.issues)}"
            )
            if recovered_on_recheck:
                diagnostics.append(
                    f"coverage_gap_embedded_recovered:{recovered_on_recheck}"
                )
        else:
            diagnostics.append(
                recheck_diagnostic or "coverage_gap_recheck_failed"
            )

        # A malformed or repetitive first recheck can still leave the same
        # line cluster uncovered.  One explicit follow-up is cheaper and more
        # predictable than allowing the caller to reopen free-form exploration;
        # the runtime still never fabricates a CandidateSeed.
        if _has_uncovered_change_cluster(result.issues, task):
            remaining = _uncovered_change_clusters(result.issues, task)
            remaining_text = ", ".join(
                f"[{cluster[0]}-{cluster[-1]}]" for cluster in remaining
            )
            followup_user = (
                f"{user}\n\n"
                f"上一轮补全后仍未覆盖变更区间 {remaining_text}。只做一次协议化复核："
                "逐个检查这些区间是否存在具体问题。保留已有候选，若存在遗漏则在"
                "DirectTriageResult.issues 顶层新增候选；若没有问题则明确返回空，"
                "不要把 coverage 解释字段当作候选，不要改写其它区间的候选。"
            )
            followed, followup_diagnostic = _invoke_once(
                llm=llm,
                system_prompt=system,
                user_prompt=followup_user,
                max_retries=1,
                structured_method=structured_method,
                reviewer=reviewer,
                change_unit_id=f"CU-{task.id}",
                location_file=task.file,
            )
            if followed is not None:
                followed, recovered_on_followup = _recover_embedded_candidates(
                    followed,
                    reviewer=reviewer,
                    task=task,
                )
                merged = list(result.issues)
                fingerprints = {
                    (
                        seed.claim.strip(),
                        seed.location_file.replace("\\", "/").lower(),
                        seed.location_line,
                    )
                    for seed in merged
                }
                for seed in followed.issues:
                    fingerprint = (
                        seed.claim.strip(),
                        seed.location_file.replace("\\", "/").lower(),
                        seed.location_line,
                    )
                    if fingerprint not in fingerprints:
                        fingerprints.add(fingerprint)
                        merged.append(seed)
                result = result.model_copy(
                    update={
                        "issues": tuple(merged),
                        "coverage": _merge_recheck_coverage(
                            result.coverage,
                            followed.coverage,
                            expected_ids=expected_ids,
                            task=task,
                        ),
                    }
                )
                diagnostics.append(
                    f"coverage_gap_followup_completed:{len(followed.issues)}"
                )
                if recovered_on_followup:
                    diagnostics.append(
                        f"coverage_gap_followup_embedded_recovered:{recovered_on_followup}"
                    )
            else:
                diagnostics.append(
                    followup_diagnostic or "coverage_gap_followup_failed"
                )

    claim_style_repair = _needs_claim_style_repair(result.issues)
    claim_consequence_repair = _needs_claim_consequence_repair(result.issues)
    claim_scope_repair = _needs_claim_scope_repair(result.issues, task=task)
    claim_grounding_repair = _needs_claim_grounding_repair(
        result.issues,
        task=task,
        symbol_context=symbol_context,
    )
    claim_self_call_repair = _needs_claim_self_call_repair(result.issues)
    claim_local_modifier_repair = _needs_local_modifier_repair(
        result.issues,
        task=task,
    )
    if (
        claim_style_repair
        or claim_consequence_repair
        or claim_scope_repair
        or claim_grounding_repair
        or claim_self_call_repair
        or claim_local_modifier_repair
    ):
        # A candidate is a bounded assertion, not an open question.  Some
        # compatible providers still serialize the investigation question in
        # ``claim`` or copy an identifier that was not present in the task.
        # Give the same triage model one protocol-only repair pass.  This does
        # not infer or rewrite a finding locally.
        repair_requirements: list[str] = []
        if claim_style_repair:
            repair_requirements.append(
                "将疑问句式 claim 改为针对 diff 机制的陈述句"
            )
        if claim_consequence_repair:
            repair_requirements.append(
                "对 graph_needed 候选在 claim 中补充由当前机制直接导致的可观察后果；"
                "不得只描述代码移动/返回值变化，也不得编造调用方或运行时事实"
            )
        if claim_scope_repair:
            repair_requirements.append(
                "每个 CandidateSeed 只能覆盖 location_line 所在的一个连续变更区间；"
                "当前候选把多个独立区间或多个独立机制混在一起，必须按区间拆开，"
                "分别保留对应位置和最短可观察后果。若多个区间各有问题，输出多个"
                "独立候选；不得用一个候选同时声称两个区间的因果链，也不得为了拆分"
                "编造调用方、消费者或运行时事实"
            )
        if claim_grounding_repair:
            repair_requirements.append(
                "把 claim/mechanism 中未出现在 task_patch 或 symbol_context 的代码标识符"
                "替换为当前方法/该调用点等中性表述"
            )
        if claim_self_call_repair:
            repair_requirements.append(
                "不要把调用另一个 symbol 写成递归或自调用；只有 GraphQuestion 的目标"
                "与 subject 是同一 symbol 且明确形成回边时才可使用该术语。若当前"
                "候选是方法 A 的 return 调用方法 B，只陈述 A 的返回责任变化，不声称"
                "B 的内部实现或递归"
            )
        if claim_local_modifier_repair:
            repair_requirements.append(
                "对直接删除 volatile、synchronized、锁或原子保护的局部候选，"
                "只保留变更本身及可直接推出的可见性、互斥或原子性保证下降；"
                "删除‘若/如果/可能存在调用方或运行时修改’等未被当前 task 证明的前提，"
                "不要把该候选改成 graph_needed，也不要新增跨线程调用事实"
            )
        style_user = (
            f"{user}\n\n"
            "请只做协议表达修复："
            + "；".join(repair_requirements)
            + "；保留原有候选数量、位置、proof_scope、evidence_need 和 graph_question，"
            "不要新增事实，不要删除需要核验的候选，不要把候选放入 coverage 或其它字段。"
        )
        repaired, repair_diagnostic = _invoke_once(
            llm=llm,
            system_prompt=system,
            user_prompt=style_user,
            max_retries=1,
            structured_method=structured_method,
            reviewer=reviewer,
            change_unit_id=f"CU-{task.id}",
            location_file=task.file,
        )
        if repaired is not None:
            repaired, recovered_on_style_repair = _recover_embedded_candidates(
                repaired,
                reviewer=reviewer,
                task=task,
            )
            if repaired.issues:
                result = result.model_copy(
                    update={
                        "issues": _merge_protocol_repair_issues(
                            result.issues,
                            repaired.issues,
                        ),
                        "coverage": _merge_recheck_coverage(
                            result.coverage,
                            repaired.coverage,
                            expected_ids=expected_ids,
                            task=task,
                        ),
                    }
                )
                diagnostics.append("claim_protocol_repaired")
                if recovered_on_style_repair:
                    diagnostics.append(
                        f"claim_style_embedded_recovered:{recovered_on_style_repair}"
                    )
            else:
                diagnostics.append("claim_style_repair_empty_ignored")
        else:
            diagnostics.append(repair_diagnostic or "claim_style_repair_failed")

    # Bind both legacy CandidateSeed IDs and neutral InvestigationSeed IDs
    # once at the controlled boundary.  The latter are never passed to the
    # subtask React as candidate claims.
    result = bind_seed_ids(result)
    normalized_investigations: list[InvestigationSeed] = []
    visible_symbol_ids = {
        symbol.symbol_id
        for symbol in (symbol_context.symbols if symbol_context is not None else ())
        if symbol.symbol_id
    }
    allowed_investigation_tools = {
        "get_file_content",
        "inspect_structure",
        "inspect_change_impact",
        "inspect_path",
    }
    for investigation_seed in result.investigation_seeds:
        investigation_seed = normalize_investigation_seed(investigation_seed)
        if investigation_seed.reviewer is not reviewer:
            diagnostics.append(
                f"{investigation_seed.seed_id}:investigation_reviewer_mismatch"
            )
            continue
        if investigation_seed.change_unit_id not in set(expected_ids):
            if len(expected_ids) == 1:
                investigation_seed = investigation_seed.model_copy(
                    update={"change_unit_id": expected_ids[0]}
                )
                diagnostics.append(
                    f"{investigation_seed.seed_id}:investigation_change_unit_repaired"
                )
            else:
                diagnostics.append(
                    f"{investigation_seed.seed_id}:investigation_unknown_change_unit"
                )
                continue
        if not set(investigation_seed.initial_symbol_ids).issubset(visible_symbol_ids):
            diagnostics.append(
                f"{investigation_seed.seed_id}:investigation_unknown_symbol"
            )
            continue
        tools = coherent_tool_bundle(
            investigation_seed,
            investigation_seed.allowed_tools,
            domain_tools=allowed_investigation_tools,
            max_tools=3,
        )
        if tools != investigation_seed.allowed_tools:
            investigation_seed = investigation_seed.model_copy(
                update={"allowed_tools": tools}
            )
        if not tools:
            diagnostics.append(
                f"{investigation_seed.seed_id}:investigation_no_allowed_tool"
            )
            continue
        normalized_investigations.append(investigation_seed)
        if len(normalized_investigations) >= max_seeds_per_reviewer:
            diagnostics.append("investigation_seed_reviewer_limit")
            break
    normalized_issues: list[CandidateSeed] = []
    for seed in result.issues:
        if not seed.mechanism.strip():
            seed = seed.model_copy(update={"mechanism": seed.claim})
            diagnostics.append(f"{seed.seed_id}:mechanism_filled_from_claim")
        if seed.reviewer is not reviewer:
            diagnostics.append(f"{seed.seed_id}:reviewer_mismatch:{seed.reviewer.value}")
            continue
        # Normalize provider aliases/defaults before validating the graph
        # question.  Providers often omit expected_targets and relationship
        # kinds even though the subject and direction are executable; the
        # conservative CALLS baseline is precisely the contract for that
        # case, and validating first would discard the seed too early.
        if seed.change_unit_id not in set(expected_ids):
            # A single task has exactly one canonical ChangeUnit.  Providers
            # occasionally prefix or truncate that id while still returning a
            # candidate for the task file.  Repair this protocol-only typo
            # when the destination is unambiguous; multi-unit tasks continue
            # to fail closed rather than guessing ownership.
            if len(expected_ids) == 1 and _same_file(seed.location_file, task.file):
                diagnostics.append(
                    f"{seed.seed_id}:change_unit_alias_repaired:{seed.change_unit_id}"
                )
                seed = seed.model_copy(update={"change_unit_id": expected_ids[0]})
        seed = _normalize_graph_question(
            seed,
            symbol_context,
            task=task,
            reviewer=reviewer,
        )
        seed, timing_repair = _normalize_state_timing_question(seed, task=task)
        if timing_repair:
            diagnostics.append(f"{seed.seed_id}:{timing_repair}")
        if (
            seed.proof_scope is ProofScope.LOCAL
            and seed.graph_question is None
            and seed.evidence_need is EvidenceNeed.NONE
            and not seed.evidence_basis
        ):
            # ``evidence_basis`` is a routing discriminator, not a semantic
            # conclusion.  Compatible providers frequently omit this optional
            # list after correctly classifying a local finding.  The current
            # task always supplies a patch (and normally resolved symbols), so
            # bind only those caller-owned basis labels instead of turning a
            # valid local candidate into an unresolved seed.
            basis: list[str] = []
            if task.changed_lines or task.deletion_anchors:
                basis.append("changed_lines")
            elif any(
                line.startswith("-") and not line.startswith("---")
                for line in task.patch.splitlines()
            ):
                # A deleted-file fallback has no new-side line and therefore
                # cannot expose a DeletionAnchor.  The patch artifact still
                # contains the complete old-side deletion and is a valid
                # local proof for a mechanism stated only at that boundary.
                basis.append("deletion_patch")
            if symbol_context is not None and symbol_context.symbols:
                basis.append("local_source")
            if basis:
                seed = seed.model_copy(update={"evidence_basis": tuple(basis)})
                diagnostics.append(f"{seed.seed_id}:local_evidence_basis_repaired")
        # Providers sometimes label a candidate ``LOCAL`` while also
        # returning a graph question. The contradiction used to discard the
        # seed before its claim could be normalized. Prefer the explicit
        # evidence request: promote it to the bounded cross-file route and
        # let GraphPlan/Judge decide whether the graph fact supports it.
        if (
            seed.proof_scope is ProofScope.LOCAL
            and seed.graph_question is not None
        ):
            seed = seed.model_copy(update={"proof_scope": ProofScope.CROSS_FILE})
            diagnostics.append(f"{seed.seed_id}:local_scope_promoted_for_graph")
        seed_errors = validate_seed(seed, change_unit_ids=set(expected_ids))
        if seed_errors:
            diagnostics.extend(f"{seed.seed_id}:{item}" for item in seed_errors)
            continue
        coverage_decision = coverage_by_unit.get(seed.change_unit_id)
        if coverage_decision is CoverageDecision.NOT_APPLICABLE:
            diagnostics.append(f"{seed.seed_id}:candidate_on_not_applicable_unit")
            continue
        if (
            coverage_decision is CoverageDecision.LOCAL_ONLY
            and route_seed(seed) == "graph_required"
        ):
            # This branch is retained for defensive callers that provide a
            # stale coverage map; normal responses are promoted above.
            diagnostics.append(f"{seed.seed_id}:graph_seed_on_local_only_unit_promoted")
        normalized_location = _normalize_seed_location(seed, task)
        if normalized_location[1]:
            diagnostics.extend(
                f"{seed.seed_id}:{item}" for item in normalized_location[1]
            )
        if normalized_location[0] is None:
            continue
        seed = normalized_location[0]
        normalized_issues.append(seed)
        diagnostics.append(f"seed_route:{seed.seed_id}:{route_seed(seed)}")
    selected_issues, budget_diagnostics = _select_seed_budget(
        normalized_issues,
        task=task,
        max_seeds_per_change_unit=max_seeds_per_change_unit,
        max_seeds_per_reviewer=max_seeds_per_reviewer,
    )
    diagnostics.extend(budget_diagnostics)
    return result.model_copy(
        update={
            "issues": tuple(selected_issues),
            "investigation_seeds": tuple(normalized_investigations),
        }
    ), tuple(diagnostics)


def _needs_claim_style_repair(issues: tuple[CandidateSeed, ...]) -> bool:
    """Detect question-shaped claims without judging their semantic content."""

    question_prefixes = ("是否", "能否", "会不会", "有没有", "是否存在", "能不能")
    return any(
        claim
        and (
            claim.endswith(("?", "？"))
            or claim.lstrip().startswith(question_prefixes)
        )
        for claim in (seed.claim.strip() for seed in issues)
    )


def _needs_claim_consequence_repair(issues: tuple[CandidateSeed, ...]) -> bool:
    """Require graph candidates to state an observable consequence.

    This is a protocol-shape check, not a bug detector.  Graph evidence can
    only validate a concrete causal claim; a sentence that merely repeats a
    moved call or changed return expression gives the Judge no adverse
    consequence to verify.  The repair remains LLM-owned and candidates are
    never dropped solely by this heuristic.
    """

    consequence_markers = (
        "影响",
        "导致",
        "造成",
        "使得",
        "从而",
        "丢失",
        "错误",
        "异常",
        "不一致",
        "不可用",
        "缺失",
        "失效",
        "affect",
        "cause",
        "lead",
        "loss",
        "wrong",
        "error",
        "inconsistent",
        "unavailable",
        "missing",
        "break",
        "fail",
    )
    # A moved operation can contain a consequence word (for example
    # ``不一致``) and still leave the affected observer completely implicit.
    # Such a seed is hard for the later Judge to validate: the graph may prove
    # a callback, listener, caller, or state consumer, while the candidate only
    # talks about the local branch.  Require a generic observer/seam marker for
    # ordering and lifecycle claims; this does not name a domain or infer a
    # defect, it only asks the LLM to make the causal boundary explicit.
    ordering_markers = (
        "移动",
        "迁移",
        "时序",
        "顺序",
        "之前",
        "之后",
        "前",
        "后",
        "register",
        "注册",
        "清理",
        "设置",
        "赋值",
        "返回",
        "调用",
        "move",
        "order",
        "before",
        "after",
        "return",
        "call",
    )
    observer_markers = (
        "下游",
        "调用方",
        "消费者",
        "观察者",
        "回调",
        "监听",
        "状态读取",
        "读取方",
        "处理方",
        "后续逻辑",
        "入口",
        "downstream",
        "caller",
        "consumer",
        "observer",
        "callback",
        "listener",
        "state reader",
    )
    for seed in issues:
        if seed.proof_scope is ProofScope.LOCAL or seed.graph_question is None:
            continue
        claim = seed.claim.strip().lower()
        if claim and not any(marker in claim for marker in consequence_markers):
            return True
        if (
            claim
            and any(marker in claim for marker in ordering_markers)
            and not any(marker in claim for marker in observer_markers)
        ):
            return True
    return False


def _needs_claim_scope_repair(
    issues: tuple[CandidateSeed, ...],
    *,
    task: ReviewTask,
) -> bool:
    """Detect one candidate that mixes independent changed-line clusters.

    A candidate spanning two unrelated hunks gives the Judge an impossible
    contract: evidence for one hunk is evaluated as if it proved the other.
    This lexical check only compares identifiers present on changed patch lines
    with identifiers in the candidate text.  It never decides whether either
    mechanism is a bug; the single protocol repair remains LLM-owned.
    """

    clusters = _changed_line_clusters(task)
    if len(clusters) < 2:
        return False
    cluster_tokens = _patch_changed_tokens_by_cluster(task, clusters)
    if sum(bool(tokens) for tokens in cluster_tokens) < 2:
        return False
    for seed in issues:
        if seed.proof_scope is ProofScope.LOCAL:
            # Local findings may legitimately mention a surrounding expression
            # from the same source excerpt; only graph candidates need the
            # one-mechanism boundary before cross-symbol evidence is planned.
            continue
        location_cluster = _cluster_for_line(seed.location_line, clusters)
        if location_cluster is None:
            continue
        text_tokens = _claim_code_tokens(seed)
        matching_clusters = [
            index
            for index, tokens in enumerate(cluster_tokens)
            if tokens and len(text_tokens & tokens) >= 1
        ]
        if location_cluster in matching_clusters and len(matching_clusters) > 1:
            return True
    return False


def _changed_line_clusters(task: ReviewTask) -> tuple[tuple[int, ...], ...]:
    """Group new/deleted anchor lines into bounded independent regions."""

    lines = sorted(
        {
            *task.changed_lines,
            *(anchor.anchor_line for anchor in task.deletion_anchors),
        }
    )
    if not lines:
        return ()
    clusters: list[list[int]] = [[lines[0]]]
    for line in lines[1:]:
        if line - clusters[-1][-1] <= 8:
            clusters[-1].append(line)
        else:
            clusters.append([line])
    return tuple(tuple(cluster) for cluster in clusters)


def _cluster_for_line(line: int, clusters: tuple[tuple[int, ...], ...]) -> int | None:
    if line <= 0:
        return None
    for index, cluster in enumerate(clusters):
        if min(cluster) <= line <= max(cluster):
            return index
    return None


def _claim_code_tokens(seed: CandidateSeed) -> set[str]:
    """Extract conservative code-like words from a candidate explanation."""

    text = " ".join(
        (seed.claim, seed.mechanism, seed.impact, seed.suggestion)
    ).lower()
    ignored = {
        "return",
        "throw",
        "new",
        "true",
        "false",
        "null",
        "this",
        "context",
        "state",
        "result",
        "value",
        "object",
        "method",
        "current",
        "call",
        "path",
        "before",
        "after",
        "可能",
        "影响",
        "导致",
    }
    tokens = set(
        re.findall(
            r"[A-Za-z_$][A-Za-z0-9_$]*(?:[.#][A-Za-z_$][A-Za-z0-9_$]*)*",
            text,
        )
    )
    return {
        token
        for token in tokens
        if token not in ignored and (len(token) >= 4 or any(char in token for char in ".#_$"))
    }


def _patch_changed_tokens_by_cluster(
    task: ReviewTask,
    clusters: tuple[tuple[int, ...], ...],
) -> tuple[set[str], ...]:
    """Map identifiers on +/- patch lines to their changed-line cluster.

    Unified-diff line counters are used only for locality.  The patch remains
    the sole source of tokens; no symbol names or facts are inferred here.
    """

    by_cluster = [set() for _ in clusters]
    old_line = new_line = 0
    for raw in task.patch.splitlines():
        if raw.startswith("@@"):
            match = re.search(r"-([0-9]+)(?:,[0-9]+)? \+([0-9]+)(?:,[0-9]+)?", raw)
            if match:
                old_line, new_line = (int(match.group(1)), int(match.group(2)))
            continue
        if not raw or raw[0] not in "+- " or raw.startswith(("+++", "---")):
            continue
        marker = raw[0]
        line = new_line if marker == "+" else old_line
        if marker in "+-":
            index = _nearest_cluster_for_line(line, clusters)
            if index is not None:
                by_cluster[index].update(
                    token.lower()
                    for token in re.findall(
                        r"[A-Za-z_$][A-Za-z0-9_$]*(?:[.#][A-Za-z_$][A-Za-z0-9_$]*)*",
                        raw[1:],
                    )
                    if len(token) >= 4
                )
        if marker == "+":
            new_line += 1
        elif marker == "-":
            old_line += 1
        else:
            old_line += 1
            new_line += 1
    return tuple(by_cluster)


def _nearest_cluster_for_line(
    line: int,
    clusters: tuple[tuple[int, ...], ...],
) -> int | None:
    if line <= 0:
        return None
    distances = [
        min(abs(line - point) for point in cluster)
        for cluster in clusters
    ]
    if not distances:
        return None
    index = min(range(len(distances)), key=distances.__getitem__)
    return index if distances[index] <= 8 else None


def _needs_claim_grounding_repair(
    issues: tuple[CandidateSeed, ...],
    *,
    task: ReviewTask,
    symbol_context: TaskSymbolContext | None,
) -> bool:
    """Detect code-like names in claims that are absent from task evidence.

    The check is intentionally lexical and conservative.  It does not decide
    whether a finding is real; it only catches a provider inventing a method,
    type, or qualified symbol while restating a hypothesis.  Natural-language
    words are ignored, and the actual repair remains an LLM protocol pass.
    """

    known_text = task.patch
    if symbol_context is not None:
        known_text += "\n" + "\n".join(
            f"{symbol.symbol_id} {symbol.signature}"
            for symbol in symbol_context.symbols
        )
    known = set(re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*(?:[.#][A-Za-z_$][A-Za-z0-9_$]*)*", known_text))
    known_lower = known_text.lower()
    # Common prose/code words are not identifiers whose provenance can be
    # checked against a patch (e.g. ``return`` or ``context``).
    ignored = {
        "return",
        "throw",
        "new",
        "true",
        "false",
        "null",
        "this",
        "if",
        "else",
        "while",
        "for",
        "try",
        "catch",
        "finally",
        "context",
        "state",
        "result",
        "value",
        "object",
        "method",
        "current",
        "call",
        "path",
    }
    for seed in issues:
        text = " ".join((seed.claim, seed.mechanism, seed.impact, seed.suggestion))
        # Control-flow nouns are easy for a provider to hallucinate while
        # paraphrasing a diff.  If a claim introduces one that is absent from
        # the task patch and resolved symbol context, ask for a wording repair
        # before it can become a graph question.  This checks provenance only;
        # it does not decide whether the actual change is defective.
        unsupported_constructs = (
            "finally",
            "try/catch",
            "try catch",
            "synchronized",
            "switch",
            "lambda",
            "do-while",
        )
        if any(construct in text.lower() and construct not in known_lower for construct in unsupported_constructs):
            return True
        for token in re.findall(
            r"[A-Za-z_$][A-Za-z0-9_$]*(?:[.#][A-Za-z_$][A-Za-z0-9_$]*)*",
            text,
        ):
            if token in known or token.lower() in ignored:
                continue
            # Only flag tokens that look like code identifiers.  Lowercase
            # prose words ("possible", "effect", ...) remain untouched.
            if (
                "." in token
                or "#" in token
                or "_" in token
                or any(char.isupper() for char in token[1:])
                or token.isupper()
            ):
                return True
    return False


def _needs_claim_self_call_repair(issues: tuple[CandidateSeed, ...]) -> bool:
    """Require proof before a graph candidate calls a cross-method call recursive.

    This is a protocol consistency check only.  It compares the candidate's
    own GraphQuestion subject/targets; it does not inspect the repository or
    decide whether recursion is a defect.  A self-call assertion is sent back
    through the normal one-shot wording repair unless the question actually
    names the same symbol as its target.
    """

    recursion_markers = (
        "递归",
        "自调用",
        "recursive",
        "recursion",
        "self-call",
        "self call",
    )

    def symbol_tail(value: str) -> str:
        text = value.strip().lower()
        if not text:
            return ""
        if "#" in text:
            text = text.rsplit("#", 1)[-1]
        text = text.split("(", 1)[0].strip()
        return text.rsplit(".", 1)[-1]

    for seed in issues:
        text = " ".join((seed.claim, seed.mechanism, seed.impact)).lower()
        if not any(marker in text for marker in recursion_markers):
            continue
        question = seed.graph_question
        if question is None:
            # Without a GraphQuestion there is no cross-symbol evidence that
            # can support the stronger term; ask the model to keep the claim
            # bounded rather than allowing the wording to pass through.
            return True
        subject = symbol_tail(question.subject_ref)
        targets = {symbol_tail(target) for target in question.expected_targets}
        if not subject or subject not in targets:
            return True
    return False


def _needs_local_modifier_repair(
    issues: tuple[CandidateSeed, ...],
    *,
    task: ReviewTask,
) -> bool:
    """Keep local synchronization-modifier findings free of unproved scope.

    Removing a concurrency modifier is directly visible in a patch.  A local
    candidate may state the lost guarantee, but adding a runtime setter,
    shared-instance, or cross-thread precondition makes Judge require graph
    evidence that this candidate intentionally does not request.  This helper
    only detects that protocol mismatch and delegates the wording repair to
    the same bounded LLM pass used for other candidate-shape repairs.
    """

    modifier_markers = (
        "volatile",
        "synchronized",
        "atomic",
        "lock",
        "mutex",
        "锁",
        "原子",
    )
    removal_lines = [
        line[1:].lower()
        for line in task.patch.splitlines()
        if line.startswith("-") and not line.startswith("---")
    ]
    if not any(
        any(marker in line for marker in modifier_markers)
        for line in removal_lines
    ):
        return False
    conditional_markers = (
        "若",
        "如果",
        "可能",
        "线程",
        "thread",
        "setter",
        "运行时",
        "runtime",
        "共享实例",
        "shared instance",
        "调用方",
        "caller",
    )
    for seed in issues:
        if (
            seed.proof_scope is not ProofScope.LOCAL
            or seed.graph_question is not None
            or seed.evidence_need is not EvidenceNeed.NONE
        ):
            continue
        text = " ".join(
            (seed.claim, seed.mechanism, seed.impact, seed.suggestion)
        ).lower()
        if any(marker in text for marker in conditional_markers):
            return True
    return False


def _has_uncovered_change_cluster(
    issues: tuple[CandidateSeed, ...], task: ReviewTask
) -> bool:
    """Return whether a graph-needed response skipped a changed-line region."""

    clusters = _uncovered_change_clusters(issues, task)
    return bool(clusters)


def _uncovered_change_clusters(
    issues: tuple[CandidateSeed, ...], task: ReviewTask
) -> tuple[tuple[int, ...], ...]:
    """Return changed-line clusters with no candidate location nearby."""

    lines = sorted(
        {
            *task.changed_lines,
            *(anchor.anchor_line for anchor in task.deletion_anchors),
        }
    )
    if not lines:
        return ()
    clusters: list[list[int]] = [[lines[0]]]
    for line in lines[1:]:
        if line - clusters[-1][-1] <= 8:
            clusters[-1].append(line)
        else:
            clusters.append([line])
    candidate_lines = [seed.location_line for seed in issues if seed.location_line > 0]
    return tuple(
        tuple(cluster)
        for cluster in clusters
        if not any(
            abs(candidate_line - line) <= 6
            for candidate_line in candidate_lines
            for line in cluster
        )
    )


def _same_file(left: str, right: str) -> bool:
    """Compare task paths without allowing an alias to cross files."""

    return left.replace("\\", "/").strip().lower() == right.replace("\\", "/").strip().lower()


def _seed_cluster(seed: CandidateSeed, task: ReviewTask) -> int | None:
    """Return the changed-line cluster nearest a candidate location."""

    lines = sorted(
        {
            *task.changed_lines,
            *(anchor.anchor_line for anchor in task.deletion_anchors),
        }
    )
    if not lines or seed.location_line <= 0:
        return None
    clusters: list[list[int]] = [[lines[0]]]
    for line in lines[1:]:
        if line - clusters[-1][-1] <= 8:
            clusters[-1].append(line)
        else:
            clusters.append([line])
    distances = [
        min(abs(seed.location_line - line) for line in cluster)
        for cluster in clusters
    ]
    nearest = min(distances)
    return distances.index(nearest) if nearest <= 6 else None


def _select_seed_budget(
    seeds: list[CandidateSeed],
    *,
    task: ReviewTask,
    max_seeds_per_change_unit: int,
    max_seeds_per_reviewer: int,
) -> tuple[list[CandidateSeed], tuple[str, ...]]:
    """Apply candidate caps while preserving independent changed regions.

    The selector uses only task locations and declared ChangeUnit ids.  It does
    not compare claims or infer whether two findings are the same bug.  At
    most one seed per changed-line cluster is reserved before the remaining
    slots are filled in model order, preventing duplicate first-cluster output
    from starving a separate hunk.
    """

    diagnostics: list[str] = []
    if max_seeds_per_reviewer <= 0:
        return [], ("seed_reviewer_limit",) if seeds else ()
    grouped: dict[str, list[CandidateSeed]] = {}
    for seed in seeds:
        grouped.setdefault(seed.change_unit_id, []).append(seed)
    selected: list[CandidateSeed] = []
    selected_ids: set[str] = set()
    for change_unit_id, unit_seeds in grouped.items():
        if max_seeds_per_change_unit <= 0:
            diagnostics.append(f"seed_change_unit_limit:{change_unit_id}")
            continue
        reserved: list[CandidateSeed] = []
        seen_clusters: set[int] = set()
        for seed in unit_seeds:
            cluster = _seed_cluster(seed, task)
            if cluster is None or cluster in seen_clusters:
                continue
            seen_clusters.add(cluster)
            reserved.append(seed)
            if len(reserved) >= max_seeds_per_change_unit:
                break
        for seed in (*reserved, *unit_seeds):
            if seed.seed_id in selected_ids:
                continue
            if sum(item.change_unit_id == change_unit_id for item in selected) >= max_seeds_per_change_unit:
                diagnostics.append(f"seed_change_unit_limit:{change_unit_id}")
                break
            selected.append(seed)
            selected_ids.add(seed.seed_id)
    if len(selected) > max_seeds_per_reviewer:
        selected = selected[:max_seeds_per_reviewer]
        diagnostics.append("seed_reviewer_limit")
    return selected, tuple(dict.fromkeys(diagnostics))


def _enclosing_method(
    symbol_context: TaskSymbolContext | None,
    file: str,
    line: int,
) -> str:
    if symbol_context is None:
        return ""
    matching = [
        symbol
        for symbol in symbol_context.symbols
        if symbol.file.replace("\\", "/").lower() == file.replace("\\", "/").lower()
        and symbol.kind.upper() in {"METHOD", "FUNCTION"}
        and symbol.start_line <= line <= symbol.end_line
    ]
    if not matching:
        return ""
    return min(
        matching,
        key=lambda symbol: (symbol.end_line - symbol.start_line, symbol.symbol_id),
    ).symbol_id


def _recover_embedded_candidates(
    result: DirectTriageResult,
    *,
    reviewer: ReviewerKind,
    task: ReviewTask,
) -> tuple[DirectTriageResult, int]:
    """Recover schema-valid candidates misplaced under coverage metadata.

    This is a protocol compatibility repair, not a detector: it does not
    inspect the patch or infer a bug.  Each recovered object is still passed
    through the normal reviewer, location, routing, graph and evidence gates.
    """

    if not any(declaration.issues for declaration in result.coverage):
        return result, 0
    allowed_fields = set(CandidateSeed.model_fields)
    existing_fingerprints = {
        (
            seed.claim.strip(),
            seed.location_file.replace("\\", "/").lower(),
            seed.location_line,
        )
        for seed in result.issues
    }
    recovered: list[CandidateSeed] = []
    for declaration in result.coverage:
        for embedded in declaration.issues:
            if isinstance(embedded, CandidateSeed):
                payload = embedded.model_dump()
            elif isinstance(embedded, dict):
                payload = {
                    key: value for key, value in embedded.items() if key in allowed_fields
                }
            else:
                continue
            payload.setdefault("reviewer", reviewer.value)
            payload.setdefault("change_unit_id", declaration.change_unit_id)
            payload.setdefault("location_file", task.file)
            if "proof_scope" not in payload:
                payload["proof_scope"] = (
                    ProofScope.CROSS_FILE.value
                    if payload.get("graph_question") is not None
                    else ProofScope.LOCAL.value
                )
            if "evidence_need" not in payload:
                payload["evidence_need"] = EvidenceNeed.NONE.value
            try:
                candidate = CandidateSeed.model_validate(payload)
            except Exception:  # noqa: BLE001 - malformed metadata is ignored
                continue
            fingerprint = (
                candidate.claim.strip(),
                candidate.location_file.replace("\\", "/").lower(),
                candidate.location_line,
            )
            if fingerprint in existing_fingerprints:
                continue
            existing_fingerprints.add(fingerprint)
            recovered.append(candidate)
    if not recovered:
        return result, 0
    return result.model_copy(update={"issues": (*result.issues, *recovered)}), len(recovered)


def _normalize_coverage(
    declarations: tuple[CoverageDeclaration, ...],
    *,
    expected_ids: tuple[str, ...],
    task: ReviewTask,
) -> tuple[CoverageDeclaration, ...] | None:
    """Repair duplicate/unknown coverage rows without inventing candidates."""

    known = set(expected_ids)
    first: dict[str, CoverageDeclaration] = {}
    for declaration in declarations:
        if declaration.change_unit_id in known and declaration.change_unit_id not in first:
            first[declaration.change_unit_id] = declaration
    if any(item not in first for item in expected_ids):
        decision = CoverageDecision.LOCAL_ONLY
        reason = "模型覆盖声明不完整，按最小局部覆盖补齐"
        for item in expected_ids:
            first.setdefault(
                item,
                CoverageDeclaration(change_unit_id=item, decision=decision, reason=reason),
            )
    return tuple(first[item] for item in expected_ids)


def _merge_recheck_coverage(
    current: tuple[CoverageDeclaration, ...],
    updated: tuple[CoverageDeclaration, ...],
    *,
    expected_ids: tuple[str, ...],
    task: ReviewTask,
) -> tuple[CoverageDeclaration, ...]:
    """Merge a recheck envelope without allowing it to downgrade coverage.

    Rechecks are protocol repairs, not a second routing decision.  A provider
    may omit the envelope again or return a weaker ``local_only`` value after
    the first pass already established ``graph_needed``.  Keep the stronger
    existing declaration in that case; accept a complete recheck row when it
    explicitly upgrades the unit or supplies a previously missing row.
    """

    if not updated:
        return current
    normalized = _normalize_coverage(updated, expected_ids=expected_ids, task=task)
    if normalized is None:
        return current
    rank = {
        CoverageDecision.NOT_APPLICABLE: 0,
        CoverageDecision.LOCAL_ONLY: 1,
        CoverageDecision.GRAPH_NEEDED: 2,
    }
    current_by_id = {row.change_unit_id: row for row in current}
    merged: list[CoverageDeclaration] = []
    for row in normalized:
        previous = current_by_id.get(row.change_unit_id)
        if previous is not None and rank[previous.decision] > rank[row.decision]:
            merged.append(previous)
        else:
            merged.append(row)
    return tuple(merged)


def _merge_protocol_repair_issues(
    original: tuple[CandidateSeed, ...],
    repaired: tuple[CandidateSeed, ...],
) -> tuple[CandidateSeed, ...]:
    """Apply wording/provenance repairs while preserving omitted candidates.

    A repair prompt is allowed to change protocol-facing prose, but it is not
    allowed to become a new sampling pass.  Some providers return only the
    first repaired seed; replacing the original tuple in that situation would
    silently erase independent changed-line candidates.  Match by exact task
    location first (then stable ordinal), update only non-empty explanatory
    fields, and retain all unmatched originals.
    """

    if not repaired:
        return original

    def canonical_file(value: str) -> str:
        return value.replace("\\", "/").strip().lower()

    merged = list(original)
    used: set[int] = set()
    for ordinal, replacement in enumerate(repaired):
        replacement_file = canonical_file(replacement.location_file)
        match_index = next(
            (
                index
                for index, candidate in enumerate(merged)
                if index not in used
                and canonical_file(candidate.location_file) == replacement_file
                and candidate.location_line > 0
                and replacement.location_line > 0
                and candidate.location_line == replacement.location_line
            ),
            None,
        )
        if match_index is None and ordinal < len(merged) and ordinal not in used:
            # The repair contract asks the model to preserve ordering.  This
            # fallback covers a provider that changes a valid line to 0 while
            # keeping the same candidate ordinal; it never matches across
            # files.
            candidate = merged[ordinal]
            if canonical_file(candidate.location_file) == replacement_file:
                match_index = ordinal
        if match_index is None:
            merged.append(replacement)
            used.add(len(merged) - 1)
            continue
        baseline = merged[match_index]
        updates: dict[str, Any] = {}
        for field in ("claim", "mechanism", "impact", "suggestion", "issue_type"):
            value = getattr(replacement, field)
            if isinstance(value, str) and value.strip():
                updates[field] = value
        if replacement.graph_question is not None:
            updates["graph_question"] = replacement.graph_question
        merged[match_index] = baseline.model_copy(update=updates)
        used.add(match_index)
    return tuple(merged)


def _normalize_graph_question(
    seed: CandidateSeed,
    symbol_context: TaskSymbolContext | None,
    *,
    task: ReviewTask,
    reviewer: ReviewerKind | None = None,
) -> CandidateSeed:
    """Keep GraphQuestion targets/properties in the deterministic proof domain.

    Triage often knows the *concept* of a listener or cache but not its full
    stable symbol id.  Retaining guessed aliases makes a later proof look
    negative even when Gateway returned the relevant path.  Exact ids already
    present in ``symbol_context`` are retained; unresolved natural-language
    target names are left for the assessment model to interpret from the
    EvidencePack.  At least one canonical relationship is kept so the graph
    question remains executable.
    """

    question = seed.graph_question
    if question is None:
        return seed
    # Keep whether the provider explicitly supplied a path domain before any
    # compatibility default is applied below.  An explicit behavior/security
    # path is stronger than a contradictory ``evidence_need=inspect_structure``
    # label: structure is a one-hop hint, while the GraphQuestion carries the
    # executable direction and path contract.
    provided_path_kind = question.path_kind
    allowed_symbols = {
        symbol.symbol_id
        for symbol in (symbol_context.symbols if symbol_context is not None else ())
        if symbol.symbol_id
    }
    subject_ref = question.subject_ref
    if subject_ref not in allowed_symbols:
        # Providers sometimes serialize a source location (``file:line``)
        # instead of the stable symbol id required by GraphPlan.  Resolve
        # only an exact task-file line to its enclosing resolved method; do
        # not fuzzy-match arbitrary names or files.
        prefix = f"{task.file}:"
        if subject_ref.startswith(prefix):
            try:
                line = int(subject_ref[len(prefix):])
            except ValueError:
                line = 0
            resolved = _enclosing_method(symbol_context, task.file, line)
            if resolved:
                subject_ref = resolved
        if subject_ref not in allowed_symbols:
            # OpenAI-compatible providers sometimes drop a method signature
            # while preserving the stable owner/member prefix (for example
            # ``java:pkg.Type#run`` instead of ``java:pkg.Type#run(...)``).
            # Resolve that alias only when it identifies one symbol in the
            # already-resolved task context.  Ambiguous overloads stay
            # unresolved; guessing among them would make the evidence query
            # less trustworthy than rejecting it.
            subject_ref = _resolve_unambiguous_symbol_alias(
                subject_ref,
                allowed_symbols,
                symbol_context=symbol_context,
                location_line=seed.location_line,
            )
        if subject_ref not in allowed_symbols:
            # Last, still deterministic repair: when a provider names a
            # callee/consumer instead of the changed enclosing method, use the
            # unique resolved method containing this candidate's changed line.
            # This is local source-to-symbol resolution, not fuzzy graph
            # search.  If more than one method overlaps the line (or the
            # location is unresolved), leave the value untouched and let
            # GraphPlan fail closed.
            resolved = _unique_changed_enclosing_symbol(
                symbol_context,
                task=task,
                location_line=seed.location_line,
            )
            if resolved:
                subject_ref = resolved
    expected_targets_list: list[str] = []
    for target in question.expected_targets:
        target_text = str(target).strip()
        if not target_text:
            continue
        resolved = (
            target_text
            if target_text in allowed_symbols
            else _resolve_unambiguous_symbol_alias(
                target_text,
                allowed_symbols,
                symbol_context=symbol_context,
                location_line=seed.location_line,
            )
        )
        if resolved in allowed_symbols:
            expected_targets_list.append(resolved)
        else:
            # GraphQuestion also permits a precise class/method alias.  Keep
            # an unresolved *symbol-shaped* alias instead of silently
            # deleting the target: ``match_graph_proof`` may resolve it
            # against the *visible* projected endpoint after the graph query.
            # Providers occasionally put a whole natural-language assertion
            # in ``expected_targets`` (for example, “the early-return branch
            # has no downstream call”).  That is not a target and requiring
            # it to match makes an otherwise positive partial graph
            # indeterminate.  Drop only prose-shaped values; one-token role
            # aliases (``listener``/``callback``) and Java-like names remain
            # eligible for proof-time resolution.
            if _looks_like_symbol_alias(target_text):
                expected_targets_list.append(target_text)
    expected_targets = tuple(dict.fromkeys(expected_targets_list))
    required_relationships = tuple(
        kind
        for kind in question.required_relationships
        if str(kind).upper() in _GRAPH_RELATIONSHIP_KINDS
    )
    if not required_relationships:
        required_relationships = ("CALLS",)
    path_kind = question.path_kind
    direction = question.direction
    # The provider emits two independently useful fields (``evidence_need``
    # and ``graph_question.direction``).  Compatible endpoints occasionally
    # contradict themselves: for example, they choose
    # ``inspect_change_impact`` while the question explicitly asks for a
    # downstream callback path.  Let the executable question text/path shape
    # arbitrate that contradiction, then use the typed evidence need only as a
    # compatibility fallback.  This is a protocol repair, not a domain rule:
    # it prevents a malformed route from silently querying the opposite side
    # of the graph and dropping an otherwise usable candidate.
    direction_hint = _direction_hint(question.question)
    if path_kind is not None:
        direction = "downstream"
    elif direction_hint is not None:
        direction = direction_hint
    elif seed.evidence_need is EvidenceNeed.INSPECT_PATH:
        direction = "downstream"
    elif seed.evidence_need is EvidenceNeed.INSPECT_CHANGE_IMPACT:
        direction = "upstream"

    # Keep the two routing fields coherent after the direction is resolved.
    # ``inspect_structure`` is deliberately left untouched because it is a
    # one-hop fact and may legitimately use either orientation.  Path and
    # impact are the only tools whose contracts encode opposite directions.
    if seed.evidence_need in {
        EvidenceNeed.INSPECT_PATH,
        EvidenceNeed.INSPECT_CHANGE_IMPACT,
    }:
        canonical_need = (
            EvidenceNeed.INSPECT_PATH
            if direction == "downstream"
            else EvidenceNeed.INSPECT_CHANGE_IMPACT
        )
    else:
        canonical_need = seed.evidence_need
    if (
        provided_path_kind in {"behavior", "security"}
        and seed.evidence_need is EvidenceNeed.INSPECT_STRUCTURE
        and direction == "downstream"
    ):
        # Some providers emit a generic structure label while simultaneously
        # describing a multi-hop path.  Route the explicit executable question
        # to inspect_path; do not let the weaker label silently shorten the
        # requested proof to one hop.
        canonical_need = EvidenceNeed.INSPECT_PATH
    if direction == "upstream":
        path_kind = None
    if direction == "downstream" and path_kind is None:
        # A blank optional path kind is a provider formatting artifact.  Use
        # the fixed reviewer's bounded traversal domain; the reviewer type is
        # not a semantic finding and no target is invented here.
        path_kind = (
            "security"
            if reviewer is ReviewerKind.THREAT_MODEL
            else "behavior"
        )
    elif (
        direction == "downstream"
        and path_kind == "security"
        and seed.proof_scope is not ProofScope.SECURITY_PROPAGATION
    ):
        # Gateway security traversal intentionally suppresses ordinary
        # unresolved call edges.  Only an explicitly security-propagation
        # scoped candidate may request that restricted view; a generic
        # cross-file/state/ordering candidate must use the behavior graph or
        # its evidence would look empty even when CALLS edges exist.
        path_kind = "behavior"
    question_text = question.question.strip() or seed.claim.strip()
    normalized = question.model_copy(
        update={
            "subject_ref": subject_ref,
            "expected_targets": expected_targets,
            "required_relationships": required_relationships,
            "direction": direction,
            "path_kind": path_kind,
            "question": question_text,
        }
    )
    return seed.model_copy(
        update={
            "graph_question": normalized,
            "evidence_need": canonical_need,
        }
    )


def _normalize_state_timing_question(
    seed: CandidateSeed,
    *,
    task: ReviewTask,
) -> tuple[CandidateSeed, str]:
    """Ensure state-registration moves ask about downstream observation order.

    A provider may notice only the local branch (for example, a guard that now
    runs before registration) and formulate a question about that branch's
    exceptional outcome.  For any candidate whose own text and diff clearly
    describe a registration/context move, retain the provider question but add
    the missing executable seam: calls made before registration and consumers
    that may observe the state.  No symbol is named and no conclusion is
    inferred; the existing subject/path/relationship contract remains intact.
    """

    question = seed.graph_question
    if question is None or question.direction != "downstream":
        return seed, ""
    text = " ".join(
        value
        for value in (seed.claim, seed.mechanism, seed.impact, question.question)
        if value
    ).lower()
    patch = task.patch.lower()
    registration_markers = (
        "register(",
        "registration",
        "registered",
        "注册",
        "synchronization",
        "同步上下文",
        "retrycontext",
        "context",
        "上下文",
    )
    timing_markers = (
        "before",
        "after",
        "之前",
        "之后",
        "移动",
        "时序",
        "顺序",
        "running",
        "分支",
        "exception",
        "异常",
    )
    if not any(marker in text for marker in registration_markers):
        return seed, ""
    if not any(marker in patch for marker in ("register(", "register (", "注册")):
        return seed, ""
    if not any(marker in text for marker in timing_markers):
        return seed, ""
    addition = (
        "同时核验该 subject 在注册/发布该 context 之前是否先调用了下游回调、"
        "监听器或状态消费者，以及这些观察点读取到的上下文时序；只依据图谱中返回的"
        "实际关系，不把未解析的观察者当作存在。"
    )
    existing = question.question.strip()
    if addition in existing:
        return seed, ""
    normalized = question.model_copy(
        update={"question": f"{existing}；{addition}" if existing else addition}
    )
    return seed.model_copy(update={"graph_question": normalized}), "state_timing_question_enriched"


def _direction_hint(question: str) -> str | None:
    """Extract an explicit graph direction from the provider's question.

    The hint is intentionally narrow and only resolves a contradiction in the
    typed route.  It does not discover symbols, infer a defect, or inspect
    source.  If both sides are mentioned (a genuinely mixed question), the
    structured fields remain authoritative and the caller's existing
    compatibility rules apply.
    """

    text = question.strip().lower()
    if not text:
        return None
    downstream_markers = (
        "下游",
        "监听器",
        "listener",
        "回调",
        "callback",
        "消费者",
        "consumer",
        "downstream",
        "执行路径",
        "调用路径",
        "传播",
    )
    upstream_markers = (
        "上游",
        "调用方",
        "入口",
        "caller",
        "upstream",
        "影响面",
        "反向",
    )
    has_downstream = any(marker in text for marker in downstream_markers)
    has_upstream = any(marker in text for marker in upstream_markers)
    if has_downstream == has_upstream:
        return None
    return "downstream" if has_downstream else "upstream"


def _looks_like_symbol_alias(value: str) -> bool:
    """Whether a provider target has a symbol/name shape rather than prose."""

    text = value.strip()
    if not text or any("\u4e00" <= char <= "\u9fff" for char in text):
        return False
    # A fully-qualified id/signature may contain punctuation and generic
    # spaces; a natural-language sentence contains multiple ordinary words.
    if " " in text and not any(marker in text for marker in ("#", ".", "::")):
        return False
    return bool(re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$.:#<>(),\[\]?*+\-/ ]*", text))


def _resolve_unambiguous_symbol_alias(
    value: str,
    allowed_symbols: set[str],
    *,
    symbol_context: TaskSymbolContext | None,
    location_line: int,
) -> str:
    """Resolve a provider's signature-less symbol alias conservatively.

    This is a protocol repair, not semantic symbol search: candidates must
    already be present in ``TaskSymbolContext`` and a member alias must map to
    exactly one overload.  If overloads remain, a changed line may disambiguate
    only when it encloses exactly one of them; otherwise the original value is
    returned and GraphPlan will fail closed.
    """

    candidate = value.strip()
    if not candidate or candidate in allowed_symbols:
        return candidate
    if "#" in candidate:
        owner, member = candidate.split("#", 1)
        member_name = member.split("(", 1)[0].strip()
        if not owner or not member_name:
            return candidate
        matches = [
            symbol_id
            for symbol_id in allowed_symbols
            if symbol_id.startswith(f"{owner}#")
            and symbol_id.split("#", 1)[1].split("(", 1)[0].strip() == member_name
        ]
    else:
        # Providers often emit only ``methodName`` or ``methodName(...)``
        # after reading the symbol context.  Resolve that short alias only
        # when it denotes one already-resolved method; overloaded names use
        # the changed line as a deterministic tie-breaker below.
        wanted = candidate.split("(", 1)[0].strip()
        # A prose-oriented provider may serialize a call chain as the
        # subject (``A.open -> doOpenInternal``) or qualify a callee without
        # a stable owner (``RetrySynchronizationManager.register``).  Only
        # use the final identifier as an alias; the enclosing-line and
        # uniqueness checks below still prevent arbitrary fuzzy matching.
        if "->" in wanted:
            wanted = wanted.rsplit("->", 1)[-1].strip()
        if "." in wanted:
            wanted = wanted.rsplit(".", 1)[-1].strip()
        matches = []
        for symbol_id in allowed_symbols:
            qualified = symbol_id.split(":", 1)[-1]
            _owner, separator, member = qualified.partition("#")
            member_name = member.split("(", 1)[0].strip() if separator else ""
            class_name = _owner.rsplit(".", 1)[-1]
            if wanted and (member_name == wanted or class_name == wanted):
                matches.append(symbol_id)
    if len(matches) == 1:
        return matches[0]
    if len(matches) <= 1 or symbol_context is None or location_line <= 0:
        return candidate
    enclosed = [
        symbol.symbol_id
        for symbol in symbol_context.symbols
        if symbol.symbol_id in matches
        and symbol.start_line <= location_line <= symbol.end_line
    ]
    return enclosed[0] if len(enclosed) == 1 else candidate


def _unique_changed_enclosing_symbol(
    symbol_context: TaskSymbolContext | None,
    *,
    task: ReviewTask,
    location_line: int,
) -> str:
    """Resolve a candidate to one enclosing changed method, if unambiguous."""

    if symbol_context is None:
        return ""
    # Prefer the candidate's own changed line.  Using every task line first
    # makes a file-level task with two methods appear ambiguous even when the
    # candidate has a precise location; that ambiguity then isolates an
    # otherwise executable graph question.  Fall back to all changed lines
    # only when the candidate location is unresolved.
    lines = (
        {location_line}
        if location_line > 0
        else {line for line in task.changed_lines if line > 0}
    )
    if not lines:
        return ""
    matches = [
        symbol.symbol_id
        for symbol in symbol_context.symbols
        if (
            symbol.kind.upper() in {"METHOD", "FUNCTION"}
            and symbol.file.replace("\\", "/").lower() == task.file.replace("\\", "/").lower()
            and any(symbol.start_line <= line <= symbol.end_line for line in lines)
        )
    ]
    if len(matches) == 1:
        return matches[0]
    if location_line > 0:
        fallback_lines = {line for line in task.changed_lines if line > 0}
        fallback = [
            symbol.symbol_id
            for symbol in symbol_context.symbols
            if (
                symbol.kind.upper() in {"METHOD", "FUNCTION"}
                and symbol.file.replace("\\", "/").lower()
                == task.file.replace("\\", "/").lower()
                and any(
                    symbol.start_line <= line <= symbol.end_line
                    for line in fallback_lines
                )
            )
        ]
        return fallback[0] if len(fallback) == 1 else ""
    return ""


def _normalize_seed_location(
    seed: CandidateSeed, task: ReviewTask
) -> tuple[CandidateSeed | None, tuple[str, ...]]:
    """把 DirectTriage 的位置限制在当前 task 可证明的行。"""

    def canonical(path: str) -> str:
        return path.replace("\\", "/").strip().lower()

    if canonical(seed.location_file) != canonical(task.file):
        return None, ("location_file_mismatch",)
    if seed.location_line <= 0:
        return seed, ()
    valid_lines = set(task.changed_lines)
    valid_lines.update(anchor.anchor_line for anchor in task.deletion_anchors)
    if seed.location_line in valid_lines:
        return seed, ()
    return (
        seed.model_copy(update={"location_line": 0}),
        ("candidate_location_unresolved",),
    )


__all__ = ["build_triage_user_prompt", "prompt_hash", "run_direct_triage"]
