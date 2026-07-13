"""行为正确性类风险证据策略。"""

from __future__ import annotations

from collections.abc import Callable

from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence_rules.recipes import callers_upstream, file_only
from codeguard_agent.pipeline.evidence_rules.types import (
    EvidenceStrategy,
    ToolCallSpec,
    ToolName,
)


def _strategies(
    tag: RiskTag,
    *,
    counter: str,
    support: str,
    severity: str,
    context_kinds: tuple[str, ...],
    upstream: bool = False,
) -> list[EvidenceStrategy]:
    slug = tag.value.lower()
    allowed_tools: tuple[ToolName, ...] = (
        ("get_file_content", "find_callers")
        if upstream
        else ("get_file_content",)
    )
    recipe: Callable[..., list[ToolCallSpec]] = file_only
    result = [
        EvidenceStrategy(
            f"{slug}.counter",
            frozenset({tag}),
            "counter",
            10,
            counter,
            context_kinds,
            allowed_tools,
            recipe,
        ),
        EvidenceStrategy(
            f"{slug}.support",
            frozenset({tag}),
            "support",
            20,
            support,
            context_kinds,
            allowed_tools,
            recipe,
        ),
    ]
    if upstream:
        result.append(
            EvidenceStrategy(
                f"{slug}.counter_upstream",
                frozenset({tag}),
                "counter",
                30,
                f"外层调用方是否提供以下保护：{counter}",
                context_kinds,
                ("find_callers",),
                callers_upstream,
            )
        )
    result.append(
        EvidenceStrategy(
            f"{slug}.severity",
            frozenset({tag}),
            "severity",
            40,
            severity,
            context_kinds,
            allowed_tools,
            recipe,
        )
    )
    return result


BEHAVIOR_STRATEGIES = [
    *_strategies(
        RiskTag.TRANSACTION_ATOMICITY,
        counter="当前或外层方法是否有事务或可验证补偿",
        support="路径是否包含多个可部分成功的写入/外部副作用",
        severity="部分成功规模、资金/状态影响和补偿难度是否支撑候选级别",
        context_kinds=("ast_structure", "find_callers"),
        upstream=True,
    ),
    *_strategies(
        RiskTag.CONCURRENCY_CONSISTENCY,
        counter="锁、原子结构、版本检查或线程封闭是否存在",
        support="共享可变状态是否被并发路径真实访问",
        severity="并发频率、冲突窗口和数据损坏范围是否支撑候选级别",
        context_kinds=("ast_structure", "find_callers"),
        upstream=True,
    ),
    *_strategies(
        RiskTag.IDEMPOTENCY_RETRY,
        counter="幂等键、去重记录、唯一约束或重复处理保护是否存在",
        support="是否存在重试、重复投递或重复写入触发条件",
        severity="重复触发频率、副作用规模和恢复成本是否支撑候选级别",
        context_kinds=("ast_structure", "find_callers"),
        upstream=True,
    ),
    *_strategies(
        RiskTag.CACHE_CONSISTENCY,
        counter="失效、更新、版本或锁保护是否覆盖 DB 变化",
        support="路径是否同时涉及持久化变化与缓存访问",
        severity="陈旧窗口、受影响 key/用户与错误状态持续时间是否支撑候选级别",
        context_kinds=("ast_structure", "find_callers"),
        upstream=True,
    ),
    *_strategies(
        RiskTag.MESSAGE_DELIVERY,
        counter="ack/retry/DLQ/outbox/消费去重保护是否存在",
        support="路径是否真实发布或消费消息并产生副作用",
        severity="丢失/重复消息规模、业务副作用和重放恢复成本是否支撑候选级别",
        context_kinds=("ast_structure", "find_callers"),
        upstream=True,
    ),
    *_strategies(
        RiskTag.ERROR_HANDLING,
        counter="异常是否正确传播、转换、恢复或记录",
        support="是否存在吞异常、错误映射或恢复缺口",
        severity="失败可见性、数据一致性和故障扩散范围是否支撑候选级别",
        context_kinds=("ast_structure", "find_callers"),
        upstream=True,
    ),
    *_strategies(
        RiskTag.NULL_STATE_SAFETY,
        counter="判空、非空契约、Optional 或默认值是否覆盖路径",
        support="可空值是否真实到达解引用或状态使用点",
        severity="可触发输入、请求影响范围和失败恢复成本是否支撑候选级别",
        context_kinds=("ast_structure",),
    ),
    *_strategies(
        RiskTag.RESOURCE_LIFECYCLE,
        counter="try-with-resources/finally/框架托管释放是否覆盖",
        support="是否真实获取需释放资源且存在提前退出/异常路径",
        severity="泄漏频率、资源上限与服务耗尽影响是否支撑候选级别",
        context_kinds=("ast_structure", "find_callers"),
        upstream=True,
    ),
    *_strategies(
        RiskTag.API_CONTRACT,
        counter="兼容适配、默认值、版本或调用方同步修改是否存在",
        support="请求/响应/公开签名是否真实发生不兼容变化",
        severity="调用方数量、公开范围和迁移成本是否支撑候选级别",
        context_kinds=("ast_structure", "find_callers"),
        upstream=True,
    ),
]
