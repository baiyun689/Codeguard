"""证据验证节点:链校验、固定配方兜底、重放执行与关系分析(ADR-046)。

取证层是确定性的通用事实采集:链校验/配方/去重/重放全部零 LLM;
LLM 只做关系分析(理解事实)与终审裁决(见 council/verdict.py)。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, Field, field_validator

from codeguard_agent.models.council import CandidateFact, FactRelation
from codeguard_agent.models.schemas import EvidenceTraceStep
from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.concurrency import run_bounded_parallel
from codeguard_agent.pipeline.evidence.guard_scan import scan_guard_fact
from codeguard_agent.pipeline.evidence.planner import CandidateDossier
from codeguard_agent.pipeline.evidence.tags import (
    MAINTAINABILITY_TAGS,
    SECURITY_TAGS,
)

logger = logging.getLogger("codeguard")

_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"

CHAIN_TOOL_NAMES = (
    "get_file_content",
    "inspect_change_impact",
    "inspect_security_path",
    "inspect_structure",
)
MAX_CHAIN_STEPS = 3
_FILE_ARG = "file_path"
_SYMBOL_ARG = "symbol_id"


def validate_chain(
    steps: list[EvidenceTraceStep] | tuple[EvidenceTraceStep, ...],
) -> tuple[EvidenceTraceStep, ...]:
    """确定性形状校验:工具四选一、参数键合法、located 必填、链长 ≤3。"""
    valid: list[EvidenceTraceStep] = []
    for step in steps[:MAX_CHAIN_STEPS]:
        if step.tool not in CHAIN_TOOL_NAMES:
            continue
        expected = _FILE_ARG if step.tool == "get_file_content" else _SYMBOL_ARG
        args = step.args or {}
        located = step.located or ""
        # model_construct 可产出 None 的 args/located;参数键白名单(多余键视为非法)
        if not args.get(expected) or set(args) != {expected} or not located.strip():
            continue
        valid.append(step)
    return tuple(valid)


def replay_calls(
    steps: tuple[EvidenceTraceStep, ...],
) -> list[tuple[str, dict[str, str]]]:
    """把校验通过的取证链转成重放调用。"""
    return [(step.tool, dict(step.args)) for step in steps]


def recipe_calls(
    dossier: CandidateDossier, tag: RiskTag,
) -> list[tuple[str, dict[str, str]]]:
    """固定配方兜底:文件内容 + 有 symbol 则上游调用方 + 标签开关(ADR-046 §5.4)。"""
    calls: list[tuple[str, dict[str, str]]] = [
        ("get_file_content", {_FILE_ARG: dossier.task.file})
    ]
    symbol = _symbol_id(dossier)
    if not symbol:
        return calls
    calls.append(("inspect_change_impact", {_SYMBOL_ARG: symbol}))
    if tag in SECURITY_TAGS:
        calls.append(("inspect_security_path", {_SYMBOL_ARG: symbol}))
    if tag in MAINTAINABILITY_TAGS:
        calls.append(("inspect_structure", {_SYMBOL_ARG: symbol}))
    return calls


def _symbol_id(dossier: CandidateDossier) -> str:
    """候选行号在 task 预解析符号区间内精确匹配;line=0 或未命中回退首个 symbol。"""
    if dossier.context_bundle is None:
        return ""
    candidate_line = dossier.candidate.line or 0
    fallback = ""
    for fact in dossier.context_bundle.facts:
        if fact.kind != "symbol_context" or fact.truncated:
            continue
        try:
            value = json.loads(fact.content)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        symbol = str(value.get("symbol_id", ""))
        if not symbol:
            continue
        if not fallback:
            fallback = symbol
        if candidate_line > 0:
            start = int(value.get("start_line", 0) or 0)
            end = int(value.get("end_line", 0) or 0)
            if start <= candidate_line <= end:
                return symbol
    return fallback


def _call_tool(
    tool_client: Any, tool: str, arguments: dict[str, str],
) -> tuple[str, str]:
    """执行一次 Gateway 工具调用,返回 (raw, limitation)。失败不抛。

    图响应的完整性校验(graph subject/source_scope/status/coverage)原样保留——
    这是证据链工具调用正确性的关键护栏(历史教训:该校验缺失导致过整档评测作废)。
    """
    kwargs = dict(arguments)
    try:
        response = getattr(tool_client, tool)(**kwargs)
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
    if tool.startswith("inspect_"):
        try:
            payload = json.loads(text)
            expected_subject = kwargs.get("symbol_id", "")
            actual_subject = str(payload.get("subject_symbol_id", ""))
            if expected_subject and actual_subject and actual_subject != expected_subject:
                return text, "graph_subject_mismatch"
            status = payload.get("status")
            coverage = payload.get("coverage")
            source_scope = str(payload.get("source_scope", "")).upper()
            relationships = payload.get("relationships")
            test_relationships = payload.get("test_relationships")
            if source_scope:
                if source_scope not in {"MAIN", "TEST", "GENERATED"}:
                    return text, "invalid_graph_source_scope"
                if isinstance(relationships, list) and any(
                    str(item.get("source_set", "")).upper() not in {"", source_scope}
                    for item in relationships
                    if isinstance(item, dict)
                ):
                    return text, "graph_source_scope_mismatch"
                if isinstance(test_relationships, list) and any(
                    str(item.get("source_set", "")).upper() not in {"", "TEST"}
                    for item in test_relationships
                    if isinstance(item, dict)
                ):
                    return text, "graph_source_scope_mismatch"
                if (
                    tool in {"inspect_change_impact", "inspect_security_path"}
                    and source_scope in {"MAIN", "GENERATED"}
                    and status == "confirmed"
                    and isinstance(relationships, list)
                    and not relationships
                    and isinstance(test_relationships, list)
                    and bool(test_relationships)
                ):
                    return text, "graph_test_only_confirmation"
            if status == "unknown":
                return text, "graph_unknown"
            # coverage=partial 只表示图数据可能不全(全局 unresolved 边/结果截断),
            # 不再整体废弃——confirmed 的调用方/入口事实仍应进入证据链,
            # 数据边界由返回中的 coverage/limitations 字段供分析层自行判断。
            if coverage == "partial":
                return text, ""
            if status not in {"confirmed", "not_found"}:
                return text, "invalid_graph_status"
        except (TypeError, ValueError, json.JSONDecodeError):
            return text, "invalid_graph_response"
    return text, ""


def _normalized(text: str) -> str:
    """去空白规范化:换行/制表/空格全部移除(代码区分大小写,大小写保留)。"""
    return "".join(text.split())


def _located_match(raw: str, located: str) -> bool:
    located_norm = _normalized(located)
    if not located_norm:
        return False
    return located_norm in _normalized(raw)


def _collect_facts(
    dossiers: list[CandidateDossier],
    *,
    tool_client: Any,
    tag_by_candidate: dict[str, RiskTag],
) -> tuple[dict[str, list[CandidateFact]], list[tuple[str, str]], list[Any]]:
    """为每个候选规划调用(链重放优先/配方兜底),全局去重执行,产出事实与 trace。"""
    per_candidate_calls: dict[str, list[tuple[str, dict[str, str]]]] = {}
    per_candidate_located: dict[str, dict[tuple[str, str], str]] = {}
    unique_calls: dict[tuple[str, str], tuple[str, dict[str, str]]] = {}

    for dossier in dossiers:
        cid = dossier.candidate.id
        tag = tag_by_candidate.get(cid, RiskTag.GENERAL_REVIEW)
        steps = validate_chain(dossier.candidate.evidence_chain)
        if steps:
            calls = replay_calls(steps)
            for step in steps:
                key_args = (
                    {_FILE_ARG: step.args[_FILE_ARG]}
                    if step.tool == "get_file_content"
                    else {_SYMBOL_ARG: step.args[_SYMBOL_ARG]}
                )
                # setdefault 保留首个 located:重复步骤以第一次引文为准
                per_candidate_located.setdefault(cid, {}).setdefault(
                    (step.tool, _stable_json(key_args)), step.located
                )
            # 链内去重:相同 (tool, args) 只保留一次,再进全局 unique_calls
            seen: set[tuple[str, str]] = set()
            deduped: list[tuple[str, dict[str, str]]] = []
            for tool, arguments in calls:
                key = (tool, _stable_json(arguments))
                if key in seen:
                    continue
                seen.add(key)
                deduped.append((tool, arguments))
            calls = deduped
        else:
            calls = recipe_calls(dossier, tag)
        per_candidate_calls[cid] = calls

    for cid, calls in per_candidate_calls.items():
        for tool, arguments in calls:
            key = (tool, _stable_json(arguments))
            unique_calls.setdefault(key, (tool, arguments))

    call_items = list(unique_calls.items())
    outcomes = run_bounded_parallel(
        call_items,
        lambda item: _call_tool(tool_client, item[1][0], item[1][1]),
    )
    cache: dict[tuple[str, str], tuple[str, str]] = {}
    for (key, (_tool, _arguments)), outcome in zip(call_items, outcomes, strict=True):
        cache[key] = (
            outcome if outcome is not None
            else ("", "tool_error:parallel_execution_failed")
        )

    facts_by_candidate: dict[str, list[CandidateFact]] = {}
    trace: list[tuple[str, str]] = []
    gathered: list[Any] = []

    for dossier in dossiers:
        cid = dossier.candidate.id
        facts: list[CandidateFact] = []
        for tool, arguments in per_candidate_calls.get(cid, []):
            key = (tool, _stable_json(arguments))
            raw, limitation = cache[key]
            located = per_candidate_located.get(cid, {}).get(key, "")
            status: Literal["verified", "unverified", "failed", "recipe"]
            if limitation:
                status = "failed"  # 调用失败优先(沙箱拒绝/符号不存在)
            elif located:
                status = "verified" if _located_match(raw, located) else "unverified"
            else:
                status = "recipe"
            fact = CandidateFact(
                fact_id=_digest(cid, tool, _stable_json(arguments)),
                source=f"tool:{tool}",
                raw=raw,
                replay_status=status,
                limitation=limitation,
            )
            facts.append(fact)
            trace.append(
                (
                    "evidence_tool_called",
                    _stable_json({
                        "candidate_id": cid,
                        "tool": tool,
                        "arguments": arguments,
                        "replay_status": status,
                    }),
                )
            )
            if raw:
                # 键对齐 GatheredContext 字段(tool/args/content),供 view_model 与误报复核读取
                gathered.append({
                    "tool": tool,
                    "args": _stable_json(arguments),
                    "content": raw,
                })
        facts_by_candidate[cid] = facts

    return facts_by_candidate, trace, gathered


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _digest(*parts: str) -> str:
    import hashlib

    payload = "\0".join(parts)
    return f"fact-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:12]}"


@dataclass
class VerifyBatch:
    facts: dict[str, list[CandidateFact]] = field(default_factory=dict)
    relations: dict[str, list[FactRelation]] = field(default_factory=dict)
    trace: list[tuple[str, str]] = field(default_factory=list)
    gathered_context: list[Any] = field(default_factory=list)


class _RelationBatch(BaseModel):
    findings: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("findings", mode="before")
    @classmethod
    def parse_stringified_findings(cls, value: object) -> object:
        # 兼容部分 OpenAI 端点把数组参数序列化为 JSON 字符串(自旧 evidence.agent 迁移)
        if isinstance(value, str):
            for candidate in (
                value,
                value[value.find("[") : value.rfind("]") + 1],
            ):
                try:
                    parsed = json.loads(candidate)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(parsed, dict):
                    parsed = parsed.get("findings", parsed)
                if isinstance(parsed, list):
                    return parsed
        return value


def analyze_relations(
    dossier: CandidateDossier,
    facts: list[CandidateFact],
    *,
    tag: RiskTag,
    analyst_llm: Any,
    structured_method: str,
) -> list[FactRelation]:
    """对单个候选的全部事实做一次关系分析(ADR-046:每候选 1 次 LLM)。

    - 带 limitation/空 raw 的事实直接 insufficient(不花 LLM);
    - guard 注解扫描器命中的事实直接 direct contradicts(确定性先验);
    - mock(analyst_llm=None)时:其余有内容的事实按 supports/contextual 处理;
    - LLM 失败/None 输出:全部 insufficient(门控按"无支持"处理,终审兜底,不误杀)。
    """
    direct: dict[str, FactRelation] = {}
    analyzable: list[CandidateFact] = []
    for fact in facts:
        if fact.limitation or not fact.raw.strip():
            direct[fact.fact_id] = FactRelation(
                fact_id=fact.fact_id, relation="insufficient", strength="contextual",
                limitation=fact.limitation or "fact_empty",
            )
        else:
            analyzable.append(fact)

    # guard 确定性先验:命中的事实不再交 LLM 重复分析
    for fact in list(analyzable):
        prior = scan_guard_fact(dossier, fact, tag)
        if prior is not None:
            direct[fact.fact_id] = prior
            analyzable.remove(fact)

    if analyzable and analyst_llm is None:
        for fact in analyzable:
            direct[fact.fact_id] = FactRelation(
                fact_id=fact.fact_id, relation="supports", strength="contextual",
                observation=fact.raw[:500], limitation="mock_mode_synthetic_relation",
            )

    if analyzable and analyst_llm is not None:
        try:
            structured = analyst_llm.with_structured_output(
                _RelationBatch, method=structured_method,
            )
            from codeguard_agent.llm.client import invoke_with_retry

            raw_result = invoke_with_retry(
                structured,
                [
                    ("system", (_PROMPT_DIR / "evidence-analysis.txt").read_text(encoding="utf-8")),
                    ("user", _relation_payload(dossier, analyzable)),
                ],
                max_retries=1,
            )
            batch_result = (
                raw_result if isinstance(raw_result, _RelationBatch)
                else _RelationBatch.model_validate(raw_result)
            )
            facts_by_id = {fact.fact_id: fact for fact in analyzable}
            seen: set[str] = set()
            for item in batch_result.findings:
                fact_id = str(item.get("fact_id", ""))
                if fact_id not in facts_by_id or fact_id in seen:
                    continue
                seen.add(fact_id)
                relation = item.get("relation")
                # 非 str(不可哈希)视为非法值逐项降级,避免 TypeError 拖垮整批
                if not isinstance(relation, str) or relation not in {
                    "supports", "contradicts", "insufficient"
                }:
                    relation = "insufficient"
                strength = item.get("strength")
                if not isinstance(strength, str) or strength not in {
                    "direct", "contextual"
                }:
                    strength = "contextual"
                observation = str(item.get("observation", "")).strip()
                limitation = str(item.get("limitation", ""))
                # 遵守 FactRelation 观察不变量(与 validator 一致)
                if relation in {"supports", "contradicts"} and not observation:
                    relation = "insufficient"
                    strength = "contextual"
                    limitation = limitation or "observation_missing"
                elif relation == "insufficient":
                    strength = "contextual"
                    limitation = limitation or "analysis_unclear"
                direct[fact_id] = FactRelation(
                    fact_id=fact_id,
                    relation=cast(
                        Literal["supports", "contradicts", "insufficient"], relation,
                    ),
                    strength=cast(Literal["direct", "contextual"], strength),
                    observation=observation,
                    limitation=limitation,
                )
        except Exception as exc:  # noqa: BLE001 分析失败安全降级
            logger.warning("evidence relation analysis failed: %s", exc)
        for fact in analyzable:
            if fact.fact_id not in direct:
                direct[fact.fact_id] = FactRelation(
                    fact_id=fact.fact_id, relation="insufficient", strength="contextual",
                    limitation="analysis_failed_or_missing",
                )

    return [direct[fact.fact_id] for fact in facts]


def _relation_payload(dossier: CandidateDossier, facts: list[CandidateFact]) -> str:
    return _stable_json({
        "candidate_alias": "C001",
        "candidate": {
            "type": dossier.candidate.type,
            "claim": dossier.candidate.claim,
            "file": dossier.candidate.file,
            "line": dossier.candidate.line,
        },
        "task_patch": dossier.task.patch,
        "facts": [
            {
                "fact_id": fact.fact_id,
                "source": fact.source,
                "raw": fact.raw[:2000],
                "raw_truncated": len(fact.raw) > 2000,
                "replay_status": fact.replay_status,
                "limitation": fact.limitation,
            }
            for fact in facts
        ],
    })


def verify_evidence(
    dossiers: list[CandidateDossier],
    *,
    tool_client: Any,
    analyst_llm: Any,
    structured_method: str,
    enabled_tools: list[str] | None,
    tag_by_candidate: dict[str, RiskTag],
) -> VerifyBatch:
    """取证验证主入口:收集事实(链重放/配方兜底)→ 逐候选关系分析。

    enabled_tools 本阶段接收但暂不用于过滤——validate_chain 的 CHAIN_TOOL_NAMES
    已隐含工具白名单;保留参数以匹配 graph 接线契约(Task 11)。
    """
    batch = VerifyBatch()

    facts_by_candidate: dict[str, list[CandidateFact]] = {}
    trace: list[tuple[str, str]] = []
    if tool_client is not None:
        facts_by_candidate, trace, batch.gathered_context = _collect_facts(
            dossiers, tool_client=tool_client, tag_by_candidate=tag_by_candidate,
        )
    else:
        # 无工具服务:事实=patch 文本,关系分析照常(ADR-046 §7)
        for dossier in dossiers:
            cid = dossier.candidate.id
            facts_by_candidate[cid] = [
                CandidateFact(
                    fact_id=_digest(cid, "patch", "0"),
                    source="diff",
                    raw=dossier.task.patch,
                    replay_status="recipe",
                )
            ]
    batch.facts = facts_by_candidate
    batch.trace.extend(trace)

    analyzable_items = [
        (dossier, facts_by_candidate.get(dossier.candidate.id, []))
        for dossier in dossiers
        if facts_by_candidate.get(dossier.candidate.id)
    ]
    outcomes = run_bounded_parallel(
        analyzable_items,
        lambda item: analyze_relations(
            item[0], item[1],
            tag=tag_by_candidate.get(item[0].candidate.id, RiskTag.GENERAL_REVIEW),
            analyst_llm=analyst_llm,
            structured_method=structured_method,
        ),
    )
    for (dossier, facts), outcome in zip(analyzable_items, outcomes, strict=True):
        batch.relations[dossier.candidate.id] = (
            outcome if outcome is not None else [
                FactRelation(fact_id=fact.fact_id, relation="insufficient",
                             strength="contextual", limitation="parallel_analysis_failed")
                for fact in facts
            ]
        )
    batch.trace.append(
        ("evidence_batch_metrics", _stable_json({
            "candidates": len(dossiers),
            "fact_count": sum(len(v) for v in facts_by_candidate.values()),
        }))
    )
    return batch
