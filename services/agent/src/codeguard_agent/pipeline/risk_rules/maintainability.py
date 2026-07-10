"""High-recall, deterministic maintainability risk signals for one diff task."""

from __future__ import annotations

import re
from collections import Counter

from codeguard_agent.models.tasks import RiskSignal, RiskTag
from codeguard_agent.pipeline.risk_rules.features import DiffFeatures


_Rule = tuple[str, re.Pattern[str]]


def _match(lines: tuple[str, ...] | tuple[tuple[int, str], ...], pattern: re.Pattern[str]):
    for item in lines:
        line, text = item if isinstance(item, tuple) else (None, item)
        match = pattern.search(text)
        if match:
            token = re.sub(r"\s+", " ", match.group(0)).strip(" '\"")[:40]
            return line, text, token
    return None


def _detect(features: DiffFeatures, tag: RiskTag, rule: _Rule, reason: str, *, deleted_score: int = 1) -> list[RiskSignal]:
    rule_id, pattern = rule
    added = _match(features.added_lines, pattern)
    deleted = _match(features.deleted_lines, pattern)
    if added and deleted and added[2] == deleted[2] and added[1] != deleted[1]:
        return [RiskSignal(tag=tag, score=max(1, deleted_score), source=f"text:changed:{rule_id}", reason=f"{reason}：命中 {added[2]}，需审查", line=added[0])]

    signals: list[RiskSignal] = []
    if added:
        signals.append(RiskSignal(tag=tag, score=1, source=f"text:added:{rule_id}", reason=f"{reason}：命中 {added[2]}，需审查", line=added[0]))
    if deleted:
        signals.append(RiskSignal(tag=tag, score=deleted_score, source=f"text:deleted:{rule_id}", reason=f"{reason}：命中 {deleted[2]}，需审查", line=None))
    return signals


def _rule(rule_id: str, *parts: str) -> _Rule:
    return rule_id, re.compile("|".join(parts), re.IGNORECASE)


def detect_complexity_control_flow(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.COMPLEXITY_CONTROL_FLOW,
        _rule("complexity_control_flow", r"\b(if|for|while|switch|try|catch)\b", r"\b(return|break|continue|throw)\b", r"&&|\|\|"),
        "控制流或条件复杂度变化",
    )


def _repeated_statement(lines: tuple[str, ...] | tuple[tuple[int, str], ...]):
    statements: list[tuple[int | None, str, str]] = []
    for item in lines:
        line, text = item if isinstance(item, tuple) else (None, item)
        for statement in text.split(";"):
            normalized = re.sub(r"\s+", " ", statement).strip()
            if normalized:
                statements.append((line, text, normalized))
    counts = Counter(statement for _, _, statement in statements)
    for line, text, statement in statements:
        if counts[statement] > 1:
            return line, text, statement[:40]
    return None


def detect_duplication_design(features: DiffFeatures) -> list[RiskSignal]:
    added = _repeated_statement(features.added_lines)
    deleted = _repeated_statement(features.deleted_lines)
    if added and deleted and added[2] == deleted[2] and added[1] != deleted[1]:
        return [RiskSignal(tag=RiskTag.DUPLICATION_DESIGN, score=1, source="text:changed:duplication_design", reason=f"重复设计或调用块变化：命中 {added[2]}，需审查", line=added[0])]

    signals: list[RiskSignal] = []
    if added:
        signals.append(RiskSignal(tag=RiskTag.DUPLICATION_DESIGN, score=1, source="text:added:duplication_design", reason=f"重复设计或调用块变化：命中 {added[2]}，需审查", line=added[0]))
    if deleted:
        signals.append(RiskSignal(tag=RiskTag.DUPLICATION_DESIGN, score=1, source="text:deleted:duplication_design", reason=f"重复设计或调用块变化：命中 {deleted[2]}，需审查", line=None))
    return signals


def detect_observability_testability(features: DiffFeatures) -> list[RiskSignal]:
    protection = _detect(
        features,
        RiskTag.OBSERVABILITY_TESTABILITY,
        _rule("observability_protection", r"\b(?:logger|log|metric|audit)\s*\.\s*\w+", r"\b(test|assert)\w*\s*\("),
        "日志、指标、审计或测试保护变化",
        deleted_score=3,
    )
    side_effect = _detect(
        features,
        RiskTag.OBSERVABILITY_TESTABILITY,
        _rule("observability_side_effect", r"\b\w*(Service|Repository|Client)\s*\.\s*\w+", r"\b(send|publish|save|delete|update)\s*\("),
        "新增副作用需关注可观测性",
    )
    return protection or side_effect
