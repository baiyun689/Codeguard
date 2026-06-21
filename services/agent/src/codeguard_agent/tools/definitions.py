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
            "输入为相对仓库根的文件路径;可读 repo 内的源码文件(含 diff 之外、get_repo_map 指向的定义文件)。"
        ),
    )


def make_repo_map_tool(client: ToolClient):
    """构造 get_repo_map 工具(导航)。

    返回一个无入参的 LangChain StructuredTool:产出"与本次改动相关"的签名级代码地图,
    告诉审查员"diff 调用/引用的某符号定义在哪个文件",再配合 get_file_content 细读。
    描述写成**动作触发式**(何时调),把调用时机绑到审查员的判断点(见 design.md D6)。
    """
    from langchain_core.tools import StructuredTool

    def _get_repo_map() -> str:
        """获取与本次改动相关的代码地图(签名级)。

        当你看到 diff 调用/引用了一个**定义不在 diff 内**的符号、需要定位它在哪个文件时调用;
        地图还会列出**改动符号的直接调用方**(谁引用了被改的符号),据此判断改动是否破坏上游调用方的假设。
        返回若干相关定义的签名与所在文件;需要看实现再用 get_file_content 读对应文件。
        """
        return client.get_repo_map().as_tool_output()

    return StructuredTool.from_function(
        func=_get_repo_map,
        name="get_repo_map",
        description=(
            "获取与本次改动相关的代码地图(签名级:列出 diff 改动符号的定义文件与最相关的若干符号签名,"
            "并列出改动符号的直接调用方)。"
            "当你看到 diff 调用/引用了一个定义不在 diff 内的符号、需要定位它在哪个文件,"
            "或需要知道改动会波及哪些上游调用方时调用。"
            "无需入参。拿到地图后,用 get_file_content 读取目标文件确认实现。"
        ),
    )
