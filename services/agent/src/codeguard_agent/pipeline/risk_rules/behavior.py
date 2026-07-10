"""High-recall, deterministic behavior risk signals for one diff task."""

from __future__ import annotations

import re

from codeguard_agent.models.tasks import RiskSignal, RiskTag
from codeguard_agent.pipeline.risk_rules.features import DiffFeatures


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


def _detect(
    features: DiffFeatures,
    tag: RiskTag,
    rule: _Rule,
    reason: str,
    *,
    added_score: int = 2,
    deleted_score: int = 2,
    changed_score: int | None = None,
) -> list[RiskSignal]:
    rule_id, pattern = rule
    added = _match(features.added_lines, pattern)
    deleted = _match(features.deleted_lines, pattern)
    if added and deleted and added[2] == deleted[2] and added[1] != deleted[1]:
        score = changed_score if changed_score is not None else max(added_score, deleted_score)
        return [
            RiskSignal(
                tag=tag,
                score=score,
                source=f"text:changed:{rule_id}",
                reason=f"{reason}：命中 {added[2]}，需审查",
                line=added[0],
            )
        ]

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


def _rule(rule_id: str, *parts: str) -> _Rule:
    return rule_id, re.compile("|".join(parts), re.IGNORECASE)


def detect_sql_data_access(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.SQL_DATA_ACCESS,
        _rule("sql_data_access", r"@Query\b", r"@Select\b", r"JdbcTemplate\b", r"\bMapper\b", r"\b(SELECT|UPDATE|DELETE)\b", r"\bwhere\b", r"\bfindAll\b"),
        "数据访问或查询变化",
    )


def detect_transaction_atomicity(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.TRANSACTION_ATOMICITY,
        _rule("transaction_atomicity", r"@Transactional\b", r"\b(save|update|delete|insert)\b", r"EntityManager\b", r"\b(commit|rollback)\b"),
        "事务边界或数据写入变化",
        deleted_score=3,
        changed_score=3,
    )


def detect_concurrency_consistency(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.CONCURRENCY_CONSISTENCY,
        _rule("concurrency_consistency", r"\bsynchronized\b", r"\bLock\b", r"\bAtomic\w*\b", r"\bConcurrent\w*\b", r"@Version\b", r"\bfor\s+update\b", r"\bwhere\b[^\n]*\bversion\b"),
        "并发控制或一致性变化",
    )


def detect_idempotency_retry(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.IDEMPOTENCY_RETRY,
        _rule("idempotency_retry", r"\bidempot\w*\b", r"\brequestId\b", r"\bdedup\w*\b", r"\bsetIfAbsent\b", r"\bSETNX\b", r"@Retryable\b", r"\b(retry|repeated\s+consumption)\b"),
        "幂等、重试或重复消费变化",
    )


def detect_cache_consistency(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.CACHE_CONSISTENCY,
        _rule("cache_consistency", r"@Cacheable\b", r"@CacheEvict\b", r"RedisTemplate\b", r"Caffeine\b", r"\b(cache|invalidate|evict)\b"),
        "缓存读写或失效变化",
    )


def detect_message_delivery(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.MESSAGE_DELIVERY,
        _rule("message_delivery", r"@KafkaListener\b", r"@RabbitListener\b", r"\b(ack|nack|offset|deadLetter|retry)\b"),
        "消息消费或投递保证变化",
    )


def detect_error_handling(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.ERROR_HANDLING,
        _rule("error_handling", r"\bcatch\b", r"\bthrows\b", r"\bfinally\b", r"@ExceptionHandler\b", r"catch\s*\([^)]*\)\s*\{\s*\}"),
        "异常处理或错误传播变化",
        deleted_score=3,
        changed_score=3,
    )


def detect_null_state_safety(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.NULL_STATE_SAFETY,
        _rule("null_state_safety", r"\bnull\b", r"\bOptional\b", r"Objects\.requireNonNull\b", r"\borElse\b", r"\bget\s*\(\s*\)"),
        "空值或状态安全变化",
    )


def detect_resource_lifecycle(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.RESOURCE_LIFECYCLE,
        _rule("resource_lifecycle", r"\bConnection\b", r"\bInputStream\b", r"\bExecutorService\b", r"\bThread\b", r"\bLock\b", r"(?P<token>\b\w+\.(?:close|shutdown))\s*\(", r"\b(close|shutdown)\s*\(\s*\)"),
        "资源获取、释放或线程生命周期变化",
        deleted_score=3,
        changed_score=3,
    )


def detect_api_contract(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.API_CONTRACT,
        _rule("api_contract", r"@RequestMapping\b", r"@(Get|Post|Put|Delete)Mapping\b", r"\bController\b", r"\bDTO\b", r"\bpublic\b", r"@Json\w*\b"),
        "接口映射、输入输出或 JSON 契约变化",
    )


def detect_performance(features: DiffFeatures) -> list[RiskSignal]:
    return _detect(
        features,
        RiskTag.PERFORMANCE,
        _rule("performance", r"(?=.*(?P<token>\bfindAll\b))", r"\bselect\s+\*\b", r"\bN\+1\b", r"\b(stream|parallelStream)\s*\(", r"\bThread\.sleep\b", r"\b(for|while)\b[^\n]*(?:find|select|read|write|fetch)"),
        "循环中的查询、I/O 或重复计算变化",
    )
