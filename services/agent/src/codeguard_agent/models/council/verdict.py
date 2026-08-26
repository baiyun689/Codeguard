"""ReviewCouncil 的裁决结果模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from codeguard_agent.models.schemas import Severity


@dataclass
class Verdict:
    candidate_id: str
    action: Literal["keep", "drop"]
    reason_code: str
    reason: str = ""
    resolved_severity: Severity | None = None
    supported: bool = False
