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

    def _get_file_content(symbol_id: str) -> str:
        """仅在图谱事实不足以回答问题时读取一个已解析 symbol 的源码片段。

        参数 symbol_id:必须来自 symbol_context 或先前图谱结果，不能自行猜测。
        METHOD/CONSTRUCTOR 返回完整声明与方法体；TYPE 返回类型定义；FIELD 返回完整字段声明。
        过大或不存在会返回以 'Error:' 开头的说明。
        """
        return client.get_file_content(symbol_id).as_tool_output()

    return StructuredTool.from_function(
        func=_get_file_content,
        name="get_file_content",
        description=(
            "高成本兜底工具:仅在必须核对具体实现代码,且 patch、symbol_context 和图谱工具都不足以回答当前缺口时,"
            "读取一个已由 SymbolResolution 或图谱结果提供的 symbol 源码片段。"
            "METHOD/CONSTRUCTOR 返回声明和方法体，TYPE 返回类型定义，FIELD 返回完整字段声明，"
            "FRAMEWORK_ENTRYPOINT 返回对应注解。涉及 caller/callee、listener/callback、字段访问或影响范围时，"
            "先用 inspect_* 定位相关 symbol；需要确认条件、顺序、状态赋值或参数使用时再读取源码。"
            "输入只能是稳定 symbol_id，不得传文件路径、文件名或自行编造 ID。"
        ),
    )


def make_path_tool(client: ToolClient):
    from langchain_core.tools import StructuredTool

    def _inspect_path(
        symbol_id: str,
        path_kind: Literal["behavior", "security"],
        max_depth: int = 3,
    ) -> str:
        """查询当前变更符号的有界下游行为或安全路径。"""
        return client.inspect_path(symbol_id, path_kind, max_depth).as_tool_output()

    return StructuredTool.from_function(
        func=_inspect_path,
        name="inspect_path",
        description=(
            "按 symbol_context 给出的稳定 symbol_id 查询有界下游路径。"
            "path_kind=behavior 查询有界下游调用，并保留完整的已解析 CALLS 路径及附属关系事实；"
            "path_kind=security 只返回有界遍历中发现的敏感调用命中和入口线索，"
            "不表示从起点到敏感调用的完整连通路径，也不证明参数污染或数据流传播。"
            "path_kind 只能是 behavior 或 security，max_depth 默认 3、最大 3。"
            "不得自行编造 symbol_id 或文件名。"
        ),
    )


def make_change_impact_tool(client: ToolClient):
    from langchain_core.tools import StructuredTool

    def _inspect_change_impact(symbol_id: str) -> str:
        """查询变更符号的影响面：方法/构造器查调用方与框架入口，字段查读写引用，类查继承实现。"""
        return client.inspect_change_impact(symbol_id).as_tool_output()

    return StructuredTool.from_function(
        func=_inspect_change_impact,
        name="inspect_change_impact",
        description=(
            "按 symbol_context 给出的稳定 symbol_id 查询影响面：方法/构造器返回最多三层"
            "调用方和框架入口，并附继承覆盖；字段返回一跳读写者；类型返回一跳继承/实现者；"
            "结果只证明已返回关系存在。不得用惯用类名猜测路径。"
        ),
    )


def make_structure_tool(client: ToolClient):
    from langchain_core.tools import StructuredTool

    def _inspect_structure(symbol_id: str) -> str:
        """查询当前变更符号的声明、依赖、继承和耦合事实。"""
        return client.inspect_structure(symbol_id).as_tool_output()

    return StructuredTool.from_function(
        func=_inspect_structure,
        name="inspect_structure",
        description=(
            "按 symbol_context 给出的稳定 symbol_id 查询声明、调用耦合、"
            "继承和字段关系。度量与关系必须结合当前 diff 解读。"
        ),
    )
