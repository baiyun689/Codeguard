"""风险规则注册表与确定性任务分派。

汇总安全、行为、可维护性三条规则线的信号检测函数，对外暴露统一的 triage_tasks
和按标签查发现者的查询接口。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from codeguard_agent.models.tasks import RiskProfile, RiskSignal, RiskTag, ReviewTask
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
    profiles: dict[str, RiskProfile]
    diagnostics: tuple[RuleDiagnostic, ...]


_THREAT_MODEL = "ThreatModelAgent"
_BEHAVIOR = "BehaviorAgent"
_MAINTAINABILITY = "MaintainabilityAgent"

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

# Keep routing metadata beside the detector catalog. GENERAL_REVIEW is not a
# detector rule, but its fallback path must still have an explicit destination.
ALL_REVIEWERS = frozenset({_THREAT_MODEL, _BEHAVIOR, _MAINTAINABILITY})
RISK_TAG_REVIEWERS: dict[RiskTag, frozenset[str]] = {
    spec.tag: spec.reviewers for spec in RULE_SPECS
}
RISK_TAG_REVIEWERS[RiskTag.GENERAL_REVIEW] = ALL_REVIEWERS


def reviewers_for_tag(tag: RiskTag) -> frozenset[str]:
    """Return the fixed reviewer set for a classified risk tag."""
    return RISK_TAG_REVIEWERS[tag]


def _is_concrete_signal(signal: RiskSignal) -> bool:
    return signal.source.startswith(("text:added:", "text:deleted:", "text:changed:"))


def _profile(task_id: str, signals: list[RiskSignal]) -> RiskProfile:
    concrete_tags = {signal.tag for signal in signals if _is_concrete_signal(signal)}
    retained = [signal for signal in signals if signal.tag in concrete_tags]
    if not concrete_tags:
        fallback = RiskSignal(
            tag=RiskTag.GENERAL_REVIEW,
            score=1,
            source="fallback:unclassified",
            reason="未命中已有风险规则，执行通用审查",
        )
        return RiskProfile(task_id=task_id, tag_scores={fallback.tag: fallback.score}, signals=[fallback])

    tag_scores: dict[RiskTag, int] = {}
    for signal in retained:
        tag_scores[signal.tag] = min(5, tag_scores.get(signal.tag, 0) + signal.score)
    return RiskProfile(task_id=task_id, tag_scores=tag_scores, signals=retained)


def _classify(task: ReviewTask) -> tuple[RiskProfile, tuple[RuleDiagnostic, ...]]:
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
    return _profile(task.id, signals), tuple(diagnostics)


def classify_task(task: ReviewTask) -> RiskProfile:
    """Classify one task, retaining no diagnostic state for direct callers."""
    return _classify(task)[0]


def triage_tasks(tasks: list[ReviewTask]) -> TriageResult:
    """Classify tasks independently and retain rule failures as diagnostics."""
    profiles: dict[str, RiskProfile] = {}
    diagnostics: list[RuleDiagnostic] = []
    for task in tasks:
        profile, task_diagnostics = _classify(task)
        profiles[task.id] = profile
        diagnostics.extend(task_diagnostics)
    return TriageResult(profiles=profiles, diagnostics=tuple(diagnostics))
