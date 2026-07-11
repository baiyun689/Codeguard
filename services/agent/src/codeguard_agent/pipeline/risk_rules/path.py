"""Weak path-role signals for deterministic risk triage."""

from __future__ import annotations

from collections.abc import Iterable

from codeguard_agent.models.tasks import RiskSignal, RiskTag
from codeguard_agent.pipeline.risk_rules.features import DiffFeatures


# A path role is context, not a finding. Keep the mapping deliberately small
# and explicit so a filename can only strengthen a matching text signal.
_ROLE_TAGS: tuple[tuple[str, tuple[str, ...], tuple[RiskTag, ...]], ...] = (
    (
        "controller",
        ("controller",),
        (
            RiskTag.AUTHORIZATION,
            RiskTag.INPUT_VALIDATION,
            RiskTag.API_CONTRACT,
            RiskTag.DATA_EXPOSURE,
        ),
    ),
    (
        "repository",
        ("repository", "repositories", "mapper", "mappers", "dao", "daos"),
        (
            RiskTag.SQL_DATA_ACCESS,
            RiskTag.TRANSACTION_ATOMICITY,
            RiskTag.PERFORMANCE,
        ),
    ),
    (
        "config",
        ("config", "configuration", "application.yml", "application.yaml", "application.properties"),
        (RiskTag.WEB_SECURITY_CONFIG, RiskTag.CONFIG_SECURITY),
    ),
    (
        "consumer",
        ("consumer", "consumers", "listener", "listeners"),
        (
            RiskTag.MESSAGE_DELIVERY,
            RiskTag.IDEMPOTENCY_RETRY,
            RiskTag.ERROR_HANDLING,
        ),
    ),
    (
        "service",
        ("service", "services"),
        (
            RiskTag.TRANSACTION_ATOMICITY,
            RiskTag.CONCURRENCY_CONSISTENCY,
            RiskTag.IDEMPOTENCY_RETRY,
            RiskTag.CACHE_CONSISTENCY,
        ),
    ),
)


def _matches_role(path: str, markers: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/").lower()
    parts = normalized.split("/")
    filename = parts[-1] if parts else normalized
    return any(marker in parts or filename == marker for marker in markers)


def path_signals(
    features: DiffFeatures, concrete_tags: Iterable[RiskTag]
) -> list[RiskSignal]:
    """Return score-1 path evidence only for already matched concrete tags."""
    concrete = set(concrete_tags)
    signals: list[RiskSignal] = []
    for role, markers, tags in _ROLE_TAGS:
        if not _matches_role(features.path, markers):
            continue
        for tag in tags:
            if tag in concrete:
                signals.append(
                    RiskSignal(
                        tag=tag,
                        score=1,
                        source=f"path:{role}",
                        reason=f"文件路径角色 {role} 与该风险方向相关，作为弱证据加权",
                    )
                )
    return signals
