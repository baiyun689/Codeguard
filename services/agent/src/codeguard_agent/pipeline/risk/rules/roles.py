"""文件路径角色 → RiskTag 的单一注册表。

统一此前 path.py 的 `_ROLE_TAGS`(弱路径信号)与 knowledge/selector.py 的
`_FILE_ROLE_TOPICS`(知识选择 file-role 评分)两套映射——此前内容漂移
(如 consumer 只存在于 path 侧、worker 只存在于 selector 侧),现在
增删角色/标签只动这一张表。

角色是上下文不是发现:命中角色只做加权/评分,不单独产生风险结论。
"""

from __future__ import annotations

from dataclasses import dataclass

from codeguard_agent.models.tasks import RiskTag


@dataclass(frozen=True)
class FileRoleSpec:
    """一个文件路径角色的匹配方式与相关审查角度。"""

    role: str
    markers: tuple[str, ...] = ()   # 命中:路径片段 / 文件名精确 / 去扩展名后缀
    tags: tuple[RiskTag, ...] = ()
    contains: tuple[str, ...] = ()  # 额外子串启发(仅保留的 legacy 宽松规则)


# 每角色的标签集合 = 原两套映射的并集(见 docstring 合并说明)。
FILE_ROLE_SPECS: tuple[FileRoleSpec, ...] = (
    FileRoleSpec("controller", ("controller",), (
        RiskTag.AUTHORIZATION, RiskTag.INPUT_VALIDATION, RiskTag.API_CONTRACT,
        RiskTag.AUTHENTICATION_SESSION, RiskTag.DATA_EXPOSURE,
    )),
    FileRoleSpec("filter", ("filter",), (
        RiskTag.AUTHORIZATION, RiskTag.AUTHENTICATION_SESSION, RiskTag.INPUT_VALIDATION,
    )),
    FileRoleSpec("interceptor", ("interceptor",), (
        RiskTag.AUTHORIZATION, RiskTag.AUTHENTICATION_SESSION,
    )),
    FileRoleSpec("repository", ("repository", "repositories"), (
        RiskTag.SQL_DATA_ACCESS, RiskTag.TRANSACTION_ATOMICITY, RiskTag.PERFORMANCE,
    )),
    FileRoleSpec("mapper", ("mapper", "mappers"), (RiskTag.SQL_DATA_ACCESS,)),
    FileRoleSpec("dao", ("dao", "daos"), (RiskTag.SQL_DATA_ACCESS,)),
    FileRoleSpec("service", ("service", "services"), (
        RiskTag.TRANSACTION_ATOMICITY, RiskTag.CONCURRENCY_CONSISTENCY,
        RiskTag.IDEMPOTENCY_RETRY, RiskTag.CACHE_CONSISTENCY,
        RiskTag.ERROR_HANDLING, RiskTag.NULL_STATE_SAFETY,
    )),
    FileRoleSpec("config", (
        "config", "configuration",
        "application.yml", "application.yaml", "application.properties",
    ), (
        RiskTag.WEB_SECURITY_CONFIG, RiskTag.CONFIG_SECURITY, RiskTag.RESOURCE_LIFECYCLE,
    )),
    FileRoleSpec("security", (), (
        RiskTag.AUTHORIZATION, RiskTag.AUTHENTICATION_SESSION, RiskTag.CONFIG_SECURITY,
    ), ("security", "auth")),
    FileRoleSpec("worker", ("worker", "workers"), (
        RiskTag.MESSAGE_DELIVERY, RiskTag.IDEMPOTENCY_RETRY, RiskTag.CONCURRENCY_CONSISTENCY,
    )),
    FileRoleSpec("consumer", ("consumer", "consumers"), (
        RiskTag.MESSAGE_DELIVERY, RiskTag.IDEMPOTENCY_RETRY, RiskTag.ERROR_HANDLING,
    )),
    FileRoleSpec("listener", ("listener", "listeners"), (
        RiskTag.MESSAGE_DELIVERY, RiskTag.IDEMPOTENCY_RETRY,
    )),
    FileRoleSpec("event", (), (
        RiskTag.MESSAGE_DELIVERY, RiskTag.TRANSACTION_ATOMICITY,
    ), ("event",)),
    FileRoleSpec("cache", (), (
        RiskTag.CACHE_CONSISTENCY, RiskTag.PERFORMANCE,
    ), ("cache",)),
    FileRoleSpec("dto", ("dto",), (RiskTag.DATA_EXPOSURE,)),
    FileRoleSpec("entity", ("entity", "entities"), (
        RiskTag.DATA_EXPOSURE, RiskTag.SQL_DATA_ACCESS,
    )),
    FileRoleSpec("util", ("util", "utils"), (
        RiskTag.NULL_STATE_SAFETY, RiskTag.PERFORMANCE,
    )),
    FileRoleSpec("test", ("test", "tests"), (RiskTag.OBSERVABILITY_TESTABILITY,)),
)


def _normalize(path: str) -> tuple[str, ...]:
    return tuple(
        part for part in (path or "").replace("\\", "/").lower().split("/") if part
    )


def matching_roles(path: str) -> tuple[FileRoleSpec, ...]:
    """路径命中的角色(可多角色命中),按注册表顺序返回。"""
    parts = _normalize(path)
    filename = parts[-1] if parts else ""
    base = filename.rsplit(".", 1)[0] if "." in filename else filename
    lowered = (path or "").replace("\\", "/").lower()
    matched: list[FileRoleSpec] = []
    for spec in FILE_ROLE_SPECS:
        marker_hit = any(
            base.endswith(marker) or marker in parts or filename == marker
            for marker in spec.markers
        )
        if marker_hit or any(item in lowered for item in spec.contains):
            matched.append(spec)
    return tuple(matched)


def path_tags(path: str) -> tuple[RiskTag, ...]:
    """路径命中角色的相关标签并集(保持注册表顺序,去重)。"""
    tags: list[RiskTag] = []
    seen: set[RiskTag] = set()
    for spec in matching_roles(path):
        for tag in spec.tags:
            if tag not in seen:
                seen.add(tag)
                tags.append(tag)
    return tuple(tags)
