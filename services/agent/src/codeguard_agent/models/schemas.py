"""定义审查问题、严重级别、证据位置及完整审查结果等对外数据结构。"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Severity(str, Enum):
    """审查问题的严重级别，限定结构化输出可使用的取值。"""

    CRITICAL = "CRITICAL"  # 严重:必须修复(如 SQL 注入、鉴权绕过)
    WARNING = "WARNING"    # 警告:建议修复(如空指针风险、资源未释放)
    INFO = "INFO"          # 提示:可选优化(如命名、可读性)


class EvidenceRole(str, Enum):
    """候选对证据用途的声明；仅作为提示，不代表经过验证的图谱关系。"""

    LOCATION = "location"
    MECHANISM = "mechanism"
    REACHABILITY = "reachability"
    IMPACT = "impact"


class EvidenceRefSelection(BaseModel):
    """发现者输出的一条证据引用:短别名 + 用途声明。

    审查员只选择编号,不重新填写工具参数、代码片段或工具原文;
    别名在离开发现子图前绑定为内容寻址 artifact ID。
    """

    alias: str = Field(min_length=1, description="Evidence Catalog 中存在的编号(T01/Cxx)")
    role: EvidenceRole = Field(description="该事实对本结论的用途声明")


class EvidenceLocation(BaseModel):
    """面向用户的证据位置摘要。

    这是已验证 Artifact 的人类可读投影，不包含 Evidence Ledger 的内部编号。
    文件、symbol、行号和关系均由运行时从真实工具结果提取；LLM 不能直接填写或
    修改该结构。
    """

    file: str = Field(min_length=1, description="证据所在源码文件")
    symbol: str = Field(default="", description="证据所在的类/方法/字段")
    start_line: int = Field(default=0, ge=0, description="证据起始行，0 表示未知")
    end_line: int = Field(default=0, ge=0, description="证据结束行，0 表示未知")
    kind: Literal["changed_code", "root_cause", "related_path"] = Field(
        default="root_cause",
        description="证据用途：变更代码、根因源码或相关调用路径",
    )
    relation: str = Field(default="", description="已验证的直接关系摘要")


class Issue(BaseModel):
    """对外发布的单条审查问题。

    包含文件与行号、严重级别、问题说明、修复建议及模型置信度。
    root_cause 描述根因，evidence_locations 给出可核对的源码位置，
    不暴露内部证据编号；证据原文通过账本与 Trace 查询。
    """

    severity: Severity = Field(description="严重级别")
    file: str = Field(description="问题所在文件路径")
    line: int = Field(default=0, description="问题所在行号,0 表示无法定位到具体行")
    type: str = Field(description="问题类型,如 'SQL注入'、'空指针'、'资源泄漏'")
    message: str = Field(description="问题描述,说清楚是什么、为什么是问题")
    root_cause: str = Field(
        default="",
        description="已验证的根因及作用机制；没有足够事实时为空",
    )
    evidence_locations: list[EvidenceLocation] = Field(
        default_factory=list,
        max_length=4,
        description="用户可读的证据来源位置，不含内部证据编号",
    )
    suggestion: str = Field(default="", description="修复建议,可选")
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="置信度 0.0~1.0,LLM 对该问题判断的把握程度",
    )


class DiscoveredIssue(BaseModel):
    """发现阶段的专用输出:问题主张与 evidence_refs 短别名引用。

    发现者不能直接输出产品 ReviewResult——内部证据引用不得污染产品接口;
    系统会自动绑定 task patch(P01),evidence_refs=[] 不代表没有证据。
    """

    model_config = ConfigDict(extra="forbid")

    file: str = Field(description="问题所在文件路径")
    line: int = Field(default=0, description="问题所在行号,0 表示无法定位到具体行")
    location_snippet: str = Field(
        default="",
        description=(
            "通常从当前 task 新增行原样复制的连续代码片段，仅用于定位；"
            "删除型变更使用运行时提供的 current-revision anchor line 时必须为空"
        ),
    )
    type: str = Field(description="问题类型")
    message: str = Field(description="问题描述")
    suggestion: str = Field(default="", description="修复建议,可选")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="置信度 0.0~1.0")
    evidence_refs: list[EvidenceRefSelection] = Field(
        default_factory=list,
        max_length=3,
        description="与候选直接相关的外部事实编号(最多 3 条)",
    )


class DiscoveryReviewResult(BaseModel):
    """发现者输出的结构化审查结果(带证据引用)。"""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="本次审查的整体摘要")
    issues: list[DiscoveredIssue] = Field(description="发现的问题列表")


class ReviewResult(BaseModel):
    """一次审查的完整结果。

    summary 给人看(整体评价),issues 给机器用(逐条问题,可被后续阶段处理)。
    """

    summary: str = Field(default="", description="本次审查的整体摘要")
    issues: list[Issue] = Field(default_factory=list, description="发现的问题列表")
