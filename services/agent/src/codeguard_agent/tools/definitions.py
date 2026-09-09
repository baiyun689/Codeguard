"""把 ToolClient 的能力封装成 LangChain 工具,供 ReAct Agent 调用。

每个工具 = 一个绑定了 ToolClient 的函数 + 一段给模型看的 description。
新增工具时在这里加一个 make_*_tool 工厂即可(扩展接缝)。
"""

from __future__ import annotations


from typing import Literal


from codeguard_agent.tools.tool_client import ToolClient


def make_read_symbol_tool(client: ToolClient):
    """Construct the stable symbol-only source reader."""
    from langchain_core.tools import StructuredTool

    def _read_symbol(
        symbol_id: str,
        start_line: int | None = None,
        end_line: int | None = None,
        cursor: str | None = None,
    ) -> str:
        response = client.read_symbol(
            symbol_id,
            start_line=start_line,
            end_line=end_line,
            cursor=cursor,
        )
        return response.as_tool_output()

    return StructuredTool.from_function(
        func=_read_symbol,
        name="read_symbol",
        description=(
            "读取已解析项目 symbol 的有界源码。逐字使用初始导航、关系结果或源码头部 owner_id/members 中的 symbol_id；"
            "支持 start_line/end_line/cursor 续取；不接受文件路径、文件名或自行编造的 symbol。"
            "类型页的 members 返回真实字段/方法 ID 和范围；长符号用 next_cursor 继续读取。"
            "源码用于确认条件、顺序、状态赋值、异常和返回值，不能用来猜测不存在的调用关系。"
        ),
    )


def make_query_relations_tool(client: ToolClient):
    """Construct the typed relation navigator."""
    from langchain_core.tools import StructuredTool

    def _query_relations(
        subject_symbol_id: str,
        relation: Literal[
            "callers",
            "callees",
            "field_readers",
            "field_writers",
            "implementations",
            "overrides",
        ],
        depth: int = 1,
        limit: int = 20,
        cursor: int | None = None,
        include_callsite: bool = True,
        include_context: bool = True,
    ) -> str:
        response = client.query_relations(
            subject_symbol_id,
            relation,
            depth=depth,
            limit=limit,
            cursor=cursor,
            include_callsite=include_callsite,
            include_context=include_context,
        )
        return response.as_tool_output()

    return StructuredTool.from_function(
        func=_query_relations,
        name="query_relations",
        description=(
            "查询已知 symbol 的一种项目关系。relation 只能是 callers、callees、field_readers、field_writers、"
            "implementations 或 overrides。callers/callees 的 subject 必须是方法；field_readers/field_writers 的 subject 必须是字段，不能传包含它的类或方法。"
            "默认一跳，depth 最大 3。结果超限时只对同一 subject/relation 使用 cursor 续取，"
            "返回精确端点、调用位置、解析状态和覆盖信息。不能使用任意图查询，不能使用源码名字拼造 symbol_id。"
        ),
    )
