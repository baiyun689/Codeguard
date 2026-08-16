"""证据验证节点:链校验、固定配方兜底、重放执行与关系分析(ADR-046)。

取证层是确定性的通用事实采集:链校验/配方/去重/重放全部零 LLM;
LLM 只做关系分析(理解事实)与终审裁决(见 council/verdict.py)。
"""

from __future__ import annotations

import json
import logging

from codeguard_agent.models.schemas import EvidenceTraceStep
from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence.planner import CandidateDossier

logger = logging.getLogger("codeguard")

CHAIN_TOOL_NAMES = (
    "get_file_content",
    "inspect_change_impact",
    "inspect_security_path",
    "inspect_structure",
)
MAX_CHAIN_STEPS = 3
_FILE_ARG = "file_path"
_SYMBOL_ARG = "symbol_id"

# 配方开关:安全敏感标签加安全路径,维护性标签加结构指标(确定性,零 LLM)。
SECURITY_TAGS = frozenset({
    RiskTag.AUTHORIZATION, RiskTag.AUTHENTICATION_SESSION,
    RiskTag.WEB_SECURITY_CONFIG, RiskTag.INPUT_VALIDATION,
    RiskTag.INJECTION, RiskTag.SQL_DATA_ACCESS, RiskTag.FILE_PATH_IO,
    RiskTag.SSRF_OUTBOUND, RiskTag.CONFIG_SECURITY, RiskTag.DATA_EXPOSURE,
    RiskTag.DESERIALIZATION,
})
MAINTAINABILITY_TAGS = frozenset({
    RiskTag.COMPLEXITY_CONTROL_FLOW, RiskTag.DUPLICATION_DESIGN,
    RiskTag.OBSERVABILITY_TESTABILITY,
})


def validate_chain(
    steps: list[EvidenceTraceStep] | tuple[EvidenceTraceStep, ...],
) -> tuple[EvidenceTraceStep, ...]:
    """确定性形状校验:工具四选一、参数键合法、located 必填、链长 ≤3。"""
    valid: list[EvidenceTraceStep] = []
    for step in steps[:MAX_CHAIN_STEPS]:
        if step.tool not in CHAIN_TOOL_NAMES:
            continue
        expected = _FILE_ARG if step.tool == "get_file_content" else _SYMBOL_ARG
        if not step.args.get(expected) or not step.located.strip():
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
