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


def make_sensitive_apis_tool(client: ToolClient):
    """构造 find_sensitive_apis 工具(安全审查员专属)。

    返回一个无入参的 LangChain StructuredTool:自动扫描 diff 涉及文件的危险 API 调用。
    描述写成动作触发式(何时调),把调用时机绑到审查员的判断点。
    """
    from langchain_core.tools import StructuredTool

    def _find_sensitive_apis() -> str:
        """扫描本次 diff 涉及的所有文件,发现危险 API 调用(SQL 执行/命令注入/反序列化/弱加密/路径操作/反射等)。

        无需入参——工具自动扫描当前会话的源文件。
        当你需要系统性地确认 diff 中是否存在危险 API 调用、而非仅凭自己看到的代码片段猜测时调用。
        """
        return client.find_sensitive_apis().as_tool_output()

    return StructuredTool.from_function(
        func=_find_sensitive_apis,
        name="find_sensitive_apis",
        description=(
            "系统性地扫描本次 diff 涉及的所有文件,发现其中的危险 API 调用"
            "(SQL 执行/命令注入/反序列化/弱加密/路径操作/反射/脚本执行/XXE/SSRF)。"
            "无需入参——工具自动扫描当前会话的源文件。"
            "当你需要确认 diff 中是否有遗漏的危险 API 调用时调用。"
        ),
    )


def make_callers_tool(client: ToolClient):
    """构造 find_callers 工具(逻辑审查员专属)。

    返回一个 LangChain StructuredTool:给定方法名,查询仓库内所有调用方。
    入参格式:'文件路径#方法名'(如 src/main/java/OrderService.java#calculatePrice)。
    """
    from langchain_core.tools import StructuredTool

    def _find_callers(query: str) -> str:
        """查询指定方法在仓库内的所有直接调用方。

        参数 query:格式为'文件路径#方法名'(如 src/main/java/com/example/OrderService.java#calculatePrice)。
        当你发现一个方法的签名/返回值被修改、需要确认哪些调用方可能受影响时调用。
        """
        return client.find_callers(query).as_tool_output()

    return StructuredTool.from_function(
        func=_find_callers,
        name="find_callers",
        description=(
            "查询指定方法在仓库内的所有直接调用方。"
            "当你发现一个方法的签名/返回值被修改、需要确认哪些调用方可能受影响时调用。"
            "入参格式:'文件路径#方法名'(如 src/main/java/OrderService.java#calculatePrice)。"
        ),
    )


def make_metrics_tool(client: ToolClient):
    """构造 get_code_metrics 工具(质量审查员专属)。

    返回一个 LangChain StructuredTool:给定文件路径,计算圈复杂度等度量。
    """
    from langchain_core.tools import StructuredTool

    def _get_code_metrics(file_path: str) -> str:
        """计算指定文件的代码度量(圈复杂度、代码行数、嵌套深度、参数数量)。

        参数 file_path:相对仓库根的文件路径。
        当你面对一个新增大段实现或怀疑某文件过度复杂、需要精确数据做判断时调用。
        """
        return client.get_code_metrics(file_path).as_tool_output()

    return StructuredTool.from_function(
        func=_get_code_metrics,
        name="get_code_metrics",
        description=(
            "计算指定文件的代码度量(圈复杂度、代码行数、嵌套深度、参数数量)。"
            "当你面对一个新增大段实现或怀疑某文件过度复杂、需要精确数据做判断时调用。"
            "入参:文件路径(如 src/main/java/com/example/OrderService.java)。"
        ),
    )
