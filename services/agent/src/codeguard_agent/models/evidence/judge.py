"""EvidenceJudge 的结构化输入输出模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from codeguard_agent.models.schemas import Severity


class EvidenceJudgeAssessment(BaseModel):
    candidate_id: str
    action: Literal["keep", "drop"]
    severity: Severity | None = None
    evidence_ids: list[str] = Field(
        default_factory=list,
        description="Judge 作出裁决时实际使用的已验证证据 ID",
    )
    reason: str = ""


class EvidenceJudgeBatch(BaseModel):
    assessments: list[EvidenceJudgeAssessment] = Field(default_factory=list)
