"""风险证据策略的不可变值对象。"""

from __future__ import annotations

from dataclasses import dataclass
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
]


@dataclass(frozen=True)
class ToolCallSpec:
    """一次尚未执行的 Gateway 工具调用。"""

    tool_name: ToolName
    arguments: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class EvidenceStrategy:
    """一个 RiskTag 在特定证据目的下的声明式策略。"""

    id: str
    tags: frozenset[RiskTag]
    purpose: EvidencePurpose
    priority: int
    question_template: str
    context_kinds: tuple[str, ...]
    allowed_tools: tuple[ToolName, ...]
    build_tool_calls: Callable[["CandidateDossier"], list[ToolCallSpec]]
