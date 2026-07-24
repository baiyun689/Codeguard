"""High-recall, deterministic security risk signals for one diff task."""

from __future__ import annotations

import re
from codeguard_agent.models.tasks import RiskSignal, RiskTag
from codeguard_agent.pipeline.risk.rules.features import DiffFeatures


_Rule = tuple[str, re.Pattern[str]]


def _match(lines: tuple[str, ...] | tuple[tuple[int, str], ...], pattern: re.Pattern[str]):
    for item in lines:
        line, text = item if isinstance(item, tuple) else (None, item)
        match = pattern.search(text)
        if match:
            token = match.groupdict().get("token") or match.group(0)
            token = re.sub(r"\s+", " ", token).strip(" '\"")[:40]
            return line, text, token
    return None


def _signal(
    features: DiffFeatures,
    tag: RiskTag,
    rule: _Rule,
    reason: str,
    *,
    deleted_score: int = 2,
    added_score: int = 1,
    changed_score: int | None = None,
) -> list[RiskSignal]:
    rule_id, pattern = rule
    added = _match(features.added_lines, pattern)
    deleted = _match(features.deleted_lines, pattern)
    if added and deleted and added[2] == deleted[2] and added[1] != deleted[1]:
        score = changed_score if changed_score is not None else max(added_score, deleted_score)
        source = f"text:changed:{rule_id}"
        line = added[0]
        return [RiskSignal(tag=tag, score=score, source=source, reason=f"{reason}：命中 {added[2]}，需审查", line=line)]
    signals: list[RiskSignal] = []
    if added:
        signals.append(
            RiskSignal(
                tag=tag,
                score=added_score,
                source=f"text:added:{rule_id}",
                reason=f"{reason}：命中 {added[2]}，需审查",
                line=added[0],
            )
        )
    if deleted:
        signals.append(
            RiskSignal(
                tag=tag,
                score=deleted_score,
                source=f"text:deleted:{rule_id}",
                reason=f"{reason}：命中 {deleted[2]}，需审查",
                line=None,
            )
        )
    return signals


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
        signals.extend(_signal(
            features,
            tag,
            rule,
            reason,
            deleted_score=deleted_score,
            added_score=added_score,
            changed_score=changed_score,
        ))
    return signals


def _rule(rule_id: str, *parts: str) -> _Rule:
    return rule_id, re.compile("|".join(parts), re.IGNORECASE)


def detect_authorization(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.AUTHORIZATION,
        (_rule("authorization_guard", r"@PreAuthorize\b", r"@Secured\b", r"hasRole\s*\(", r"hasAuthority\s*\("),),
        "鉴权保护变化",
        deleted_score=3,
        changed_score=3,
    )


def detect_authentication_session(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.AUTHENTICATION_SESSION,
        (_rule("authentication_session", r"Authentication\b", r"SecurityContext\b", r"BearerToken\b", r"JSESSIONID\b", r"OAuth2\b", r"\b(login|logout)\b"),),
        "认证或会话变化",
        deleted_score=3,
        changed_score=3,
    )


def detect_web_security_config(features: DiffFeatures) -> list[RiskSignal]:
    weakening = _detect(
        features,
        RiskTag.WEB_SECURITY_CONFIG,
        (
            _rule("web_security_weakening", r"csrf\s*\(\s*\)\s*\.\s*disable\s*\(\s*\)", r"permitAll\b", r"anonymous\b"),
        ),
        "Web 安全配置变化",
        deleted_score=3,
        added_score=3,
        changed_score=3,
    )
    config = _detect(
        features,
        RiskTag.WEB_SECURITY_CONFIG,
        (_rule("web_security_config", r"\bcsrf\b", r"\bcors\b", r"\bActuator\b", r"management\.endpoints"),),
        "Web 安全配置变化",
        deleted_score=2,
        added_score=1,
        changed_score=2,
    )
    return weakening or config


def detect_input_validation(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.INPUT_VALIDATION,
        (_rule("input_validation", r"@Valid\b", r"@Validated\b", r"@(NotNull|NotBlank|Size)\b", r"BindingResult\b", r"\.isBlank\s*\("),),
        "输入校验变化",
        deleted_score=3,
        changed_score=3,
    )


def detect_injection(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.INJECTION,
        (
            _rule("injection_sql", r"(?=.*(?:\+|format\s*\())(?:select|insert|update|delete)\b"),
            _rule("injection_command", r"\"(?P<token>sh\s+-c|bash|cmd|powershell)[^\"]*\"\s*\+", r"Runtime\.getRuntime\(\)\.exec", r"ProcessBuilder\b"),
            _rule("injection_template", r"createNativeQuery\b", r"\$\{[^}]+\}"),
        ),
        "外部输入进入执行或查询的变化",
        added_score=3,
        deleted_score=2,
        changed_score=3,
    )


def detect_file_path_io(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.FILE_PATH_IO,
        (_rule("file_path_io", r"Files\.readString", r"Paths\.get\b", r"new\s+File\b", r"Files\.", r"FileInputStream\b", r"MultipartFile\b", r"ZipInputStream\b"),),
        "文件路径或输入输出边界变化",
        added_score=2,
        deleted_score=2,
        changed_score=2,
    )


def detect_ssrf_outbound(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.SSRF_OUTBOUND,
        (_rule("ssrf_outbound", r"RestTemplate\b", r"WebClient\b", r"HttpClient\b", r"\bURL\b", r"\bURI\b", r"Feign\b"),),
        "出站请求或 URL 构造变化",
        added_score=3,
        deleted_score=2,
        changed_score=3,
    )


def detect_config_security(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.CONFIG_SECURITY,
        (
            _rule("config_security", r"@Value\b", r"\b(password|secret|token|apiKey)\b", r"application\.(yml|yaml|properties)\b"),
            _rule("config_hardcoded_key", r'''(?:static\s+)?(?:final\s+)?String\s+\w*(?:key|secret|password|token)\w*\s*=\s*"[^"]{8,}"'''),
            _rule("config_weak_crypto", r'''(?:MessageDigest|Cipher|KeyGenerator|SecretKeyFactory|Mac)\.getInstance\s*\(\s*"(?:DES|MD5|SHA-?1|RC4|Blowfish)"''', r'\b(?:DES|MD5|SHA-?1|RC4|Blowfish)\b'),
        ),
        "敏感配置或凭据暴露变化",
        added_score=3,
        deleted_score=2,
        changed_score=3,
    )


def detect_deserialization(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.DESERIALIZATION,
        (_rule("deserialization",
            r"ObjectInputStream\b",
            r"\.readObject\s*\(",
            r"\.readUnshared\s*\(",
            r"XMLDecoder\b",
            r"XStream\b",
            r"Kryo\b",
            r"\.readResolve\s*\(",
            r"\.readExternal\s*\(",
            r"ObjectInput\b",
        ),),
        "不可信反序列化变化",
        added_score=3,
        deleted_score=2,
        changed_score=3,
    )


def detect_data_exposure(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.DATA_EXPOSURE,
        (_rule("data_exposure", r"\b(password|token|email|phone)\b", r"ResponseEntity\b", r"\bDTO\b", r"\b(return|log|logger)\b[^\n]*(?:toString|password|token|email|phone)", r"toString\s*\("),),
        "敏感数据输出或脱敏变化",
        added_score=3,
        deleted_score=3,
        changed_score=3,
    )
