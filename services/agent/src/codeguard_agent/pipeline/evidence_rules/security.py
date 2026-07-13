"""安全类风险证据策略。"""

from __future__ import annotations

from collections.abc import Callable

from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence_rules.recipes import (
    callers_upstream,
    file_only,
    file_sensitive,
)
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
    allowed_tools: tuple[ToolName, ...],
    recipe: Callable[..., list[ToolCallSpec]],
    upstream: bool = False,
) -> list[EvidenceStrategy]:
    slug = tag.value.lower()
    result = [
        EvidenceStrategy(
            id=f"{slug}.counter",
            tags=frozenset({tag}),
            purpose="counter",
            priority=10,
            question_template=counter,
            context_kinds=context_kinds,
            allowed_tools=allowed_tools,
            build_tool_calls=recipe,
        ),
        EvidenceStrategy(
            id=f"{slug}.support",
            tags=frozenset({tag}),
            purpose="support",
            priority=20,
            question_template=support,
            context_kinds=context_kinds,
            allowed_tools=allowed_tools,
            build_tool_calls=recipe,
        ),
    ]
    if upstream:
        result.append(
            EvidenceStrategy(
                id=f"{slug}.counter_upstream",
                tags=frozenset({tag}),
                purpose="counter",
                priority=30,
                question_template=f"外层调用方是否提供以下保护：{counter}",
                context_kinds=context_kinds,
                allowed_tools=("find_callers",),
                build_tool_calls=callers_upstream,
            )
        )
    result.append(
        EvidenceStrategy(
            id=f"{slug}.severity",
            tags=frozenset({tag}),
            purpose="severity",
            priority=40,
            question_template=severity,
            context_kinds=context_kinds,
            allowed_tools=allowed_tools,
            build_tool_calls=recipe,
        )
    )
    return result


SECURITY_STRATEGIES = [
    *_strategies(
        RiskTag.AUTHORIZATION,
        counter="当前方法、类或调用方是否已有鉴权或资源归属校验，足以排除越权",
        support="路径是否真实执行敏感操作或访问受保护资源",
        severity="未授权路径的可达性、受保护资源敏感度和影响用户范围是否支撑候选级别",
        context_kinds=("sensitive_api", "ast_structure", "find_callers"),
        allowed_tools=("get_file_content", "find_sensitive_apis", "find_callers"),
        recipe=file_sensitive,
        upstream=True,
    ),
    *_strategies(
        RiskTag.AUTHENTICATION_SESSION,
        counter="token/session 是否已校验有效期、撤销状态和主体绑定",
        support="变更是否真实影响认证凭据或会话生命周期",
        severity="可利用会话范围、凭据敏感度和账户影响面是否支撑候选级别",
        context_kinds=("ast_structure", "find_callers"),
        allowed_tools=("get_file_content", "find_callers"),
        recipe=file_only,
        upstream=True,
    ),
    *_strategies(
        RiskTag.WEB_SECURITY_CONFIG,
        counter="配置是否存在最小授权、CSRF/CORS 等明确限制",
        support="变更是否扩大公开路由或关闭安全保护",
        severity="暴露路由范围、默认生效环境和跨域/伪造影响是否支撑候选级别",
        context_kinds=("ast_structure",),
        allowed_tools=("get_file_content",),
        recipe=file_only,
    ),
    *_strategies(
        RiskTag.INPUT_VALIDATION,
        counter="入口是否已有格式、范围和业务约束校验",
        support="外部输入是否真实到达敏感操作或状态修改",
        severity="输入可控程度、敏感 sink 与状态影响范围是否支撑候选级别",
        context_kinds=("sensitive_api", "ast_structure"),
        allowed_tools=("get_file_content",),
        recipe=file_only,
    ),
    *_strategies(
        RiskTag.INJECTION,
        counter="参数化、编码、allowlist 或安全 builder 是否覆盖路径",
        support="不可信输入是否真实进入解释器、SQL 或命令 sink",
        severity="sink 权限、输入可控性和执行/数据影响是否支撑候选级别",
        context_kinds=("sensitive_api", "ast_structure"),
        allowed_tools=("get_file_content", "find_sensitive_apis"),
        recipe=file_sensitive,
    ),
    *_strategies(
        RiskTag.SQL_DATA_ACCESS,
        counter="参数绑定、租户条件、分页或索引约束是否存在",
        support="路径是否真实执行查询/写入并满足候选数据条件",
        severity="数据敏感度、租户/行影响范围与写入可恢复性是否支撑候选级别",
        context_kinds=("sensitive_api", "ast_structure", "find_callers"),
        allowed_tools=("get_file_content", "find_callers"),
        recipe=file_only,
        upstream=True,
    ),
    *_strategies(
        RiskTag.FILE_PATH_IO,
        counter="canonicalize、根目录约束和扩展名/大小 allowlist 是否存在",
        support="外部可控路径是否真实进入文件系统操作",
        severity="可读写路径范围、文件敏感度和覆盖/泄露影响是否支撑候选级别",
        context_kinds=("sensitive_api", "ast_structure"),
        allowed_tools=("get_file_content", "find_sensitive_apis"),
        recipe=file_sensitive,
    ),
    *_strategies(
        RiskTag.SSRF_OUTBOUND,
        counter="scheme/host/IP allowlist 与 redirect 限制是否存在",
        support="外部可控 URL 是否真实进入出站客户端",
        severity="内网可达范围、凭据转发和云元数据影响是否支撑候选级别",
        context_kinds=("sensitive_api", "ast_structure"),
        allowed_tools=("get_file_content", "find_sensitive_apis"),
        recipe=file_sensitive,
    ),
    *_strategies(
        RiskTag.CONFIG_SECURITY,
        counter="secret indirection、安全默认值和环境隔离是否存在",
        support="变更是否引入弱配置、明文秘密或暴露开关",
        severity="生效环境、秘密价值和默认暴露范围是否支撑候选级别",
        context_kinds=("ast_structure",),
        allowed_tools=("get_file_content",),
        recipe=file_only,
    ),
    *_strategies(
        RiskTag.DATA_EXPOSURE,
        counter="脱敏、最小 DTO、访问控制或日志过滤是否存在",
        support="敏感数据是否真实进入响应、日志或错误信息",
        severity="数据类别、记录规模和可访问受众是否支撑候选级别",
        context_kinds=("sensitive_api", "ast_structure"),
        allowed_tools=("get_file_content",),
        recipe=file_only,
    ),
]
