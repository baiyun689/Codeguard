"""审查上下文抽象：PipelineContext + PipelineStage。

设计要点：
- 用 PipelineStage 抽象 + 共享 PipelineContext，把一次审查拆成可组合、可增删的环节。
- 环节间传类型化对象（Issue / ReviewResult），不传 JSON 字符串。
- PipelineContext 是扁平 dataclass，各环节按需读写。
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
    # 工具调用上下文。
    # repo_path：被审仓库根（绝对路径），供工具会话/沙箱解析文件；
    # allowed_files：本次 diff 涉及的文件集合，沙箱据此授权；
    # tool_client：绑定到工具会话的客户端。三者均为 None/空表示"无工具"，审查员走直连基准。
    repo_path: str | None = None
    allowed_files: list[str] = field(default_factory=list)
    tool_client: Any = None
    # 工具白名单:暴露给 ReAct 审查员的工具名集合(由评测 profile 控制,确保"开哪些工具"是
    # 唯一变量、对照可控)。None 表示暴露所有已实现工具(CLI 默认行为)。
    enabled_tools: list[str] | None = None
    # ContextProvider 可直接消费已选 task 的精确文件/变更行，避免重新解析裁剪后的 diff。
    change_locations: list[dict[str, object]] = field(default_factory=list)
    # 误报过滤第二段的验证模型;为 None 时回退到 llm。
    # 应尽量与审查器异源，避免"同一模型核查自己刚报的结论"的自我确认偏差。
    fp_verify_llm: Any = None

    # --- 摘要产出（SummaryStage 写入，ReviewerStage 读取）---
    # diff_summary:结构化变更摘要文本,作为背景透传给各审查员的 user 输入({{summary}})。
    #   与下面的 summary(面向人的最终审查摘要)是两个不同概念,刻意分开两个字段。
    diff_summary: str = ""

    # --- 输出(stage 累积写入)---
    issues: list[Issue] = field(default_factory=list)
    summary: str = ""
    # ContextProvider 写入的共享事实包。用 Any 避免 base 反向依赖 council 模型。
    context_bundle: Any = None
    # ContextProvider 工具失败/不可用诊断；失败信封不得伪装成 ContextFact。
    context_diagnostics: dict[str, str] = field(default_factory=dict)


class PipelineStage(ABC):
    """管线环节的抽象基类。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """环节标识，用于日志。"""

    @abstractmethod
    def execute(self, context: PipelineContext) -> PipelineContext:
        """执行本环节：从 context 读取所需输入，把产出写回 context 并返回。"""
