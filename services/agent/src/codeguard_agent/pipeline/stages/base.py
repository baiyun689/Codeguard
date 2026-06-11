"""管线阶段抽象 + 阶段间传递的上下文。

设计要点(借鉴 Diffguard,但刻意简化):
- 用 PipelineStage 抽象 + 共享 PipelineContext,把"一次审查"拆成可组合、可增删的环节。
- 阶段间传**类型化对象**(Issue / ReviewResult),不传 JSON 字符串——比 Diffguard 更不易出错。
- 阶段 1 不引入 async、不引入 chunking、不引入工具会话:那些是后续阶段/过度工程,现在不要。

PipelineContext 当前是一个扁平 dataclass。等阶段数变多(摘要/审查/聚合各有产出)再考虑
拆成子对象避免 god object——现在只有一个 stage,扁平就够。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from codeguard_agent.models.schemas import Issue


@dataclass
class PipelineContext:
    """在各 stage 之间流转的共享上下文。

    输入字段(管线开始时设好,过程中只读):
        diff_text / llm / max_retries / structured_method
    输出字段(各 stage 往里累积写):
        issues / summary
    """

    # --- 输入 ---
    diff_text: str
    llm: Any = None  # LangChain Chat 模型;None 表示 mock 模式(由下游识别)
    max_retries: int = 3
    structured_method: str = "function_calling"

    # --- 输出(stage 累积写入)---
    issues: list[Issue] = field(default_factory=list)
    summary: str = ""


class PipelineStage(ABC):
    """单个管线阶段的抽象基类。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """阶段标识,用于日志。"""

    @abstractmethod
    def execute(self, context: PipelineContext) -> PipelineContext:
        """执行本阶段:从 context 读取所需输入,把产出写回 context 并返回。"""
