"""可观测性追踪的数据模型。

纯 Pydantic 模型——无外部依赖，可独立单测。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TokenUsage(BaseModel):
    """单次 LLM 调用的 token 消耗。"""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    node_name: str = ""


class TraceEvent(BaseModel):
    """一次追踪事件。"""

    sequence: int
    timestamp_ms: float
    event_type: str
    node_name: str
    phase: str
    depth: int
    summary: str
    detail: dict = Field(default_factory=dict)
    tokens: TokenUsage | None = None
    run_id: str = ""
    parent_ids: list[str] = Field(default_factory=list)
    parent_run_id: str = ""
    node_path: str = ""
    invocation_id: str = ""


class NodeStats(BaseModel):
    """单个节点的耗时与调用统计。"""

    node_name: str
    start_ms: float
    end_ms: float
    duration_ms: float
    llm_calls: int = 0
    tool_calls: int = 0
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    run_id: str = ""
    parent_run_id: str = ""
    node_path: str = ""
    depth: int = 0
    invocation_id: str = ""


class TraceSummary(BaseModel):
    """聚合统计。"""

    total_duration_ms: float = 0.0
    total_tokens: TokenUsage = Field(default_factory=TokenUsage)
    tokens_by_node: dict[str, TokenUsage] = Field(default_factory=dict)
    event_counts: dict[str, int] = Field(default_factory=dict)
    node_timeline: list[NodeStats] = Field(default_factory=list)


class TraceReport(BaseModel):
    """一次审查的完整追踪报告。"""

    run_id: str
    timestamp: str
    diff_size: int = 0
    events: list[TraceEvent] = Field(default_factory=list)
    summary: TraceSummary = Field(default_factory=TraceSummary)
