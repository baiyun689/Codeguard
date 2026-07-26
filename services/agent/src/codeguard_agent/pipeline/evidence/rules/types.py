"""风险证据策略的不可变值对象。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable, Literal

from codeguard_agent.models.council import EvidencePurpose
from codeguard_agent.models.tasks import RiskTag

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

_LEGACY_TOOL_CAPABILITY: dict[str, EvidenceCapability] = {
    "get_file_content": EvidenceCapability.CURRENT_IMPLEMENTATION,
    "find_callers": EvidenceCapability.UPSTREAM_REACHABILITY,
    "find_sensitive_apis": EvidenceCapability.SECURITY_PATH,
    "get_code_metrics": EvidenceCapability.STRUCTURAL_METRICS,
}


def as_capability(value: EvidenceCapability | str) -> EvidenceCapability:
    if isinstance(value, EvidenceCapability):
        return value
    legacy = _LEGACY_TOOL_CAPABILITY.get(value)
    return legacy if legacy is not None else EvidenceCapability(value)


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
    """一个 RiskTag 在特定证据目的下的声明式策略。"""

    id: str
    tags: frozenset[RiskTag]
    purpose: EvidencePurpose
    priority: int
    question_template: str
    context_kinds: tuple[str, ...]
    allowed_capabilities: tuple[EvidenceCapability, ...]
    build_tool_calls: Callable[["CandidateDossier"], list[ToolCallSpec]]

    @property
    def allowed_tools(self) -> tuple[ToolName, ...]:
        """Legacy request-envelope projection; planning policy is capability-based."""
        return tuple(CAPABILITY_TO_TOOL[capability] for capability in self.allowed_capabilities)
