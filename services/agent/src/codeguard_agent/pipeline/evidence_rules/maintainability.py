"""可维护性类风险证据策略。"""

from __future__ import annotations

from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence_rules.recipes import callers_upstream, file_metrics
from codeguard_agent.pipeline.evidence_rules.types import EvidenceStrategy, ToolName


def _strategies(
    tag: RiskTag,
    *,
    counter: str,
    support: str,
    severity: str,
    context_kinds: tuple[str, ...],
    upstream_question: str | None = None,
) -> list[EvidenceStrategy]:
    slug = tag.value.lower()
    allowed_tools: tuple[ToolName, ...] = (
        "get_file_content",
        "get_code_metrics",
    )
    result = [
        EvidenceStrategy(
            f"{slug}.counter",
            frozenset({tag}),
            "counter",
            10,
            counter,
            context_kinds,
            allowed_tools,
            file_metrics,
        ),
        EvidenceStrategy(
            f"{slug}.support",
            frozenset({tag}),
            "support",
            20,
            support,
            context_kinds,
            allowed_tools,
            file_metrics,
        ),
    ]
    if upstream_question is not None:
        result.append(
            EvidenceStrategy(
                f"{slug}.counter_upstream",
                frozenset({tag}),
                "counter",
                30,
                upstream_question,
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
            file_metrics,
        )
    )
    return result


MAINTAINABILITY_STRATEGIES = [
    *_strategies(
        RiskTag.PERFORMANCE,
        counter="分页、批处理、缓存、边界或短路是否控制成本",
        support="是否存在循环 I/O/查询、无界集合或高复杂度",
        severity="输入规模、调用频率与资源放大倍数是否支撑候选级别",
        context_kinds=("ast_structure", "get_code_metrics", "find_callers"),
        upstream_question="上游是否限制输入规模/调用频率或批量化调用以控制成本",
    ),
    *_strategies(
        RiskTag.COMPLEXITY_CONTROL_FLOW,
        counter="提取方法、早返回或封装是否实质降低复杂度",
        support="变更是否增加分支、嵌套和难推理路径",
        severity="复杂度增量、关键路径和维护/缺陷风险是否支撑候选级别",
        context_kinds=("ast_structure", "get_code_metrics"),
    ),
    *_strategies(
        RiskTag.DUPLICATION_DESIGN,
        counter="重复是否为有意隔离/专用实现或已有共享抽象",
        support="相同业务规则是否在多处重复并可能漂移",
        severity="重复位置数量、变更频率和漂移后果是否支撑候选级别",
        context_kinds=("ast_structure", "get_code_metrics"),
    ),
    *_strategies(
        RiskTag.OBSERVABILITY_TESTABILITY,
        counter="是否已有结构化日志、指标、trace、注入 seam 或测试覆盖",
        support="关键副作用/失败路径是否缺少可观测或可替换入口",
        severity="故障关键性、诊断盲区和恢复时长是否支撑候选级别",
        context_kinds=("ast_structure", "get_code_metrics", "find_callers"),
        upstream_question=(
            "上游入口是否提供覆盖当前调用的日志、指标、trace 或可替换 seam"
        ),
    ),
]
