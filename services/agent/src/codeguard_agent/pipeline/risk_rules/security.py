"""High-recall, deterministic security risk signals for one diff task."""

from __future__ import annotations

import re
from codeguard_agent.models.tasks import RiskSignal, RiskTag
from codeguard_agent.pipeline.risk_rules.features import DiffFeatures


_Rule = tuple[str, re.Pattern[str]]


def _signal(
    features: DiffFeatures,
    tag: RiskTag,
    rule: _Rule,
    reason: str,
    *,
    deleted_score: int = 2,
    added_score: int = 1,
    changed_score: int | None = None,
) -> RiskSignal | None:
    rule_id, pattern = rule
    added = next(((line, text) for line, text in features.added_lines if pattern.search(text)), None)
    deleted = any(pattern.search(text) for text in features.deleted_lines)
    if added and deleted:
        score = changed_score if changed_score is not None else max(added_score, deleted_score)
        source = f"text:changed:{rule_id}"
        line = added[0]
    elif added:
        score = added_score
        source = f"text:added:{rule_id}"
        line = added[0]
    elif deleted:
        score = deleted_score
        source = f"text:deleted:{rule_id}"
        line = None
    else:
        return None
    return RiskSignal(tag=tag, score=score, source=source, reason=reason, line=line)


def _detect(
    features: DiffFeatures,
    tag: RiskTag,
    rules: tuple[_Rule, ...],
    reason: str,
    *,
    deleted_score: int = 2,
    added_score: int = 1,
    changed_score: int | None = None,
) -> list[RiskSignal]:
    signals: list[RiskSignal] = []
    for rule in rules:
        signal = _signal(
            features,
            tag,
            rule,
            reason,
            deleted_score=deleted_score,
            added_score=added_score,
            changed_score=changed_score,
        )
        if signal is not None:
            signals.append(signal)
    return signals


def _rule(rule_id: str, *parts: str) -> _Rule:
    return rule_id, re.compile("|".join(parts), re.IGNORECASE)


def detect_authorization(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.AUTHORIZATION,
        (_rule("authorization", r"@PreAuthorize\b", r"@Secured\b", r"hasRole\s*\(", r"hasAuthority\s*\("),),
        "命中鉴权保护变化，需审查",
        deleted_score=3,
        changed_score=3,
    )


def detect_authentication_session(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.AUTHENTICATION_SESSION,
        (_rule("authentication_session", r"Authentication\b", r"SecurityContext\b", r"BearerToken\b", r"JSESSIONID\b", r"OAuth2\b", r"\b(login|logout)\b"),),
        "命中认证或会话变化，需审查",
        deleted_score=3,
        changed_score=3,
    )


def detect_web_security_config(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.WEB_SECURITY_CONFIG,
        (_rule("web_security_config", r"\bcsrf\b", r"\bcors\b", r"permitAll\b", r"anonymous\b", r"\bActuator\b", r"management\.endpoints"),),
        "命中 Web 安全配置变化，需审查",
        deleted_score=3,
        changed_score=3,
    )


def detect_input_validation(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.INPUT_VALIDATION,
        (_rule("input_validation", r"@Valid\b", r"@Validated\b", r"@(NotNull|NotBlank|Size)\b", r"BindingResult\b", r"\.isBlank\s*\("),),
        "命中输入校验变化，需审查",
        deleted_score=3,
        changed_score=3,
    )


def detect_injection(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.INJECTION,
        (
            _rule("injection", r"Runtime\.getRuntime\(\)\.exec", r"ProcessBuilder\b", r"createNativeQuery\b", r"\$\{[^}]+\}", r"(?:select|insert|update|delete)\b[^\n]*(?:\+|format\s*\()"),
        ),
        "命中外部输入进入执行或查询拼接的变化，需审查",
        added_score=3,
        deleted_score=2,
        changed_score=3,
    )


def detect_file_path_io(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.FILE_PATH_IO,
        (_rule("file_path_io", r"Paths\.get\b", r"new\s+File\b", r"Files\.", r"FileInputStream\b", r"MultipartFile\b", r"ZipInputStream\b"),),
        "命中文件路径或输入输出边界变化，需审查",
        added_score=2,
        deleted_score=2,
        changed_score=2,
    )


def detect_ssrf_outbound(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.SSRF_OUTBOUND,
        (_rule("ssrf_outbound", r"RestTemplate\b", r"WebClient\b", r"HttpClient\b", r"\bURL\b", r"\bURI\b", r"Feign\b"),),
        "命中出站请求或 URL 构造变化，需审查",
        added_score=3,
        deleted_score=2,
        changed_score=3,
    )


def detect_config_security(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.CONFIG_SECURITY,
        (_rule("config_security", r"\b(password|secret|token|apiKey)\b", r"@Value\b", r"application\.(yml|yaml|properties)\b"),),
        "命中敏感配置或凭据暴露变化，需审查",
        added_score=3,
        deleted_score=2,
        changed_score=3,
    )


def detect_data_exposure(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.DATA_EXPOSURE,
        (_rule("data_exposure", r"ResponseEntity\b", r"\bDTO\b", r"\b(return|log|logger)\b[^\n]*(?:toString|password|token|email|phone)", r"toString\s*\(", r"\b(password|token|email|phone)\b"),),
        "命中敏感数据输出或脱敏变化，需审查",
        added_score=3,
        deleted_score=3,
        changed_score=3,
    )
