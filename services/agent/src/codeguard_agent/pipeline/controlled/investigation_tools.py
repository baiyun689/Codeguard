"""Require a concrete fact question on ReAct queries, without changing Java tools."""
from __future__ import annotations

from typing import Annotated, Any
from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import Field, StringConstraints, create_model


def require_fact_question(base: BaseTool) -> BaseTool:
    """Keep query intent in the agent trace, outside the factual evidence ledger.

    This is an auditable semantic checkpoint, not a claim that nonempty prose
    proves progress. Identity, budget and repetition guards remain in the client.
    """
    prompt = Path(__file__).resolve().parents[2] / "prompts/controlled/tool-fact-question.txt"
    question_type = Annotated[str, StringConstraints(strip_whitespace=True), Field(min_length=1, max_length=240)]
    schema = create_model(
        f"{base.name}_investigation_input", __base__=base.get_input_schema(),
        fact_question=(question_type, Field(description=prompt.read_text(encoding="utf-8").strip())),
    )

    def execute(fact_question: str, **arguments: Any) -> Any:
        # Model-authored intent is not a tool fact, a cache key, or a Java input.
        assert isinstance(base, StructuredTool) and base.func is not None
        return base.func(**arguments)

    return StructuredTool(name=base.name, description=base.description, args_schema=schema,
        func=execute, handle_validation_error=True)
