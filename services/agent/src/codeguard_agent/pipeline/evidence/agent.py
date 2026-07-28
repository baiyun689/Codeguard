"""策略约束下的候选级证据收集与关系分析。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, cast

from pydantic import BaseModel

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.council import EvidenceFinding, EvidenceNote, EvidenceRequest
from codeguard_agent.pipeline.context import rules as context_rules
from codeguard_agent.pipeline.concurrency import run_bounded_parallel
from codeguard_agent.pipeline.engines import GatheredContext
from codeguard_agent.pipeline.evidence.planner import CandidateDossier
from codeguard_agent.pipeline.evidence.rules import STRATEGIES_BY_ID
from codeguard_agent.pipeline.evidence.rules.types import ToolCallSpec, as_capability

logger = logging.getLogger("codeguard")
_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"


@dataclass
class EvidenceBatch:
    """一次收集产生的 note、trace 与实际新工具调用。"""

    notes: list[EvidenceNote] = field(default_factory=list)
    trace: list[tuple[str, str]] = field(default_factory=list)
    gathered_context: list[GatheredContext] = field(default_factory=list)


@dataclass(frozen=True)
class BoundEvidence:
    """A finding whose note and registered request are bound to one dossier."""

    request: EvidenceRequest
    finding: EvidenceFinding


@dataclass(frozen=True)
class _RawFact:
    evidence_id: str
    source: str
    raw: str
    limitation: str = ""
    prior_finding: EvidenceFinding | None = None


@dataclass(frozen=True)
class _ToolUse:
    call: ToolCallSpec
    key: tuple[str, str]
    canonical_args: str
    first_use: bool


@dataclass
class _RequestWork:
    request: EvidenceRequest
    dossier: CandidateDossier | None
    facts: list[_RawFact] = field(default_factory=list)
    tool_uses: list[_ToolUse] = field(default_factory=list)
    tool_trace: list[tuple[str, str]] = field(default_factory=list)
    ready_note: EvidenceNote | None = None


class _EvidenceAnalysis(BaseModel):
    evidence_id: str
    relation: Literal["supports", "contradicts", "insufficient"]
    strength: Literal["direct", "contextual"]
    observation: str = ""
    limitation: str = ""


class _EvidenceAnalysisBatch(BaseModel):
    findings: list[_EvidenceAnalysis]


@dataclass(frozen=True)
class _RequestAnalysis:
    findings: tuple[EvidenceFinding, ...]
    trace: tuple[tuple[str, str], ...] = ()
    llm_called: bool = False


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(*parts: str) -> str:
    payload = "\0".join(parts)
    return f"evidence-{sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def _insufficient(request: EvidenceRequest, limitation: str, *, detail: str = "") -> EvidenceNote:
    return EvidenceNote(
        request_id=request.id,
        candidate_id=request.candidate_id,
        findings=[
            EvidenceFinding(
                evidence_id=_digest(request.id, limitation, detail),
                source="request_validation",
                observation=detail,
                relation="insufficient",
                strength="contextual",
                limitation=limitation,
            )
        ],
    )


def _expected_tools(calls: list[ToolCallSpec]) -> list[str]:
    return list(dict.fromkeys(call.tool_name for call in calls))


def request_strategy_mismatch(
    request: EvidenceRequest,
    dossier: CandidateDossier | None,
) -> str | None:
    if dossier is None:
        return "missing_dossier"
    if request.candidate_id != dossier.candidate.id:
        return "candidate_id"
    strategy = STRATEGIES_BY_ID.get(request.strategy_id)
    if strategy is None:
        return "strategy_id"
    if request.purpose != strategy.purpose:
        return "purpose"
    target = context_rules.normalize_path(request.target)
    if target != context_rules.normalize_path(dossier.task.file):
        return "target"
    if not request.question.strip() or request.question != strategy.question_template:
        return "question"
    calls = strategy.build_tool_calls(dossier)
    accepted_preferred_tools = [
        _expected_tools(calls),
        list(strategy.allowed_tools),
    ]
    if request.preferred_tools not in accepted_preferred_tools:
        return "preferred_tools"
    if any(
        as_capability(call.capability) not in strategy.allowed_capabilities
        for call in calls
    ):
        return "tool_allowlist"
    return None


def bound_evidence(dossier: CandidateDossier) -> list[BoundEvidence]:
    """Return only findings with a valid request and same-candidate note binding."""
    valid_requests = {
        request.id: request
        for request in dossier.requests
        if request_strategy_mismatch(request, dossier) is None
    }
    return [
        BoundEvidence(valid_requests[note.request_id], finding)
        for note in dossier.notes
        if note.candidate_id == dossier.candidate.id
        and note.request_id in valid_requests
        for finding in note.findings
    ]


def _symbol_line(payload: dict[str, Any], field_name: str) -> int:
    try:
        return int(payload.get(field_name, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _selected_symbol_contexts(dossier: CandidateDossier) -> dict[int, str]:
    """选择候选局部符号；值为空或明确的保守分析限制。"""
    bundle = dossier.context_bundle
    if bundle is None:
        return {}
    parsed: list[tuple[int, dict[str, Any]]] = []
    malformed: list[int] = []
    task_file = context_rules.normalize_path(dossier.task.file)
    for fact in bundle.facts:
        if fact.kind != "symbol_context":
            continue
        try:
            payload = json.loads(fact.content)
            if not isinstance(payload, dict):
                malformed.append(id(fact))
                continue
            fact_file = context_rules.normalize_path(str(payload.get("file", "")))
            if fact_file and fact_file != task_file:
                continue
            parsed.append((id(fact), payload))
        except (TypeError, ValueError, json.JSONDecodeError):
            malformed.append(id(fact))

    line = dossier.candidate.line
    covering = [
        (fact_id, payload)
        for fact_id, payload in parsed
        if _symbol_line(payload, "start_line")
        <= line
        <= _symbol_line(payload, "end_line")
    ]
    local = [
        (fact_id, payload)
        for fact_id, payload in covering
        if str(payload.get("kind", "")).lower()
        in {"method", "constructor", "field"}
    ]
    selected: dict[int, str] = {}
    owner_ids: set[str] = set()
    if local:
        minimum_span = min(
            _symbol_line(payload, "end_line") - _symbol_line(payload, "start_line")
            for _, payload in local
        )
        for fact_id, payload in local:
            span = _symbol_line(payload, "end_line") - _symbol_line(
                payload, "start_line"
            )
            if span != minimum_span:
                continue
            selected[fact_id] = ""
            owner = str(payload.get("owner_type") or "")
            if owner:
                owner_ids.add(owner)
    elif covering:
        minimum_span = min(
            _symbol_line(payload, "end_line") - _symbol_line(payload, "start_line")
            for _, payload in covering
        )
        for fact_id, payload in covering:
            if (
                _symbol_line(payload, "end_line")
                - _symbol_line(payload, "start_line")
                == minimum_span
            ):
                selected[fact_id] = ""
    elif parsed:
        file_fallbacks = [
            (fact_id, payload)
            for fact_id, payload in parsed
            if str(payload.get("kind", "")).lower() == "type"
        ] or parsed
        fallback_id, _ = min(
            file_fallbacks,
            key=lambda item: (
                _symbol_line(item[1], "end_line")
                - _symbol_line(item[1], "start_line"),
                str(item[1].get("symbol_id") or ""),
            ),
        )
        selected[fallback_id] = "symbol_context_location_unresolved"
    elif malformed:
        selected[malformed[0]] = "invalid_symbol_context"

    if owner_ids:
        for fact_id, payload in parsed:
            if str(payload.get("symbol_id") or "") in owner_ids:
                selected[fact_id] = ""
    return selected


def _base_facts(dossier: CandidateDossier, request: EvidenceRequest) -> list[_RawFact]:
    facts = [
        _RawFact(
            evidence_id=_digest(dossier.task.id, dossier.task.patch),
            source="task_patch",
            raw=dossier.task.patch,
        )
    ]
    strategy = STRATEGIES_BY_ID[request.strategy_id]
    bundle = dossier.context_bundle
    if bundle is not None:
        selected_symbol_contexts = _selected_symbol_contexts(dossier)
        for fact in bundle.facts:
            if fact.kind not in strategy.context_kinds and fact.kind != "symbol_context":
                continue
            if (
                fact.kind == "symbol_context"
                and id(fact) not in selected_symbol_contexts
            ):
                continue
            truncated = bundle.truncated or fact.truncated
            selection_limitation = selected_symbol_contexts.get(id(fact), "")
            facts.append(
                _RawFact(
                    evidence_id=_digest(fact.source, fact.kind, fact.content),
                    source=f"context:{fact.kind}",
                    raw=fact.content,
                    limitation=(
                        "context_truncated" if truncated else selection_limitation
                    ),
                )
            )
    planned_tools = {
        call.tool_name for call in strategy.build_tool_calls(dossier)
    }
    for note in dossier.notes:
        for finding in note.findings:
            is_relevant_tool = any(
                tool_name in finding.source for tool_name in planned_tools
            )
            reusable_for_severity = (
                request.purpose == "severity" and bool(finding.observation.strip())
            )
            if not is_relevant_tool and not reusable_for_severity:
                continue
            facts.append(
                _RawFact(
                    evidence_id=finding.evidence_id,
                    source=f"prior:{finding.source}",
                    raw=finding.observation,
                    limitation=finding.limitation,
                    prior_finding=finding,
                )
            )
    return facts


def _has_fact_for_tool(
    tool_name: str,
    facts: list[_RawFact],
    dossier: CandidateDossier,
) -> bool:
    source_markers = {
        "inspect_security_path": ("inspect_security_path",),
        "inspect_change_impact": ("inspect_change_impact",),
        "inspect_structure": ("inspect_structure",),
        "find_sensitive_apis": ("sensitive_api", "find_sensitive_apis"),
        "find_callers": ("find_callers",),
        "get_code_metrics": ("get_code_metrics",),
    }
    if tool_name == "get_file_content":
        if (
            dossier.task.patch_complete
            and dossier.task.hunk_header.strip().startswith("@@ -0,0 +")
        ):
            return True
        return any("get_file_content" in fact.source for fact in facts)
    markers = source_markers.get(tool_name, ())
    if any(any(marker in fact.source for marker in markers) for fact in facts):
        return True
    return any(
        any(any(marker in finding.source for marker in markers) for finding in note.findings)
        for note in dossier.notes
    )


def _call_tool(tool_client: Any, call: ToolCallSpec) -> tuple[str, str, float]:
    kwargs = dict(call.arguments)
    started = perf_counter()
    try:
        response = getattr(tool_client, call.tool_name)(**kwargs)
    except Exception as exc:  # noqa: BLE001 - 单次工具异常收敛为不足证据
        return "", f"tool_error:{exc}", (perf_counter() - started) * 1000
    duration_ms = (perf_counter() - started) * 1000
    success = bool(getattr(response, "success", True))
    raw = getattr(response, "result", None)
    if raw is None and hasattr(response, "as_tool_output"):
        raw = response.as_tool_output()
    text = str(raw or "")
    if not success:
        return text, "tool_failed", duration_ms
    if not text.strip():
        return "", "tool_empty", duration_ms
    if call.tool_name.startswith("inspect_"):
        try:
            payload = json.loads(text)
            expected_subject = kwargs.get("symbol_id", "")
            actual_subject = str(payload.get("subject_symbol_id", ""))
            if expected_subject and actual_subject and actual_subject != expected_subject:
                return text, "graph_subject_mismatch", duration_ms
            status = payload.get("status")
            coverage = payload.get("coverage")
            if status == "unknown" or coverage == "partial":
                return text, "graph_unknown", duration_ms
            if status not in {"confirmed", "not_found"}:
                return text, "invalid_graph_status", duration_ms
        except (TypeError, ValueError, json.JSONDecodeError):
            return text, "invalid_graph_response", duration_ms
    return text, "", duration_ms


def _strip_comments_and_strings(source: str) -> str:
    """移除 Java 注释/字符串内容并保持字符位置和换行。"""
    result = list(source)
    index = 0
    state = "code"
    while index < len(source):
        char = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""
        if state == "code" and char == "/" and nxt == "/":
            result[index] = result[index + 1] = " "
            state = "line_comment"
            index += 2
            continue
        if state == "code" and char == "/" and nxt == "*":
            result[index] = result[index + 1] = " "
            state = "block_comment"
            index += 2
            continue
        if state == "code" and char in {'"', "'"}:
            result[index] = " "
            state = "string" if char == '"' else "char"
            index += 1
            continue
        if state == "line_comment":
            if char == "\n":
                state = "code"
            else:
                result[index] = " "
            index += 1
            continue
        if state == "block_comment":
            if char == "*" and nxt == "/":
                result[index] = result[index + 1] = " "
                state = "code"
                index += 2
            else:
                if char != "\n":
                    result[index] = " "
                index += 1
            continue
        if state in {"string", "char"}:
            quote = '"' if state == "string" else "'"
            if char == "\\" and nxt:
                result[index] = " "
                if nxt != "\n":
                    result[index + 1] = " "
                index += 2
            elif char == quote:
                result[index] = " "
                state = "code"
                index += 1
            else:
                if char != "\n":
                    result[index] = " "
                index += 1
            continue
        index += 1
    return "".join(result)


_METHOD_RANGE = re.compile(r"\b(\w+)\([^)]*\).*\[L(\d+)-L(\d+)\]\s*$")


def _resolved_method(dossier: CandidateDossier) -> tuple[str, int, int, str] | None:
    bundle = dossier.context_bundle
    if bundle is None:
        return None
    for context_fact in bundle.facts:
        if context_fact.kind == "symbol_context" and not context_fact.truncated:
            try:
                payload = json.loads(context_fact.content)
                if payload.get("kind") not in {"method", "constructor"}:
                    continue
                symbol_id = str(payload.get("symbol_id", ""))
                method_name = symbol_id.rsplit("#", 1)[-1].split("(", 1)[0]
                return (
                    method_name,
                    int(payload.get("start_line", 0)),
                    int(payload.get("end_line", 0)),
                    " ".join(
                        [
                            *(
                                f"@{name}"
                                for name in payload.get("annotations", [])
                            ),
                            str(payload.get("signature", "")),
                        ]
                    ).strip(),
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        if context_fact.kind != "ast_structure" or context_fact.truncated:
            continue
        legacy_method_name = context_rules.resolve_method_name(
            context_fact.content, dossier.task
        )
        if legacy_method_name is None:
            continue
        task_span = context_rules._task_span(dossier.task)
        if task_span is None:
            return None
        for line in context_fact.content.splitlines():
            match = _METHOD_RANGE.search(line.strip())
            if not match or match.group(1) != legacy_method_name:
                continue
            start, end = int(match.group(2)), int(match.group(3))
            if start <= task_span[1] and end >= task_span[0]:
                return legacy_method_name, start, end, line.strip()
    return None


def _matching_brace(source: str, open_index: int) -> int | None:
    depth = 0
    for index in range(open_index, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _scoped_annotation(
    dossier: CandidateDossier,
    source: str,
    annotation_names: tuple[str, ...],
) -> str | None:
    resolved = _resolved_method(dossier)
    if resolved is None:
        return None
    method_name, start, end, ast_signature = resolved
    annotation_pattern = re.compile(
        r"@(" + "|".join(re.escape(name) for name in annotation_names) + r")\b"
    )
    ast_match = annotation_pattern.search(ast_signature)
    if ast_match:
        return f"当前方法 AST 声明含 @{ast_match.group(1)}"

    sanitized = _strip_comments_and_strings(source)
    lines = sanitized.splitlines()
    start_index = max(0, start - 1)
    end_index = min(len(lines), end)
    method_line = next(
        (
            index
            for index in range(start_index, end_index)
            if re.search(rf"\b{re.escape(method_name)}\s*\(", lines[index])
        ),
        None,
    )
    if method_line is None:
        return None
    method_declaration = "\n".join(lines[start_index : method_line + 1])
    method_match = annotation_pattern.search(method_declaration)
    if method_match:
        return f"当前方法声明含 @{method_match.group(1)}"

    line_offsets: list[int] = []
    offset = 0
    for line in sanitized.splitlines(keepends=True):
        line_offsets.append(offset)
        offset += len(line)
    if method_line >= len(line_offsets):
        return None
    method_offset = line_offsets[method_line]
    class_pattern = re.compile(r"\b(?:class|interface|record|enum)\s+\w+[^\{]*\{")
    owner = None
    for match in class_pattern.finditer(sanitized, 0, method_offset + 1):
        open_index = sanitized.find("{", match.start(), match.end())
        close_index = _matching_brace(sanitized, open_index)
        if close_index is not None and open_index < method_offset < close_index:
            owner = match
    if owner is None:
        return None
    class_line = sanitized.count("\n", 0, owner.start())
    declaration_start = class_line
    while declaration_start > 0:
        previous = lines[declaration_start - 1].strip()
        if not previous or previous.startswith("@") or previous.endswith(")"):
            declaration_start -= 1
            continue
        break
    class_declaration = "\n".join(lines[declaration_start : class_line + 1])
    class_match = annotation_pattern.search(class_declaration)
    if class_match:
        return f"当前所属类声明含 @{class_match.group(1)}"
    return None


def _direct_counter_finding(
    dossier: CandidateDossier,
    request: EvidenceRequest,
    fact: _RawFact,
) -> EvidenceFinding | None:
    if request.purpose != "counter":
        return None
    annotations: tuple[str, ...]
    if request.strategy_id.startswith("authorization."):
        annotations = ("PreAuthorize", "PostAuthorize", "Secured", "RolesAllowed")
    elif request.strategy_id.startswith("transaction_atomicity."):
        annotations = ("Transactional",)
    else:
        return None
    observation = _scoped_annotation(dossier, fact.raw, annotations)
    if observation is None:
        return None
    return EvidenceFinding(
        evidence_id=fact.evidence_id,
        source=fact.source,
        observation=observation,
        relation="contradicts",
        strength="direct",
    )


def _analysis_user_prompt(
    dossier: CandidateDossier,
    request: EvidenceRequest,
    facts: list[_RawFact],
) -> str:
    profile = dossier.risk_profile
    risk = {
        "tags": [tag.value for tag, score in (profile.tag_scores.items() if profile else ()) if score > 0],
        "signals": [signal.model_dump(mode="json") for signal in (profile.signals if profile else ())],
    }
    payload = {
        "candidate": {
            "type": dossier.candidate.type,
            "claim": dossier.candidate.claim,
            "severity": dossier.candidate.severity_proposal.value,
        },
        "candidate_group": (
            {
                "group_id": dossier.candidate_group.id,
                "shared_root_cause": dossier.candidate_group.shared_root_cause,
                "shared_behavior": dossier.candidate_group.shared_behavior,
                "shared_fix": dossier.candidate_group.shared_fix,
                "members": [
                    {
                        "candidate_id": member.id,
                        "type": member.type,
                        "claim": member.claim,
                        "suggestion": member.suggestion,
                    }
                    for member in dossier.candidate_group.members
                ],
            }
            if dossier.candidate_group is not None
            else None
        ),
        "purpose": request.purpose,
        "strategy_question": request.question,
        "task_patch": dossier.task.patch,
        "risk_profile": risk,
        "task_context": (
            dossier.context_bundle.model_dump(mode="json")
            if dossier.context_bundle is not None
            else None
        ),
        "facts": [
            {
                "evidence_id": fact.evidence_id,
                "source": fact.source,
                "raw": fact.raw,
                "limitation": fact.limitation,
            }
            for fact in facts
        ],
    }
    return _stable_json(payload)


def _finding_from_analysis(
    request: EvidenceRequest,
    fact: _RawFact,
    result: _EvidenceAnalysis,
) -> EvidenceFinding:
    if result.relation == "insufficient":
        return EvidenceFinding(
            evidence_id=fact.evidence_id,
            source=fact.source,
            observation=result.observation,
            relation="insufficient",
            strength="contextual",
            limitation=result.limitation.strip() or "analyst_insufficient",
        )
    strength = result.strength
    if (
        strength == "direct"
        and request.purpose == "counter"
        and request.strategy_id.startswith(
            ("authorization.", "transaction_atomicity.")
        )
    ):
        strength = "contextual"
    return EvidenceFinding(
        evidence_id=fact.evidence_id,
        source=fact.source,
        observation=result.observation,
        relation=result.relation,
        strength=strength,
        limitation=result.limitation,
    )


def _analyze_request(
    dossier: CandidateDossier,
    request: EvidenceRequest,
    facts: list[_RawFact],
    analyst_llm: Any,
    structured_method: str,
) -> _RequestAnalysis:
    resolved: dict[str, EvidenceFinding] = {}
    analyzable: list[_RawFact] = []
    for fact in facts:
        if fact.prior_finding is not None:
            prior = fact.prior_finding
            resolved[fact.evidence_id] = EvidenceFinding(
                evidence_id=fact.evidence_id,
                source=fact.source,
                observation=prior.observation,
                relation=prior.relation,
                strength=prior.strength,
                limitation=prior.limitation,
            )
            continue
        if fact.limitation:
            resolved[fact.evidence_id] = _finding_from_fact(fact)
            continue
        direct = _direct_counter_finding(dossier, request, fact)
        if direct is not None:
            resolved[fact.evidence_id] = direct
            continue
        analyzable.append(fact)

    if not analyzable:
        return _RequestAnalysis(
            tuple(resolved[fact.evidence_id] for fact in facts)
        )
    if analyst_llm is None:
        resolved.update(
            {
                fact.evidence_id: _mock_finding_from_fact(request, fact)
                for fact in analyzable
            }
        )
        return _RequestAnalysis(
            tuple(resolved[fact.evidence_id] for fact in facts)
        )

    trace: list[tuple[str, str]] = []
    try:
        structured = analyst_llm.with_structured_output(
            _EvidenceAnalysisBatch,
            method=structured_method,
        )
        raw_result = invoke_with_retry(
            structured,
            [
                (
                    "system",
                    (_PROMPT_DIR / "evidence-analysis.txt").read_text(encoding="utf-8"),
                ),
                ("user", _analysis_user_prompt(dossier, request, analyzable)),
            ],
            max_retries=1,
        )
        if raw_result is None:
            raise ValueError("structured evidence analysis returned None")
        batch_result = (
            raw_result
            if isinstance(raw_result, _EvidenceAnalysisBatch)
            else _EvidenceAnalysisBatch.model_validate(raw_result)
        )
        facts_by_id = {fact.evidence_id: fact for fact in analyzable}
        seen_ids: set[str] = set()
        for result in batch_result.findings:
            matched_fact = facts_by_id.get(result.evidence_id)
            if matched_fact is None:
                trace.append(
                    (
                        "analyst_unknown_evidence_ignored",
                        _stable_json(
                            {
                                "request_id": request.id,
                                "evidence_id": result.evidence_id,
                            }
                        ),
                    )
                )
                continue
            if result.evidence_id in seen_ids:
                trace.append(
                    (
                        "analyst_duplicate_evidence_ignored",
                        _stable_json(
                            {
                                "request_id": request.id,
                                "evidence_id": result.evidence_id,
                            }
                        ),
                    )
                )
                continue
            seen_ids.add(result.evidence_id)
            resolved[result.evidence_id] = _finding_from_analysis(
                request, matched_fact, result
            )
        for fact in analyzable:
            if fact.evidence_id in resolved:
                continue
            resolved[fact.evidence_id] = _analysis_missing_finding(fact)
            trace.append(
                (
                    "analyst_missing_evidence",
                    _stable_json(
                        {
                            "request_id": request.id,
                            "evidence_id": fact.evidence_id,
                        }
                    ),
                )
            )
    except Exception as exc:  # noqa: BLE001 - 结构化输出失败安全降级
        logger.warning("EvidenceAgent 关系分析失败，降级 insufficient: %s", exc)
        resolved.update(
            {
                fact.evidence_id: _analysis_error_finding(fact)
                for fact in analyzable
            }
        )
    return _RequestAnalysis(
        tuple(resolved[fact.evidence_id] for fact in facts),
        tuple(trace),
        True,
    )


def _finding_from_fact(fact: _RawFact) -> EvidenceFinding:
    limitation = fact.limitation or "no_analyst_llm"
    return EvidenceFinding(
        evidence_id=fact.evidence_id,
        source=fact.source,
        observation="",
        relation="insufficient",
        strength="contextual",
        limitation=limitation,
    )


def _mock_finding_from_fact(
    request: EvidenceRequest,
    fact: _RawFact,
) -> EvidenceFinding:
    """Return deterministic fake evidence for the explicit mock provider path."""
    if request.purpose == "support" and fact.raw.strip():
        return EvidenceFinding(
            evidence_id=fact.evidence_id,
            source=fact.source,
            observation=fact.raw,
            relation="supports",
            strength="contextual",
            limitation="mock_mode_synthetic_relation",
        )
    return _finding_from_fact(fact)


def _analysis_error_finding(fact: _RawFact) -> EvidenceFinding:
    return EvidenceFinding(
        evidence_id=fact.evidence_id,
        source=fact.source,
        observation="",
        relation="insufficient",
        strength="contextual",
        limitation="analyst_error",
    )


def _analysis_missing_finding(fact: _RawFact) -> EvidenceFinding:
    return EvidenceFinding(
        evidence_id=fact.evidence_id,
        source=fact.source,
        observation="",
        relation="insufficient",
        strength="contextual",
        limitation="analysis_missing_evidence",
    )


def _unique_facts(facts: list[_RawFact]) -> list[_RawFact]:
    unique: list[_RawFact] = []
    seen_ids: set[str] = set()
    for fact in facts:
        if fact.evidence_id in seen_ids:
            continue
        seen_ids.add(fact.evidence_id)
        unique.append(fact)
    return unique


def collect_evidence(
    dossiers: list[CandidateDossier] | tuple[CandidateDossier, ...],
    pending_requests: list[EvidenceRequest] | tuple[EvidenceRequest, ...],
    *,
    tool_client: Any,
    analyst_llm: Any,
    structured_method: str,
    enabled_tools: list[str] | None,
) -> EvidenceBatch:
    """执行已规划请求；每请求恰好生成一条非空 EvidenceNote。"""
    batch = EvidenceBatch()
    by_candidate = {dossier.candidate.id: dossier for dossier in dossiers}
    works: list[_RequestWork] = []
    unique_calls: dict[tuple[str, str], ToolCallSpec] = {}

    # 第一遍只校验请求并规划工具调用；跨请求的相同调用在执行前即完成去重。
    for request in pending_requests:
        dossier = by_candidate.get(request.candidate_id)
        work = _RequestWork(request=request, dossier=dossier)
        works.append(work)
        mismatch = request_strategy_mismatch(request, dossier)
        if mismatch is not None:
            work.ready_note = _insufficient(
                request, "request_strategy_mismatch", detail=mismatch
            )
            continue
        assert dossier is not None
        strategy = STRATEGIES_BY_ID[request.strategy_id]
        work.facts = _base_facts(dossier, request)
        calls = strategy.build_tool_calls(dossier)
        for call in calls:
            if _has_fact_for_tool(call.tool_name, work.facts, dossier):
                continue
            if enabled_tools is not None and call.tool_name not in enabled_tools:
                work.facts.append(
                    _RawFact(
                        _digest(request.id, call.tool_name, "disabled"),
                        f"tool:{call.tool_name}",
                        "",
                        "tool_disabled",
                    )
                )
                continue
            if tool_client is None:
                work.facts.append(
                    _RawFact(
                        _digest(request.id, call.tool_name, "no-client"),
                        f"tool:{call.tool_name}",
                        "",
                        "no_tool_client",
                    )
                )
                continue
            arguments = dict(call.arguments)
            canonical_args = _stable_json(arguments)
            call_key = (call.tool_name, canonical_args)
            first_use = call_key not in unique_calls
            if first_use:
                unique_calls[call_key] = call
            work.tool_uses.append(
                _ToolUse(
                    call=call,
                    key=call_key,
                    canonical_args=canonical_args,
                    first_use=first_use,
                )
            )

    # 第二遍并发执行唯一工具调用，结果仍按首次出现顺序回收。
    call_items = list(unique_calls.items())
    tool_started = perf_counter()
    call_outcomes = run_bounded_parallel(
        call_items,
        lambda item: _call_tool(tool_client, item[1]),
    )
    tool_collection_ms = (perf_counter() - tool_started) * 1000
    cache: dict[tuple[str, str], tuple[str, str, float]] = {}
    first_call_ids: dict[tuple[str, str], str] = {}
    for (cache_key, call), tool_outcome in zip(
        call_items, call_outcomes, strict=True
    ):
        raw, limitation, duration_ms = (
            tool_outcome
            if tool_outcome is not None
            else ("", "tool_error:parallel_execution_failed", 0.0)
        )
        cache[cache_key] = (raw, limitation, duration_ms)
        first_call_ids[cache_key] = _digest(
            "evidence-tool-call", cache_key[0], cache_key[1]
        )
        batch.gathered_context.append(
            GatheredContext(
                call.tool_name,
                cache_key[1],
                raw or limitation,
                duration_ms=duration_ms,
                status=(
                    "failed"
                    if limitation.startswith(("tool_error", "tool_failed"))
                    else "complete"
                ),
            )
        )

    # 第三遍把共享工具结果按请求作用域切片并回填，不改变请求/事实顺序。
    fact_preparation_started = perf_counter()
    for work in works:
        if work.ready_note is not None:
            continue
        request = work.request
        dossier = work.dossier
        assert dossier is not None
        for use in work.tool_uses:
            call = use.call
            raw, limitation, duration_ms = cache[use.key]
            arguments = dict(call.arguments)
            reuse_key = f"{use.key[0]}:{use.key[1]}"
            if use.first_use:
                work.tool_trace.append(
                    (
                        "evidence_tool_called",
                        _stable_json(
                            {
                                "request_id": request.id,
                                "candidate_id": request.candidate_id,
                                "call_id": first_call_ids[use.key],
                                "reuse_key": reuse_key,
                                "tool": call.tool_name,
                                "arguments": arguments,
                                "limitation": limitation,
                                "duration_ms": round(duration_ms, 3),
                            }
                        ),
                    )
                )
            scoped_raw = raw
            scoped_limitation = limitation
            if call.tool_name == "find_sensitive_apis" and raw:
                rows = context_rules.sensitive_api_rows_for_task(raw, dossier.task)
                if rows:
                    scoped_raw = "\n".join(rows)
                else:
                    scoped_raw = ""
                    scoped_limitation = "no_task_sensitive_api"
            evidence_id = _digest(call.tool_name, use.canonical_args, scoped_raw)
            if not use.first_use:
                work.tool_trace.append(
                    (
                        "evidence_tool_reused",
                        _stable_json(
                            {
                                "request_id": request.id,
                                "candidate_id": request.candidate_id,
                                "call_id": _digest(
                                    "evidence-tool-reuse",
                                    request.id,
                                    use.key[0],
                                    use.key[1],
                                ),
                                "reuse_key": reuse_key,
                                "reused_from_call_id": first_call_ids[use.key],
                                "tool": call.tool_name,
                                "arguments": arguments,
                                "evidence_id": evidence_id,
                                "output": raw or limitation,
                            }
                        ),
                    )
                )
            work.facts.append(
                _RawFact(
                    evidence_id=evidence_id,
                    source=f"tool:{call.tool_name}",
                    raw=scoped_raw,
                    limitation=scoped_limitation,
                )
            )

        work.facts = _unique_facts(work.facts)
    fact_preparation_ms = (perf_counter() - fact_preparation_started) * 1000

    # 最后按请求批量分析全部事实，避免同一请求重复发送候选和 task 上下文。
    analysis_items: list[tuple[int, _RequestWork]] = [
        (work_index, work)
        for work_index, work in enumerate(works)
        if work.ready_note is None
    ]
    analysis_started = perf_counter()
    analysis_outcomes = run_bounded_parallel(
        analysis_items,
        lambda item: _analyze_request(
            cast(CandidateDossier, item[1].dossier),
            item[1].request,
            item[1].facts,
            analyst_llm,
            structured_method,
        ),
    )
    fact_analysis_ms = (perf_counter() - analysis_started) * 1000
    findings_by_work: dict[int, list[EvidenceFinding]] = {}
    analysis_trace_by_work: dict[int, list[tuple[str, str]]] = {}
    for (work_index, work), analysis_outcome in zip(
        analysis_items, analysis_outcomes, strict=True
    ):
        outcome = (
            analysis_outcome
            if analysis_outcome is not None
            else _RequestAnalysis(
                tuple(_analysis_error_finding(fact) for fact in work.facts)
            )
        )
        findings_by_work[work_index] = list(outcome.findings)
        analysis_trace_by_work[work_index] = list(outcome.trace)

    for work_index, work in enumerate(works):
        request = work.request
        batch.trace.extend(work.tool_trace)
        batch.trace.extend(analysis_trace_by_work.get(work_index, ()))
        if work.ready_note is not None:
            batch.notes.append(work.ready_note)
            continue
        findings = findings_by_work.get(work_index, [])
        if not findings:
            findings = _insufficient(request, "no_evidence").findings
        note = EvidenceNote(
            request_id=request.id,
            candidate_id=request.candidate_id,
            findings=findings,
        )
        batch.notes.append(note)
        for finding in findings:
            batch.trace.append(
                (
                    "evidence_finding_recorded",
                    _stable_json(
                        {
                            "request_id": request.id,
                            "candidate_id": request.candidate_id,
                            "strategy_id": request.strategy_id,
                            "purpose": request.purpose,
                            "evidence_id": finding.evidence_id,
                            "source": finding.source,
                            "relation": finding.relation,
                            "strength": finding.strength,
                            "limitation": finding.limitation,
                            "observation": finding.observation[:500],
                        }
                    ),
                )
            )
    batch.trace.append(
        (
            "evidence_batch_metrics",
            _stable_json(
                {
                    "request_count": len(pending_requests),
                    "fact_count": sum(
                        len(work.facts)
                        for work in works
                        if work.ready_note is None
                    ),
                    "llm_analysis_calls": sum(
                        outcome is not None and outcome.llm_called
                        for outcome in analysis_outcomes
                    ),
                    "tool_unique_calls": len(call_items),
                    "tool_reused_calls": sum(
                        not use.first_use
                        for work in works
                        for use in work.tool_uses
                    ),
                    "tool_collection_ms": round(tool_collection_ms, 3),
                    "fact_preparation_ms": round(fact_preparation_ms, 3),
                    "fact_analysis_ms": round(fact_analysis_ms, 3),
                }
            ),
        )
    )
    return batch


__all__ = [
    "BoundEvidence",
    "EvidenceBatch",
    "bound_evidence",
    "collect_evidence",
    "request_strategy_mismatch",
]
