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
            "输入为相对仓库根的文件路径;仅限本次变更涉及的文件。"
        ),
    )
