"""证据策略的不可变值对象。

该模块位于 ``rules`` package 之外，避免 capability registry 与规则注册表
通过 package ``__init__`` 形成循环导入。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable, Literal

from codeguard_agent.models.council import EvidencePurpose
if TYPE_CHECKING:
    from codeguard_agent.pipeline.evidence.planner import CandidateDossier


ToolName = Literal[
    "get_file_content",
    "find_sensitive_apis",
    "find_callers",
    "get_code_metrics",
    "inspect_security_path",
    "inspect_change_impact",
    "inspect_structure",
]


class EvidenceCapability(str, Enum):
    CURRENT_IMPLEMENTATION = "CURRENT_IMPLEMENTATION"
    UPSTREAM_REACHABILITY = "UPSTREAM_REACHABILITY"
    FRAMEWORK_ENTRY_REACHABILITY = "FRAMEWORK_ENTRY_REACHABILITY"
    SECURITY_PATH = "SECURITY_PATH"
    STRUCTURAL_METRICS = "STRUCTURAL_METRICS"
    INHERITANCE_IMPACT = "INHERITANCE_IMPACT"


CAPABILITY_TO_TOOL: dict[EvidenceCapability, ToolName] = {
    EvidenceCapability.CURRENT_IMPLEMENTATION: "get_file_content",
    EvidenceCapability.UPSTREAM_REACHABILITY: "inspect_change_impact",
    EvidenceCapability.FRAMEWORK_ENTRY_REACHABILITY: "inspect_change_impact",
    EvidenceCapability.SECURITY_PATH: "inspect_security_path",
    EvidenceCapability.STRUCTURAL_METRICS: "inspect_structure",
    EvidenceCapability.INHERITANCE_IMPACT: "inspect_structure",
}

def as_capability(value: EvidenceCapability | str) -> EvidenceCapability:
    if isinstance(value, EvidenceCapability):
        return value
    return EvidenceCapability(value)


@dataclass(frozen=True)
class ToolCallSpec:
    """一次尚未执行的语义证据查询；具体 Gateway 工具由能力映射决定。"""

    capability: EvidenceCapability | str
    arguments: tuple[tuple[str, str], ...]

    @property
    def tool_name(self) -> ToolName:
        return CAPABILITY_TO_TOOL[as_capability(self.capability)]


@dataclass(frozen=True)
class EvidenceStrategy:
    """一个 claim fact 在特定证据目的下的声明式策略。"""

    id: str
    purpose: EvidencePurpose
    priority: int
    question_template: str
    context_kinds: tuple[str, ...]
    allowed_capabilities: tuple[EvidenceCapability, ...]
    build_tool_calls: Callable[["CandidateDossier"], list[ToolCallSpec]]

    @property
    def allowed_tools(self) -> tuple[ToolName, ...]:
        """供 EvidenceRequest 校验使用的 Gateway 工具投影。"""
        return tuple(CAPABILITY_TO_TOOL[capability] for capability in self.allowed_capabilities)
