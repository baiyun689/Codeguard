"""Provider-facing schemas for controlled structured-output calls.

OpenAI-compatible providers sometimes append display-only fields to otherwise
valid objects (for example ``confidence_note`` or an alternate location
label).  The internal controlled models deliberately keep ``extra=forbid`` so
runtime code cannot silently consume an unowned field.  These small transport
subclasses are the only tolerant boundary: unknown metadata is ignored by the
provider parser, then the parsed object is converted back to the strict model
before any routing or execution occurs.
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
    """Tolerate JSON null for fields that have a model-side default.

    OpenAI-compatible providers frequently serialize an omitted optional field
    as ``null``.  Pydantic would normally reject that before the controlled
    boundary can apply its per-row salvage.  Removing only non-required fields
    keeps claims, routing discriminators, and other semantic requirements
    strict while treating null exactly like omission.
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
