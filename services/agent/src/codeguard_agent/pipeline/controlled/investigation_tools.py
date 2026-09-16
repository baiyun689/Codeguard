"""为 ReAct 查询附加待确认的事实问题，并保持 Java 工具参数不变。"""
from __future__ import annotations

from typing import Annotated, Any
from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import Field, StringConstraints, create_model


def require_fact_question(base: BaseTool) -> BaseTool:
    """记录查询意图供 Trace 查看，不将其作为事实证据。

    符号来源、调用预算和重复查询由客户端校验，查询说明不参与进展判断。
    """
    prompt = Path(__file__).resolve().parents[2] / "prompts/controlled/tool-fact-question.txt"
    question_type = Annotated[str, StringConstraints(strip_whitespace=True), Field(min_length=1, max_length=240)]
    schema = create_model(
        f"{base.name}_investigation_input", __base__=base.get_input_schema(),
        fact_question=(question_type, Field(description=prompt.read_text(encoding="utf-8").strip())),
    )

    def execute(fact_question: str, **arguments: Any) -> Any:
        # 模型填写的查询意图不进入工具事实、缓存键或 Java 请求参数。
        assert isinstance(base, StructuredTool) and base.func is not None
        return base.func(**arguments)

    return StructuredTool(name=base.name, description=base.description, args_schema=schema,
        func=execute, handle_validation_error=True)
