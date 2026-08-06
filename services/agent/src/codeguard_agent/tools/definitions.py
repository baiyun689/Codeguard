"""把 ToolClient 的能力封装成 LangChain 工具,供 ReAct Agent 调用。

每个工具 = 一个绑定了 ToolClient 的函数 + 一段给模型看的 description。
新增工具时在这里加一个 make_*_tool 工厂即可(扩展接缝)。
"""

from __future__ import annotations

from codeguard_agent.tools.tool_client import ToolClient


def make_file_content_tool(client: ToolClient):
    """构造 get_file_content 工具。

    返回一个 LangChain StructuredTool:Agent 给定文件相对路径,经 Java 沙箱读取内容。
    LangChain 相关导入延迟到此处,保证 mock 模式 / 没装 langchain 时本模块仍可被引用。
    """
    from langchain_core.tools import StructuredTool

    def _get_file_content(file_path: str) -> str:
        """读取仓库中指定文件的完整内容,用于了解 diff 之外的上下文。

        参数 file_path:相对仓库根的文件路径(如 src/main/java/com/example/Service.java)。
        只能读取本次变更涉及的文件;越权 / 不存在 / 过大会返回以 'Error:' 开头的说明。
        """
        return client.get_file_content(file_path).as_tool_output()

    return StructuredTool.from_function(
        func=_get_file_content,
        name="get_file_content",
        description=(
            "读取仓库中指定文件的完整内容,用于了解 diff 之外的上下文"
            "(被改方法的完整定义、调用方、相关类等)。"
            "输入为相对仓库根的文件路径;可读 repo 内的源码文件。"
        ),
    )


def make_security_path_tool(client: ToolClient):
    from langchain_core.tools import StructuredTool

    def _inspect_security_path(symbol_id: str) -> str:
        """查询当前变更符号的框架入口、敏感 API 路径与解析限制。"""
        return client.inspect_security_path(symbol_id).as_tool_output()

    return StructuredTool.from_function(
        func=_inspect_security_path,
        name="inspect_security_path",
        description=(
            "按 prefetched_context 给出的稳定 symbol_id 查询安全路径：方法/构造器返回"
            "框架入口与敏感调用链；字段返回读写它的方法并标记敏感字段类型；类型返回"
            "内部方法的敏感调用与继承者；并附解析限制。不得自行编造 symbol_id 或文件名。"
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
            "按 prefetched_context 给出的稳定 symbol_id 查询影响面：方法/构造器返回"
            "调用方、框架入口与继承覆盖；字段返回读写它的方法；类型返回继承/实现它的"
            "类型；并附解析覆盖状态。不得用惯用类名猜测路径。"
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
            "按 prefetched_context 给出的稳定 symbol_id 查询声明、调用耦合、"
            "继承和字段关系。度量与关系必须结合当前 diff 解读。"
        ),
    )
