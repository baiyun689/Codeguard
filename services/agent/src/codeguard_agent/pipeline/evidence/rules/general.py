"""通用审查风险证据策略。"""

from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence.rules.recipes import file_only
from codeguard_agent.pipeline.evidence.rules.types import EvidenceStrategy


_TAG = RiskTag.GENERAL_REVIEW
_SLUG = _TAG.value.lower()

GENERAL_STRATEGIES = [
    EvidenceStrategy(
        id=f"{_SLUG}.counter",
        tags=frozenset({_TAG}),
        purpose="counter",
        priority=10,
        question_template="task 中是否存在直接推翻候选的保护或前置条件",
        context_kinds=("task_patch",),
        allowed_tools=("get_file_content",),
        build_tool_calls=file_only,
    ),
    EvidenceStrategy(
        id=f"{_SLUG}.support",
        tags=frozenset({_TAG}),
        purpose="support",
        priority=20,
        question_template="task 中是否存在候选主张依赖的直接事实",
        context_kinds=("task_patch",),
        allowed_tools=("get_file_content",),
        build_tool_calls=file_only,
    ),
    EvidenceStrategy(
        id=f"{_SLUG}.severity",
        tags=frozenset({_TAG}),
        purpose="severity",
        priority=40,
        question_template="候选影响范围和恢复成本是否足以支撑所提级别",
        context_kinds=("task_patch",),
        allowed_tools=("get_file_content",),
        build_tool_calls=file_only,
    ),
]
