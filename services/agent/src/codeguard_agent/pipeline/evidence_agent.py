"""策略约束下的候选级证据收集与关系分析。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.council import EvidenceFinding, EvidenceNote, EvidenceRequest
from codeguard_agent.pipeline import context_rules
from codeguard_agent.pipeline.concurrency import run_bounded_parallel
from codeguard_agent.pipeline.engines import GatheredContext
from codeguard_agent.pipeline.evidence_planner import CandidateDossier
from codeguard_agent.pipeline.evidence_rules import STRATEGIES_BY_ID
from codeguard_agent.pipeline.evidence_rules.types import ToolCallSpec

logger = logging.getLogger("codeguard")
_PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"


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
    relation: Literal["supports", "contradicts", "insufficient"]
    strength: Literal["direct", "contextual"]
    observation: str = ""
    limitation: str = ""


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
    if request.preferred_tools != _expected_tools(calls):
        return "preferred_tools"
    if any(call.tool_name not in strategy.allowed_tools for call in calls):
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
        for fact in bundle.facts:
            if fact.kind not in strategy.context_kinds:
                continue
            truncated = bundle.truncated or fact.truncated
            facts.append(
                _RawFact(
                    evidence_id=_digest(fact.source, fact.kind, fact.content),
                    source=f"context:{fact.kind}",
                    raw=fact.content,
                    limitation="context_truncated" if truncated else "",
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


def _call_tool(tool_client: Any, call: ToolCallSpec) -> tuple[str, str]:
    kwargs = dict(call.arguments)
    try:
        response = getattr(tool_client, call.tool_name)(**kwargs)
    except Exception as exc:  # noqa: BLE001 - 单次工具异常收敛为不足证据
        return "", f"tool_error:{exc}"
    success = bool(getattr(response, "success", True))
    raw = getattr(response, "result", None)
    if raw is None and hasattr(response, "as_tool_output"):
        raw = response.as_tool_output()
    text = str(raw or "")
    if not success:
        return text, "tool_failed"
    if not text.strip():
        return "", "tool_empty"
    return text, ""


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
        if context_fact.kind != "ast_structure" or context_fact.truncated:
            continue
        method_name = context_rules.resolve_method_name(context_fact.content, dossier.task)
        if method_name is None:
            continue
        task_span = context_rules._task_span(dossier.task)
        if task_span is None:
            return None
        for line in context_fact.content.splitlines():
            match = _METHOD_RANGE.search(line.strip())
            if not match or match.group(1) != method_name:
                continue
            start, end = int(match.group(2)), int(match.group(3))
            if start <= task_span[1] and end >= task_span[0]:
                return method_name, start, end, line.strip()
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
    fact: _RawFact,
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
        "purpose": request.purpose,
        "strategy_question": request.question,
        "task_patch": dossier.task.patch,
        "risk_profile": risk,
        "task_context": (
            dossier.context_bundle.model_dump(mode="json")
            if dossier.context_bundle is not None
            else None
        ),
        "fact": {
            "source": fact.source,
            "raw": fact.raw,
            "limitation": fact.limitation,
        },
    }
    return _stable_json(payload)


def _analyze_fact(
    dossier: CandidateDossier,
    request: EvidenceRequest,
    fact: _RawFact,
    analyst_llm: Any,
    structured_method: str,
) -> EvidenceFinding:
    if fact.prior_finding is not None:
        prior = fact.prior_finding
        return EvidenceFinding(
            evidence_id=fact.evidence_id,
            source=fact.source,
            observation=prior.observation,
            relation=prior.relation,
            strength=prior.strength,
            limitation=prior.limitation,
        )
    if fact.limitation:
        return _finding_from_fact(fact)
    direct = _direct_counter_finding(dossier, request, fact)
    if direct is not None:
        return direct
    if analyst_llm is None:
        return _mock_finding_from_fact(request, fact)
    try:
        structured = analyst_llm.with_structured_output(
            _EvidenceAnalysis,
            method=structured_method,
        )
        raw_result = invoke_with_retry(
            structured,
            [
                (
                    "system",
                    (_PROMPT_DIR / "evidence-analysis.txt").read_text(encoding="utf-8"),
                ),
                ("user", _analysis_user_prompt(dossier, request, fact)),
            ],
            max_retries=1,
        )
        if raw_result is None:
            raise ValueError("structured evidence analysis returned None")
        result = (
            raw_result
            if isinstance(raw_result, _EvidenceAnalysis)
            else _EvidenceAnalysis.model_validate(raw_result)
        )
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
    except Exception as exc:  # noqa: BLE001 - 结构化输出失败安全降级
        logger.warning("EvidenceAgent 关系分析失败，降级 insufficient: %s", exc)
        return _analysis_error_finding(fact)


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
    call_outcomes = run_bounded_parallel(
        call_items,
        lambda item: _call_tool(tool_client, item[1]),
    )
    cache: dict[tuple[str, str], tuple[str, str]] = {}
    for (cache_key, call), tool_outcome in zip(
        call_items, call_outcomes, strict=True
    ):
        raw, limitation = (
            tool_outcome
            if tool_outcome is not None
            else ("", "tool_error:parallel_execution_failed")
        )
        cache[cache_key] = (raw, limitation)
        batch.gathered_context.append(
            GatheredContext(call.tool_name, cache_key[1], raw or limitation)
        )

    # 第三遍把共享工具结果按请求作用域切片并回填，不改变请求/事实顺序。
    for work in works:
        if work.ready_note is not None:
            continue
        request = work.request
        dossier = work.dossier
        assert dossier is not None
        for use in work.tool_uses:
            call = use.call
            raw, limitation = cache[use.key]
            arguments = dict(call.arguments)
            if use.first_use:
                work.tool_trace.append(
                    (
                        "evidence_tool_called",
                        _stable_json(
                            {
                                "request_id": request.id,
                                "candidate_id": request.candidate_id,
                                "tool": call.tool_name,
                                "arguments": arguments,
                                "limitation": limitation,
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
                                "tool": call.tool_name,
                                "evidence_id": evidence_id,
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

    # 最后把所有需要判断的事实扁平化后受控并发分析，再按原坐标稳定组装。
    analysis_items: list[tuple[int, _RequestWork, _RawFact]] = []
    for work_index, work in enumerate(works):
        if work.ready_note is not None:
            continue
        for fact in work.facts:
            analysis_items.append((work_index, work, fact))
    analysis_outcomes = run_bounded_parallel(
        analysis_items,
        lambda item: _analyze_fact(
            cast(CandidateDossier, item[1].dossier),
            item[1].request,
            item[2],
            analyst_llm,
            structured_method,
        ),
    )
    findings_by_work: dict[int, list[EvidenceFinding]] = {}
    for (work_index, _, fact), analysis_outcome in zip(
        analysis_items, analysis_outcomes, strict=True
    ):
        findings_by_work.setdefault(work_index, []).append(
            analysis_outcome
            if analysis_outcome is not None
            else _analysis_error_finding(fact)
        )

    for work_index, work in enumerate(works):
        request = work.request
        batch.trace.extend(work.tool_trace)
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
    return batch


__all__ = [
    "BoundEvidence",
    "EvidenceBatch",
    "bound_evidence",
    "collect_evidence",
    "request_strategy_mismatch",
]
