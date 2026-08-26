"""ReviewCouncil 因果画像与语义合并模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CausalProfile(BaseModel):
    candidate_id: str = Field(description="待比较候选的稳定 ID")
    defect_mechanism: str = Field(default="unknown", description="导致缺陷的机制")
    location: list[str] = Field(default_factory=list, description="候选涉及的文件、符号或代码位置")
    trigger: str = Field(default="unknown", description="触发该缺陷的输入、状态或调用条件")
    runtime_consequence: str = Field(default="unknown", description="缺陷在运行时造成的直接后果")
    affected_scope: str = Field(default="unknown", description="受影响的调用方、数据范围或功能范围")
    observable_behavior: str = Field(default="unknown", description="用户、调用方或系统能够观察到的行为")
    cause_evidence_ids: list[str] = Field(default_factory=list, description="支持 cause 的已验证证据 ID")
    effect_evidence_ids: list[str] = Field(default_factory=list, description="支持 effect 的已验证证据 ID")


class CausalComparison(BaseModel):
    left_candidate_id: str = Field(description="左侧候选的稳定 ID")
    right_candidate_id: str = Field(description="右侧候选的稳定 ID")
    same_cause: bool | None = Field(default=None, description="两个候选的 cause 是否语义相同")
    same_effect: bool | None = Field(default=None, description="两个候选的 effect 是否语义相同")


class CausalAnalysisBatch(BaseModel):
    profiles: list[CausalProfile] = Field(default_factory=list, description="每个候选提取出的因果信息")
    comparisons: list[CausalComparison] = Field(default_factory=list, description="候选两两之间的因果相似性判断")


class CausalMergeGroup(BaseModel):
    id: str = Field(description="合并组的稳定 ID")
    member_ids: tuple[str, ...] = Field(description="属于该合并组的候选 ID")
