"""定义模型服务使用的受控输出结构。

传输层忽略模型附加的展示字段，解析后转换为严格的内部模型，
再执行路由和工具调用，附加字段不会进入运行时状态。
"""

from __future__ import annotations
from typing import Any, get_args
from pydantic import ConfigDict, Field, model_validator
from codeguard_agent.models.tasks import (
    InvestigationFinding,
    InvestigationObservation,
    InvestigationResult,
)


class _ProviderEnvelope:
    """将带默认值的可选字段中的 null 按未提供处理。

    必填字段、主张和路由判别字段仍按内部模型进行严格校验。
    """

    @model_validator(mode="before")
    @classmethod
    def _drop_null_defaults(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        fields = getattr(cls, "model_fields")
        display_fields = {
            "claim",
            "mechanism",
            "impact",
            "location_file",
            "location_snippet",
            "suggestion",
            "type_hint",
            "subtask_id",
            "observation_id",
        }
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if item is None and key in fields and (not fields[key].is_required()):
                continue
            annotation = fields[key].annotation if key in fields else None
            string_compatible = annotation is str or str in get_args(annotation)
            if (
                key in display_fields
                and string_compatible
                and (item is not None)
                and (not isinstance(item, str))
            ):
                item = str(item)
            normalized[key] = item
        return normalized


class LlmInvestigationObservation(_ProviderEnvelope, InvestigationObservation):
    model_config = ConfigDict(extra="ignore")


class LlmInvestigationFinding(_ProviderEnvelope, InvestigationFinding):
    model_config = ConfigDict(extra="ignore")
    observations: tuple[LlmInvestigationObservation, ...] = Field(
        default=(), max_length=3
    )


class LlmInvestigationResult(_ProviderEnvelope, InvestigationResult):
    model_config = ConfigDict(extra="ignore")
    findings: tuple[LlmInvestigationFinding, ...] = Field(default=(), max_length=8)
