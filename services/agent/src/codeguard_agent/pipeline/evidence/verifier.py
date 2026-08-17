"""证据验证节点:链校验、固定配方兜底、重放执行与关系分析(ADR-046)。

取证层是确定性的通用事实采集:链校验/配方/去重/重放全部零 LLM;
LLM 只做关系分析(理解事实)与终审裁决(见 council/verdict.py)。
"""

from __future__ import annotations

import json
import logging
import re
import time
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


# ────────────────────────────────────────────────────────────────
# 图工具关系断言匹配(2026-08-17 单 case 评测 TP=0 修复)
#
# 审查员对图工具的 located 引文多为"人话转述"("getCommandLine ->
# getRawCommandLine (line 129)"),与 JSON 原文做子串匹配必然全灭。
# 改为从 located 解析"调用关系断言",与 relationships 数组结构化核对:
# 符号双侧短名匹配 + 行号容差 ±2 + 文件名后缀限定。编造的边依旧
# 核对不上,幻觉检测内核保留。
# ────────────────────────────────────────────────────────────────

_GRAPH_TOOLS = ("inspect_change_impact", "inspect_security_path", "inspect_structure")
_GRAPH_LINE_TOLERANCE = 2


@dataclass(frozen=True)
class _GraphAssertion:
    """located 中一条 'A -> B' 调用关系断言(可带行号/文件名限定)。"""

    source: str
    target: str
    file: str = ""
    line: int = 0


_EDGE_RE = re.compile(
    r"(?P<source>[A-Za-z_$][\w$]*(?:[.#][A-Za-z_$][\w$]*)*)"
    r"\s*->\s*"
    r"(?P<target>[A-Za-z_$][\w$]*(?:[.#][A-Za-z_$][\w$]*)*)"
    r"(?:\s*\(\s*(?P<loc>[^()]*)\s*\))?"
)
_LINE_RE = re.compile(r"\bline\s*(\d+)", re.IGNORECASE)
_FILE_LINE_RE = re.compile(r"([\w$./\\-]+\.\w+)\s*:\s*(\d+)")


def _parse_assertions(located: str) -> list[_GraphAssertion]:
    """全局扫描 located 中的 'A -> B' 断言,其余文本(前缀、混入的代码原文)自然忽略。

    括号内支持两种定位形态:'(line 129)' 与 '(CmdShell.java:84)'。
    """
    assertions: list[_GraphAssertion] = []
    for match in _EDGE_RE.finditer(located):
        loc = match.group("loc") or ""
        line = 0
        file_name = ""
        line_match = _LINE_RE.search(loc)
        if line_match:
            line = int(line_match.group(1))
        file_match = _FILE_LINE_RE.search(loc)
        if file_match:
            file_name = file_match.group(1)
            if not line:
                line = int(file_match.group(2))
        assertions.append(
            _GraphAssertion(
                source=match.group("source").replace("#", "."),
                target=match.group("target").replace("#", "."),
                file=file_name,
                line=line,
            )
        )
    return assertions


def _split_symbol_id(side_id: str) -> tuple[str, str]:
    """拆图符号 id 为 (类全名, 方法名)。

    'java:...Shell#getCommandLine(...)' → ('org...Shell', 'getCommandLine');
    类型节点无 '#' → (类全名, '');构造器方法名保留 '<init>Commandline' 形态。
    """
    body = side_id.split(":", 1)[-1]
    if "#" not in body:
        return body, ""
    class_part, _, rest = body.partition("#")
    return class_part, rest.partition("(")[0]


def _side_match(token: str, side_id: str) -> bool:
    """located 一侧的符号 token 与边条目 sourceId/targetId 的匹配。

    规则:类名逐段后缀匹配(容忍无限定短名)、方法名精确相等;
    构造器特例('<init>Commandline' 与裸类名 'Commandline' 匹配);
    类型节点按类短名匹配。
    """
    parts = [p for p in re.split(r"[.#]", token) if p]
    if not parts:
        return False
    class_full, method = _split_symbol_id(side_id)
    method_token = parts[-1]
    qualifiers = parts[:-1]
    if method:
        if (
            not qualifiers
            and method.startswith("<init>")
            and method[len("<init>"):] == method_token
        ):
            return True
        if method_token != method:
            return False
    else:
        if qualifiers:
            return False
        return class_full.split(".")[-1] == method_token
    if qualifiers:
        class_segments = class_full.split(".")
        return class_segments[-len(qualifiers):] == qualifiers
    return True


def _graph_edges(raw: str) -> list[dict[str, Any]]:
    """汇总图响应四个关系数组里 sourceId/targetId 齐全的边;非 JSON 返回空。"""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    edges: list[dict[str, Any]] = []
    for key in (
        "relationships",
        "main_relationships",
        "test_relationships",
        "generated_relationships",
    ):
        for item in payload.get(key) or []:
            if isinstance(item, dict) and item.get("sourceId") and item.get("targetId"):
                edges.append(item)
    return edges


def _assertion_hit(assertion: _GraphAssertion, edges: list[dict[str, Any]]) -> bool:
    for edge in edges:
        if not _side_match(assertion.source, str(edge.get("sourceId", ""))):
            continue
        if not _side_match(assertion.target, str(edge.get("targetId", ""))):
            continue
        if (
            assertion.line
            and abs(int(edge.get("line", 0) or 0) - assertion.line)
            > _GRAPH_LINE_TOLERANCE
        ):
            continue
        if assertion.file and not str(edge.get("file", "")).replace("\\", "/").endswith(
            "/" + assertion.file
        ):
            continue
        return True
    return False


def _non_assertion_remnants(located: str) -> list[str]:
    """切除断言片段后的余料(前缀/分隔符/混入的原文引用),供兜底子串核对。"""
    remnants: list[str] = []
    last_end = 0
    for match in _EDGE_RE.finditer(located):
        if match.start() > last_end:
            remnants.append(located[last_end:match.start()])
        last_end = match.end()
    if last_end < len(located):
        remnants.append(located[last_end:])
    return [r for r in remnants if r.strip()]


def _graph_assertions_match(located: str, raw: str) -> bool:
    """图工具 located 核对:解析 'A -> B' 断言与 relationships 结构化核对。

    裁决决策:至少一条断言命中即 verified——一条 'A->B+行号+文件' 全吻合的边
    不可能"蒙对",即构成诚实性证明;unverified 只是降权不击杀,未命中的其余
    断言仍留在事实原文里由关系分析与终审把关。无断言可提取时回退逐字子串
    核对(兼容纯原文引用形态);断言全部未命中时,用切除断言后的余料做子串
    兜底(兼容"断言 + 原文片段"混合引用)。
    """
    located = located.strip()
    if not located:
        return False
    assertions = _parse_assertions(located)
    if not assertions:
        return _located_match(raw, located)
    edges = _graph_edges(raw)
    if any(_assertion_hit(assertion, edges) for assertion in assertions):
        return True
    return any(
        _located_match(raw, remnant)
        for remnant in _non_assertion_remnants(located)
    )


def _replay_hit(tool: str, raw: str, located: str) -> bool:
    """重放命中分派:图工具走断言核对,文件工具保持逐字子串核对。"""
    if tool in _GRAPH_TOOLS:
        return _graph_assertions_match(located, raw)
    return _located_match(raw, located)


# ────────────────────────────────────────────────────────────────
# 图响应确定性压缩(2026-08-17 单 case 评测 TP=0 修复之二)
#
# 14KB 图 JSON 被 raw[:2000] 截断在 symbols 数组中间,关系分析 LLM
# 看不到 relationships 内容,叠加 unverified 判全 insufficient。
# 改为进分析前确定性结构化压缩:保留头部/符号核心字段/全部调用边,
# 丢低信息字段与冗余 fallback 数组。CandidateFact.raw 与
# gathered_context 保持全量原文,只压缩发给关系分析 LLM 的载荷。
# ────────────────────────────────────────────────────────────────

_GRAPH_SUMMARY_MAX_CHARS = 8000
_GRAPH_HEADER_KEYS = (
    "status", "coverage", "source_scope", "subject_symbol_id", "limitations",
)
_GRAPH_SYMBOL_KEYS = ("id", "kind", "file", "startLine", "endLine")
_GRAPH_RELATION_KEYS = ("sourceId", "targetId", "kind", "file", "line")
_GRAPH_FALLBACK_KEYS = (
    "main_relationships", "test_relationships", "generated_relationships",
)
_FILE_RAW_MAX_CHARS = 2000


def _graph_summary(raw: str) -> str:
    """对 inspect_* 图响应做确定性结构化压缩(零 LLM)。

    主 relationships 非空时丢弃四个 fallback 数组(体积减半以上);
    主数组为空(not_found + test 边透传场景)时补 fallback 数组,
    分析层才能判断不足而不是瞎判。非 JSON 按上限截断兜底。
    """
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return raw[:_GRAPH_SUMMARY_MAX_CHARS]
    if not isinstance(payload, dict):
        return raw[:_GRAPH_SUMMARY_MAX_CHARS]
    summary: dict[str, Any] = {
        key: payload.get(key) for key in _GRAPH_HEADER_KEYS if key in payload
    }
    symbols = [
        {key: symbol.get(key) for key in _GRAPH_SYMBOL_KEYS if key in symbol}
        for symbol in payload.get("symbols") or []
        if isinstance(symbol, dict)
    ]
    relations = [
        {key: rel.get(key) for key in _GRAPH_RELATION_KEYS if key in rel}
        for rel in payload.get("relationships") or []
        if isinstance(rel, dict)
    ]
    if not relations:
        for key in _GRAPH_FALLBACK_KEYS:
            extra = [
                {k: rel.get(k) for k in _GRAPH_RELATION_KEYS if k in rel}
                for rel in payload.get(key) or []
                if isinstance(rel, dict)
            ]
            if extra:
                summary[key] = extra
    summary["symbols"] = symbols
    summary["relationships"] = relations
    return _fit_graph_summary(json.dumps(summary, ensure_ascii=False))


def _fit_graph_summary(text: str, *, max_chars: int = _GRAPH_SUMMARY_MAX_CHARS) -> str:
    """长度阶梯(信息牺牲从小到大):删 symbols → 边截 60/30/10 → 硬截断。

    删符号先于截边:调用关系是本次修复的核心,符号先让位;每级截断后
    都是合法 JSON(硬截断是极端图的最后防线,现实中边截 10 已足够)。
    """
    if len(text) <= max_chars:
        return text
    payload: Any = json.loads(text)
    if isinstance(payload, dict) and payload.get("symbols"):
        del payload["symbols"]
        text = json.dumps(payload, ensure_ascii=False)
    if len(text) <= max_chars:
        return text
    if isinstance(payload, dict):
        for limit in (60, 30, 10):
            relations = payload.get("relationships") or []
            if len(relations) > limit:
                payload["relationships"] = relations[:limit]
                text = json.dumps(payload, ensure_ascii=False)
            if len(text) <= max_chars:
                return text
    return text[:max_chars]


def _relation_raw(fact: CandidateFact) -> tuple[str, bool]:
    """图事实确定性压缩、文件事实维持 2000 字符截断;返回 (载荷文本, 是否压缩)。

    文件事实的 located 已逐字 verified,LLM 对内容已有信心,维持 2000 旋钮。
    """
    if fact.source in {
        "tool:inspect_change_impact",
        "tool:inspect_security_path",
        "tool:inspect_structure",
    }:
        summary = _graph_summary(fact.raw)
        return summary, len(summary) < len(fact.raw)
    return fact.raw[:_FILE_RAW_MAX_CHARS], len(fact.raw) > _FILE_RAW_MAX_CHARS


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
    trace: list[tuple[str, str]] = []

    for dossier in dossiers:
        cid = dossier.candidate.id
        tag = tag_by_candidate.get(cid, RiskTag.GENERAL_REVIEW)
        steps = validate_chain(dossier.candidate.evidence_chain)
        if steps:
            calls = replay_calls(steps)
            path = "chain"
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
            path = "recipe"
        per_candidate_calls[cid] = calls
        trace.append(
            (
                "candidate_evidence_path",
                _stable_json({"candidate_id": cid, "path": path}),
            )
        )

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
                status = "verified" if _replay_hit(tool, raw, located) else "unverified"
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
    llm_calls: list[int] | None = None,
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

            if llm_calls is not None:
                llm_calls.append(1)
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
    fact_entries: list[dict[str, Any]] = []
    for fact in facts:
        raw_text, raw_truncated = _relation_raw(fact)
        fact_entries.append({
            "fact_id": fact.fact_id,
            "source": fact.source,
            "raw": raw_text,
            "raw_truncated": raw_truncated,
            "replay_status": fact.replay_status,
            "limitation": fact.limitation,
        })
    return _stable_json({
        "candidate_alias": "C001",
        "candidate": {
            "type": dossier.candidate.type,
            "claim": dossier.candidate.claim,
            "file": dossier.candidate.file,
            "line": dossier.candidate.line,
        },
        "task_patch": dossier.task.patch,
        "facts": fact_entries,
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
    llm_calls: list[int] = []
    analysis_start = time.monotonic()
    outcomes = run_bounded_parallel(
        analyzable_items,
        lambda item: analyze_relations(
            item[0], item[1],
            tag=tag_by_candidate.get(item[0].candidate.id, RiskTag.GENERAL_REVIEW),
            analyst_llm=analyst_llm,
            structured_method=structured_method,
            llm_calls=llm_calls,
        ),
        max_workers=6,
    )
    fact_analysis_ms = (time.monotonic() - analysis_start) * 1000
    for (dossier, facts), outcome in zip(analyzable_items, outcomes, strict=True):
        batch.relations[dossier.candidate.id] = (
            outcome if outcome is not None else [
                FactRelation(fact_id=fact.fact_id, relation="insufficient",
                             strength="contextual", limitation="parallel_analysis_failed")
                for fact in facts
            ]
        )
    # 重放四态与路径规划统计,供 trace 仪表盘 evidence_verifier 节点摘要渲染。
    replay_counts = {"verified": 0, "unverified": 0, "failed": 0, "recipe": 0}
    for facts in facts_by_candidate.values():
        for fact in facts:
            replay_counts[fact.replay_status] += 1
    request_count = len({
        (event_detail["tool"], _stable_json(event_detail["arguments"]))
        for event, detail in trace
        if event == "evidence_tool_called"
        for event_detail in [json.loads(detail)]
    })
    path_counts = [
        json.loads(detail).get("path")
        for event, detail in trace
        if event == "candidate_evidence_path"
    ]
    batch.trace.append(
        ("evidence_batch_metrics", _stable_json({
            "candidates": len(dossiers),
            "request_count": request_count,
            "fact_count": sum(len(v) for v in facts_by_candidate.values()),
            "replay_verified_count": replay_counts["verified"],
            "replay_unverified_count": replay_counts["unverified"],
            "replay_failed_count": replay_counts["failed"],
            "recipe_fact_count": replay_counts["recipe"],
            "chain_used": path_counts.count("chain"),
            "recipe_fallback": path_counts.count("recipe"),
            "llm_analysis_calls": len(llm_calls),
            "fact_analysis_ms": round(fact_analysis_ms, 3),
        }))
    )
    return batch
