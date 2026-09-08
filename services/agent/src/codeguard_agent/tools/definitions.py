"""把 ToolClient 的能力封装成 LangChain 工具,供 ReAct Agent 调用。

每个工具 = 一个绑定了 ToolClient 的函数 + 一段给模型看的 description。
新增工具时在这里加一个 make_*_tool 工厂即可(扩展接缝)。
"""

from __future__ import annotations

from typing import Literal

from codeguard_agent.tools.tool_client import ToolClient


def make_file_content_tool(client: ToolClient):
    """构造 get_file_content 工具。

    返回一个 LangChain StructuredTool:Agent 给定稳定 symbol_id,经 Java 图谱快照读取源码片段。
    这是高成本兜底工具;涉及跨符号关系时应优先使用图谱工具。
    LangChain 相关导入延迟到此处,保证 mock 模式 / 没装 langchain 时本模块仍可被引用。
    """
    from langchain_core.tools import StructuredTool

    def _get_file_content(
        symbol_id: str,
        start_line: int | None = None,
        end_line: int | None = None,
        cursor: str | None = None,
    ) -> str:
        """仅在图谱事实不足以回答问题时读取一个已解析 symbol 的源码片段。

        参数 symbol_id:必须是 GraphPlan 提供的 Sxx 或工具结果返回的 Rxx alias，不能自行猜测。
        METHOD/CONSTRUCTOR 返回完整声明与方法体；TYPE 返回类型定义；FIELD 返回完整字段声明。
        过大或不存在会返回以 'Error:' 开头的说明。
        """
        if start_line is None and end_line is None and cursor is None:
            response = client.get_file_content(symbol_id)
        else:
            response = client.get_file_content(
                symbol_id,
                start_line=start_line,
                end_line=end_line,
                cursor=cursor,
            )
        return response.as_tool_output()

    return StructuredTool.from_function(
        func=_get_file_content,
        name="get_file_content",
        description=(
            "高成本兜底工具:仅在必须核对具体实现代码,且 patch、symbol_context 和图谱工具都不足以回答当前缺口时,"
            "读取一个已由 SymbolResolution 或图谱结果提供的 symbol 源码片段。"
            "METHOD/CONSTRUCTOR 返回声明和方法体，TYPE 返回类型定义，FIELD 返回完整字段声明，"
            "FRAMEWORK_ENTRYPOINT 返回对应注解。涉及 caller/callee、listener/callback、字段访问或影响范围时，"
            "先用 inspect_* 定位相关 symbol；需要确认条件、顺序、状态赋值或参数使用时再读取源码。"
            "可选 start_line/end_line/cursor 只能在同一 symbol 的声明范围内续取，不能传任意文件路径。"
            "输入只能是稳定 Sxx/Rxx symbol alias，不得传完整 Gateway symbol_id、文件路径、文件名或自行编造 ID。"
        ),
    )


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
            "读取已解析项目 symbol 的有界源码。只接受 GraphPlan 或前序 query_relations 返回的 symbol_id；"
            "支持 start_line/end_line/cursor 续取；不接受文件路径、文件名或自行编造的 symbol。"
            "源码用于确认条件、顺序、状态赋值、异常和返回值，不能用来猜测不存在的调用关系。"
        ),
    )


def make_query_relations_tool(client: ToolClient):
    """Construct the typed relation navigator."""
    from langchain_core.tools import StructuredTool

    def _query_relations(
        subject_symbol_id: str,
        relation: Literal[
            "callers", "callees", "field_readers", "field_writers",
            "implementations", "overrides",
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
            "implementations 或 overrides；默认一跳，depth 最大 3。结果超限时只对同一 subject/relation 使用 cursor 续取，"
            "返回精确端点、调用位置、解析状态和覆盖信息。不能使用任意图查询，不能使用源码名字拼造 symbol_id。"
        ),
    )


def make_path_tool(client: ToolClient):
    from langchain_core.tools import StructuredTool

    def _inspect_path(
        symbol_id: str,
        path_kind: Literal["behavior", "security"],
        max_depth: int = 3,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> str:
        """查询当前变更符号的有界下游行为或安全路径。"""
        return client.inspect_path(
            symbol_id, path_kind, max_depth, limit=limit, cursor=cursor
        ).as_tool_output()

    return StructuredTool.from_function(
        func=_inspect_path,
        name="inspect_path",
        description=(
            "按 GraphPlan 给出的 Sxx/Rxx symbol alias 查询有界下游路径。"
            "path_kind=behavior 查询有界下游调用，并保留完整的已解析 CALLS 路径及附属关系事实；"
            "path_kind=security 只返回有界遍历中发现的敏感调用命中和入口线索，"
            "不表示从起点到敏感调用的完整连通路径，也不证明参数污染或数据流传播。"
            "path_kind 只能是 behavior 或 security，max_depth 默认 3、最大 3。"
            "结果超限时才使用 cursor 继续同一查询；不要把 cursor 当作新 subject。不得自行编造 Sxx/Rxx alias 或文件名。"
        ),
    )


def make_change_impact_tool(client: ToolClient):
    from langchain_core.tools import StructuredTool

    def _inspect_change_impact(
        symbol_id: str,
        max_depth: int | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> str:
        """查询变更符号的影响面：方法/构造器查调用方与框架入口，字段查读写引用，类查继承实现。"""
        if max_depth is None and limit is None and cursor is None:
            response = client.inspect_change_impact(symbol_id)
        else:
            response = client.inspect_change_impact(
                symbol_id, max_depth=max_depth, limit=limit, cursor=cursor
            )
        return response.as_tool_output()

    return StructuredTool.from_function(
        func=_inspect_change_impact,
        name="inspect_change_impact",
        description=(
            "按 GraphPlan 给出的 Sxx/Rxx alias 查询向上的调用方/入口影响面。默认从一层开始，"
            "只有调查问题需要时才增加 max_depth；limit/cursor 只用于超限后的同一查询续取。"
            "结果只证明已返回关系存在，不证明未返回关系不存在，也不得用惯用类名猜测路径。"
        ),
    )


def make_structure_tool(client: ToolClient):
    from langchain_core.tools import StructuredTool

    def _inspect_structure(
        symbol_id: str,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> str:
        """查询当前变更符号的声明、依赖、继承和耦合事实。"""
        if limit is None and cursor is None:
            response = client.inspect_structure(symbol_id)
        else:
            response = client.inspect_structure(symbol_id, limit=limit, cursor=cursor)
        return response.as_tool_output()

    return StructuredTool.from_function(
        func=_inspect_structure,
        name="inspect_structure",
        description=(
            "按 GraphPlan 给出的 Sxx/Rxx alias 查询一跳声明、调用、继承和字段关系。"
            "只在当前结构性问题需要时使用；关系数量本身不构成复杂度缺陷。超限时才用 cursor 续取。"
        ),
    )
