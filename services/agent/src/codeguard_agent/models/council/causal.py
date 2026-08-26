"""ReviewCouncil 因果画像与语义合并模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CausalProfile(BaseModel):
    candidate_id: str
    defect_mechanism: str = "unknown"
    location: list[str] = Field(default_factory=list)
    trigger: str = "unknown"
    runtime_consequence: str = "unknown"
    affected_scope: str = "unknown"
    observable_behavior: str = "unknown"
    cause_evidence_ids: list[str] = Field(default_factory=list)
    effect_evidence_ids: list[str] = Field(default_factory=list)


class CausalComparison(BaseModel):
    left_candidate_id: str
    right_candidate_id: str
    same_cause: bool | None = None
    same_effect: bool | None = None


class CausalAnalysisBatch(BaseModel):
    profiles: list[CausalProfile] = Field(default_factory=list)
    comparisons: list[CausalComparison] = Field(default_factory=list)


class CausalMergeGroup(BaseModel):
    id: str
    member_ids: tuple[str, ...]
