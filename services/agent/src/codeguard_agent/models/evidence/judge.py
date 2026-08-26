"""EvidenceJudge 的结构化输入输出模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from codeguard_agent.models.schemas import Severity


class EvidenceJudgeAssessment(BaseModel):
    candidate_id: str = Field(description="待裁决候选的稳定 ID")
    action: Literal["keep", "drop"] = Field(description="是否保留该候选进入最终审查结果")
    severity: Severity | None = Field(default=None, description="仅当 action=keep 时填写最终严重级别；drop 必须为空")
    evidence_ids: list[str] = Field(
        default_factory=list,
        description="Judge 作出裁决时实际使用的已验证证据 ID",
    )
    reason: str = Field(description="基于证据作出该裁决的简要理由")


class EvidenceJudgeBatch(BaseModel):
    assessments: list[EvidenceJudgeAssessment] = Field(default_factory=list, description="本批次每个候选对应的一条裁决")
