"""Task 变更位置对应的项目符号模型。"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Self

from pydantic import BaseModel, Field, StrictInt, model_validator


class SymbolResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class ResolvedSymbol(BaseModel):
    file: str
    symbol_id: str
    kind: str
    start_line: StrictInt = Field(ge=1)
    end_line: StrictInt = Field(ge=1)
    signature: str = ""
    annotations: tuple[str, ...] = ()
    control_flow: tuple[str, ...] = ()
    source_set: Literal["MAIN", "TEST", "GENERATED"]


class TaskSymbolContext(BaseModel):
    task_id: str
    symbols: tuple[ResolvedSymbol, ...] = ()
    status: SymbolResolutionStatus
    limitations: tuple[str, ...] = ()
    truncated: bool = False

    @model_validator(mode="after")
    def validate_status_contract(self) -> Self:
        if self.status is SymbolResolutionStatus.RESOLVED and not self.symbols:
            raise ValueError("resolved symbol context must contain symbols")
        if self.status is not SymbolResolutionStatus.RESOLVED and self.symbols:
            raise ValueError("non-resolved symbol context cannot contain symbols")
        return self
