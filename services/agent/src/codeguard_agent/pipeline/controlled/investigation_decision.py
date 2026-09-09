"""A single model decision consumes observations, then queries or concludes.

The schema reuses the two actual tool schemas. It does not invent graph actions,
interpret source, or treat the model's assessment as factual evidence.
"""

from __future__ import annotations
from typing import Any, Literal, Union
from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator
from codeguard_agent.pipeline.controlled.llm_contracts import LlmInvestigationResult


class _Decision(BaseModel):
    model_config = ConfigDict(extra="ignore")
    assessment: str = Field(min_length=1, max_length=360)
    result: LlmInvestigationResult | None = None

    @field_validator("assessment", mode="before")
    @classmethod
    def bound_commentary(cls, value: Any) -> Any:
        return value.strip()[:360] if isinstance(value, str) else value


def decision_schema(
    tools: list[Any],
) -> Any:
    """Publish query arguments from the actual tool definitions, without drift."""
    variants: list[Any] = []
    for tool in tools:
        variants.append(
            create_model(
                f"{tool.name}_decision_query",
                __config__=ConfigDict(extra="ignore"),
                __doc__=tool.description,
                tool=(Literal[tool.name], ...),
                arguments=(tool.get_input_schema(), ...),
            )
        )
    query_type: Any = Union[tuple(variants)] if variants else Any
    return create_model(
        "LlmInvestigationDecision",
        __base__=_Decision,
        queries=(
            tuple[query_type, ...],
            Field(default=(), max_length=2 if variants else 0),
        ),
    )


def symbol_name(symbol_id: str) -> str:
    """Simple declared name from the Gateway's canonical Java symbol format."""
    if not symbol_id.startswith("java:"):
        return ""
    if "#" in symbol_id:
        return symbol_id.split("#", 1)[1].split("(", 1)[0].removeprefix("<init>")
    return symbol_id.removeprefix("java:").rsplit(".", 1)[-1].rsplit("$", 1)[-1]


def decision_error(
    decision: Any, *, aliases: dict[str, str], known: set[str], pending: set[str]
) -> str:
    """Check observable protocol facts; semantic correctness remains an LLM task."""
    if not decision.assessment.strip():
        return "assessment_empty"
    if bool(decision.queries) == (decision.result is not None):
        return "provide_queries_or_result_exclusively"
    for query in decision.queries:
        args = query.arguments.model_dump()
        reference = args.get("symbol_id") or args.get("subject_symbol_id") or ""
        if not aliases:
            continue
        raw = aliases.get(reference)
        if raw is None:
            return f"unknown_symbol:{reference}"
    return ""


def decision_messages(messages: list[Any], originals: dict[str, Any]) -> list[Any]:
    """Expose one decision call/result pair, retaining every actual observation.

    Internal query calls still execute in the existing tool node. They must not
    teach the model a second, unadvertised function-calling protocol. This view
    is for model input only; tool tracing and factual ledger capture are intact.
    """
    from langchain_core.messages import AIMessage, ToolMessage

    result: list[Any] = []
    observations: list[dict[str, Any]] = []
    expected: set[str] = set()
    original: Any = None
    for message in messages:
        if (
            isinstance(message, AIMessage)
            and message.tool_calls
            and (message.tool_calls[0]["id"] in originals)
        ):
            original = originals[message.tool_calls[0]["id"]]
            expected = {str(call["id"]) for call in message.tool_calls}
            result.append(AIMessage(content=message.content, tool_calls=[original]))
        elif isinstance(message, ToolMessage) and message.tool_call_id in expected:
            observations.append({"tool": message.name, "result": message.content})
            expected.remove(message.tool_call_id)
            if not expected:
                import json

                result.append(
                    ToolMessage(
                        content=json.dumps(observations, ensure_ascii=False),
                        tool_call_id=original["id"],
                        name="LlmInvestigationDecision",
                    )
                )
                observations = []
        else:
            result.append(message)
    if expected:
        raise ValueError("decision_observations_incomplete")
    return result
