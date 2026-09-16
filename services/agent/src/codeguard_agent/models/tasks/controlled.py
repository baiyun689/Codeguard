"""受控审查模式的计划、候选和证据契约。

这些模型是 controlled pipeline 的内部协议，不改变产品 ``Issue`` schema，
也不作为 Gateway 请求协议。LLM 只能生成其中的语义字段；ID、证据绑定和
执行状态由 Python 运行时负责。
"""

from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt


class ControlledModel(BaseModel):
    """所有受控内部模型禁止未声明字段，避免 LLM 静默扩展协议。"""

    model_config = ConfigDict(extra="forbid")


class InvestigationObservation(ControlledModel):
    """React 最终结论引用的本次子任务局部观察。"""

    observation_id: str = Field(min_length=1)
    role: Literal["relation", "mechanism", "impact", "location"]


class InvestigationFinding(ControlledModel):
    """子任务 React 根据证据形成的候选草案。"""

    claim: str = Field(min_length=1)
    mechanism: str = Field(min_length=1)
    impact: str = ""
    observations: tuple[InvestigationObservation, ...] = Field(default=(), max_length=3)
    location_file: str = Field(min_length=1)
    location_line: StrictInt = Field(default=0, ge=0)
    location_snippet: str = Field(
        default="",
        max_length=1000,
        description="1-5 verbatim added lines for location; empty for deletion anchors",
    )
    suggestion: str = ""
    type_hint: str = ""


class InvestigationResult(ControlledModel):
    """一次子任务 React 的唯一终止结果。"""

    subtask_id: str = Field(min_length=1)
    outcome: Literal["findings", "no_finding", "inconclusive", "failed"]
    findings: tuple[InvestigationFinding, ...] = Field(default=(), max_length=8)
    limitations: tuple[str, ...] = Field(default=(), max_length=6)


class SubtaskInstruction(ControlledModel):
    """单个 ReAct 调查组的上下文、符号入口和执行限制，由运行时生成。"""

    subtask_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    # 同一文件任务中的字段声明共用调查组；执行范围由组数和工具预算约束。
    initial_symbol_ids: tuple[str, ...] = Field(default=())
    allowed_tools: tuple[str, ...] = Field(default=(), max_length=4)
    allowed_relations: tuple[
        Literal[
            "callers",
            "callees",
            "field_readers",
            "field_writers",
            "implementations",
            "overrides",
            "parents",
            "children",
            "type_users",
            "type_references",
            "entrypoints",
        ],
        ...,
    ] = Field(default=(), max_length=11)
    max_tool_calls: StrictInt = Field(default=10, ge=0, le=20)
    max_rounds: StrictInt = Field(default=6, ge=1, le=12)


class SubtaskPlan(ControlledModel):
    """一个 reviewer 的调查分派容器；只包含调查指导，不包含候选。"""

    task_id: str = Field(min_length=1)
    subtasks: tuple[SubtaskInstruction, ...] = Field(default=(), max_length=24)
