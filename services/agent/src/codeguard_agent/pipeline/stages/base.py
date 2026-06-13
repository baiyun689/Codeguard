"""管线阶段抽象 + 阶段间传递的上下文。

设计要点(刻意从简):
- 用 PipelineStage 抽象 + 共享 PipelineContext,把"一次审查"拆成可组合、可增删的环节。
- 阶段间传**类型化对象**(Issue / ReviewResult),不传 JSON 字符串——比传字符串更不易出错。
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
    # 阶段 3:工具调用上下文(见 design.md D6)。
    # repo_path:被审仓库根(绝对路径),供工具会话/沙箱解析文件;
    # allowed_files:本次 diff 涉及的文件集合,沙箱据此授权;
    # tool_client:绑定到工具会话的客户端。三者均为 None/空表示"无工具",审查员走直连基准。
    repo_path: str | None = None
    allowed_files: list[str] = field(default_factory=list)
    tool_client: Any = None
    # 误报过滤第二段的验证模型;为 None 时回退到 llm。
    # 应尽量与审查器**异源**,避免"同一模型核查自己刚报的结论"的自我确认偏差(见 ADR-005)。
    fp_verify_llm: Any = None

    # --- 摘要阶段产出(SummaryStage 写入,ReviewerStage 读取)---
    # diff_summary:结构化变更摘要文本,作为背景透传给各审查员的 user 输入({{summary}})。
    #   与下面的 summary(面向人的最终审查摘要)是两个不同概念,刻意分开两个字段。
    # file_groups:reviewer 名 → 该维度相关文件路径列表(软路由);空 dict 表示未做分派,
    #   审查员吃整份 diff。change_types / risk_level 仅用于日志与诊断。
    diff_summary: str = ""
    file_groups: dict[str, list[str]] = field(default_factory=dict)
    change_types: list[str] = field(default_factory=list)
    risk_level: int = 0

    # --- 输出(stage 累积写入)---
    issues: list[Issue] = field(default_factory=list)
    summary: str = ""
    # 误报过滤阶段写入的统计(FilterStats);None 表示该阶段未运行。
    # 用 Any 避免 base 反向依赖 fp_filter(后者要 import 本模块的 PipelineStage)。
    filter_stats: Any = None


class PipelineStage(ABC):
    """单个管线阶段的抽象基类。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """阶段标识,用于日志。"""

    @abstractmethod
    def execute(self, context: PipelineContext) -> PipelineContext:
        """执行本阶段:从 context 读取所需输入,把产出写回 context 并返回。"""
