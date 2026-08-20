"""风险规则注册表与确定性风险先验构建。

汇总安全、行为、可维护性三条规则线的信号检测函数，对外直接产出
``TaskRiskPrior``，并提供按标签查发现者的查询接口。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from codeguard_agent.models.tasks import (
    RiskCoverage,
    RiskHypothesis,
    RiskSignal,
    RiskTag,
    ReviewTask,
    TaskRiskPrior,
)
from codeguard_agent.pipeline.risk.rules.behavior import (
    detect_api_contract,
    detect_cache_consistency,
    detect_concurrency_consistency,
    detect_error_handling,
    detect_idempotency_retry,
    detect_message_delivery,
    detect_null_state_safety,
    detect_performance,
    detect_resource_lifecycle,
    detect_sql_data_access,
    detect_transaction_atomicity,
)
from codeguard_agent.pipeline.risk.rules.features import DiffFeatures, extract_features
from codeguard_agent.pipeline.risk.rules.maintainability import (
    detect_complexity_control_flow,
    detect_duplication_design,
    detect_observability_testability,
)
from codeguard_agent.pipeline.risk.rules.path import path_signals
from codeguard_agent.pipeline.risk.rules.security import (
    detect_authentication_session,
    detect_authorization,
    detect_config_security,
    detect_data_exposure,
    detect_deserialization,
    detect_file_path_io,
    detect_input_validation,
    detect_injection,
    detect_ssrf_outbound,
    detect_web_security_config,
)

RiskRule = Callable[[DiffFeatures], list[RiskSignal]]


@dataclass(frozen=True)
class RiskRuleSpec:
    rule_id: str
    tag: RiskTag
    reviewers: frozenset[str]
    detect: RiskRule


@dataclass(frozen=True)
class RuleDiagnostic:
    task_id: str
    rule_id: str
    detail: str


@dataclass(frozen=True)
class TriageResult:
    priors: dict[str, TaskRiskPrior]
    diagnostics: tuple[RuleDiagnostic, ...]


_THREAT_MODEL = "ThreatModelAgent"
_BEHAVIOR = "BehaviorAgent"
_MAINTAINABILITY = "MaintainabilityAgent"

# ── 聚合阈值(启发式默认值,标定工具 = eval-triage-off 消融档)──
# 置信度语义是"该审查方向值得重点看"的规则命中强度,不是问题成立概率。
_HYPOTHESIS_CONFIDENCE_CAP = 0.90  # 合成置信度上限:规则再一致也不给满值,留出规则不可知的余地
_PATH_ONLY_CONFIDENCE_CAP = 0.60   # 仅路径证据时上限:路径是上下文不是发现,压到"仅作参考"档位
_AMBIGUITY_CONFIDENCE_FLOOR = 0.65  # 最高假设低于此值 → AMBIGUOUS(三路全审兜底,只加不减)
_AMBIGUITY_GAP = 0.10              # 前两假设置信度差小于此值且发现者集不同 → 歧义

# Stable order is part of triage determinism. Each concrete tag has one detector spec.
RULE_SPECS: tuple[RiskRuleSpec, ...] = (
    RiskRuleSpec("authorization", RiskTag.AUTHORIZATION, frozenset({_THREAT_MODEL, _BEHAVIOR}), detect_authorization),
    RiskRuleSpec("authentication_session", RiskTag.AUTHENTICATION_SESSION, frozenset({_THREAT_MODEL, _BEHAVIOR}), detect_authentication_session),
    RiskRuleSpec("web_security_config", RiskTag.WEB_SECURITY_CONFIG, frozenset({_THREAT_MODEL}), detect_web_security_config),
    RiskRuleSpec("input_validation", RiskTag.INPUT_VALIDATION, frozenset({_THREAT_MODEL, _BEHAVIOR}), detect_input_validation),
    RiskRuleSpec("injection", RiskTag.INJECTION, frozenset({_THREAT_MODEL, _BEHAVIOR}), detect_injection),
    RiskRuleSpec("sql_data_access", RiskTag.SQL_DATA_ACCESS, frozenset({_BEHAVIOR}), detect_sql_data_access),
    RiskRuleSpec("file_path_io", RiskTag.FILE_PATH_IO, frozenset({_THREAT_MODEL, _BEHAVIOR}), detect_file_path_io),
    RiskRuleSpec("ssrf_outbound", RiskTag.SSRF_OUTBOUND, frozenset({_THREAT_MODEL, _BEHAVIOR}), detect_ssrf_outbound),
    RiskRuleSpec("config_security", RiskTag.CONFIG_SECURITY, frozenset({_THREAT_MODEL}), detect_config_security),
    RiskRuleSpec("data_exposure", RiskTag.DATA_EXPOSURE, frozenset({_THREAT_MODEL, _BEHAVIOR}), detect_data_exposure),
    RiskRuleSpec("deserialization", RiskTag.DESERIALIZATION, frozenset({_THREAT_MODEL}), detect_deserialization),
    RiskRuleSpec("transaction_atomicity", RiskTag.TRANSACTION_ATOMICITY, frozenset({_BEHAVIOR}), detect_transaction_atomicity),
    RiskRuleSpec("concurrency_consistency", RiskTag.CONCURRENCY_CONSISTENCY, frozenset({_BEHAVIOR}), detect_concurrency_consistency),
    RiskRuleSpec("idempotency_retry", RiskTag.IDEMPOTENCY_RETRY, frozenset({_BEHAVIOR}), detect_idempotency_retry),
    RiskRuleSpec("cache_consistency", RiskTag.CACHE_CONSISTENCY, frozenset({_BEHAVIOR}), detect_cache_consistency),
    RiskRuleSpec("message_delivery", RiskTag.MESSAGE_DELIVERY, frozenset({_BEHAVIOR}), detect_message_delivery),
    RiskRuleSpec("error_handling", RiskTag.ERROR_HANDLING, frozenset({_BEHAVIOR}), detect_error_handling),
    RiskRuleSpec("null_state_safety", RiskTag.NULL_STATE_SAFETY, frozenset({_BEHAVIOR}), detect_null_state_safety),
    RiskRuleSpec("resource_lifecycle", RiskTag.RESOURCE_LIFECYCLE, frozenset({_BEHAVIOR, _MAINTAINABILITY}), detect_resource_lifecycle),
    RiskRuleSpec("api_contract", RiskTag.API_CONTRACT, frozenset({_BEHAVIOR, _MAINTAINABILITY}), detect_api_contract),
    RiskRuleSpec("performance", RiskTag.PERFORMANCE, frozenset({_BEHAVIOR, _MAINTAINABILITY}), detect_performance),
    RiskRuleSpec("complexity_control_flow", RiskTag.COMPLEXITY_CONTROL_FLOW, frozenset({_MAINTAINABILITY}), detect_complexity_control_flow),
    RiskRuleSpec("duplication_design", RiskTag.DUPLICATION_DESIGN, frozenset({_MAINTAINABILITY}), detect_duplication_design),
    RiskRuleSpec("observability_testability", RiskTag.OBSERVABILITY_TESTABILITY, frozenset({_MAINTAINABILITY}), detect_observability_testability),
)

ALL_REVIEWERS = frozenset({_THREAT_MODEL, _BEHAVIOR, _MAINTAINABILITY})
RISK_TAG_REVIEWERS: dict[RiskTag, frozenset[str]] = {
    spec.tag: spec.reviewers for spec in RULE_SPECS
}


def reviewers_for_tag(tag: RiskTag) -> frozenset[str]:
    """Return the fixed reviewer set for a concrete risk hypothesis."""
    return RISK_TAG_REVIEWERS.get(tag, ALL_REVIEWERS)


def _is_concrete_signal(signal: RiskSignal) -> bool:
    return signal.source_kind != "path"


def _hypothesis(tag: RiskTag, signals: list[RiskSignal]) -> RiskHypothesis:
    confidence_remaining = 1.0
    for signal in signals:
        confidence_remaining *= 1.0 - signal.match_confidence
    confidence = 1.0 - confidence_remaining
    source_kinds = {signal.source_kind for signal in signals}
    if source_kinds == {"path"}:
        confidence = min(confidence, _PATH_ONLY_CONFIDENCE_CAP)
    strongest = max(
        signals,
        key=lambda signal: (
            signal.match_confidence,
            signal.review_priority,
            signal.source,
            signal.line or 0,
        ),
    )
    return RiskHypothesis(
        tag=tag,
        match_confidence=min(confidence, _HYPOTHESIS_CONFIDENCE_CAP),
        review_priority=max(signal.review_priority for signal in signals),
        source_kind=(
            strongest.source_kind
            if len(source_kinds) == 1
            else "diff_text"
        ),
        source="+".join(sorted({signal.source for signal in signals})),
        reason="; ".join(sorted({signal.reason for signal in signals})),
        line=strongest.line,
    )


def _prior(task_id: str, signals: list[RiskSignal]) -> TaskRiskPrior:
    concrete_tags = {signal.tag for signal in signals if _is_concrete_signal(signal)}
    retained = [signal for signal in signals if signal.tag in concrete_tags]
    if not concrete_tags:
        return TaskRiskPrior(
            task_id=task_id,
            coverage=RiskCoverage.UNCLASSIFIED,
        )

    hypotheses = [
        _hypothesis(tag, [signal for signal in retained if signal.tag is tag])
        for tag in sorted(concrete_tags, key=lambda item: item.value)
    ]
    hypotheses.sort(
        key=lambda item: (
            -item.match_confidence,
            -item.review_priority,
            item.tag.value,
        )
    )
    ambiguous = (
        max(item.match_confidence for item in hypotheses)
        < _AMBIGUITY_CONFIDENCE_FLOOR
    )
    if len(hypotheses) >= 2:
        first, second = hypotheses[:2]
        if (
            abs(first.match_confidence - second.match_confidence) < _AMBIGUITY_GAP
            and reviewers_for_tag(first.tag) != reviewers_for_tag(second.tag)
        ):
            ambiguous = True
    return TaskRiskPrior(
        task_id=task_id,
        hypotheses=tuple(hypotheses),
        coverage=RiskCoverage.AMBIGUOUS if ambiguous else RiskCoverage.CONFIDENT,
    )


def _classify(task: ReviewTask) -> tuple[TaskRiskPrior, tuple[RuleDiagnostic, ...]]:
    features = extract_features(task)
    signals: list[RiskSignal] = []
    diagnostics: list[RuleDiagnostic] = []
    seen: set[tuple[RiskTag, str, int | None, str]] = set()

    for spec in RULE_SPECS:
        try:
            detected = spec.detect(features)
        except Exception as exc:  # A broken rule must not suppress other rule results.
            diagnostics.append(RuleDiagnostic(task.id, spec.rule_id, str(exc)))
            continue
        for signal in detected:
            key = (signal.tag, signal.source, signal.line, signal.reason)
            if key not in seen:
                seen.add(key)
                signals.append(signal)

    concrete_tags = {
        signal.tag for signal in signals if _is_concrete_signal(signal)
    }
    signals.extend(path_signals(features, concrete_tags))
    return _prior(task.id, signals), tuple(diagnostics)


def triage_tasks(
    tasks: list[ReviewTask], *, rules_enabled: bool = True
) -> TriageResult:
    """Classify tasks independently and retain rule failures as diagnostics.

    rules_enabled=False(triage 消融档):不跑任何规则,全部 UNCLASSIFIED——
    下游 routing 因此走三路全审 baseline、无 ReAct 升格,用于量化
    风险先验的净收益。
    """
    if not rules_enabled:
        return TriageResult(
            priors={
                task.id: TaskRiskPrior(
                    task_id=task.id, coverage=RiskCoverage.UNCLASSIFIED
                )
                for task in tasks
            },
            diagnostics=(),
        )
    priors: dict[str, TaskRiskPrior] = {}
    diagnostics: list[RuleDiagnostic] = []
    for task in tasks:
        prior, task_diagnostics = _classify(task)
        priors[task.id] = prior
        diagnostics.extend(task_diagnostics)
    return TriageResult(priors=priors, diagnostics=tuple(diagnostics))
