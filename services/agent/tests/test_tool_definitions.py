"""工具定义的工程正确性:ReAct 工具集名称正确,且调用透传到 ToolClient。
不依赖真实 langchain agent / LLM。

跳过条件:未安装 langchain_core 时跳过(mock 环境最小依赖)。
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain_core")

from codeguard_agent.tools.definitions import (  # noqa: E402
    make_file_content_tool,
)


class _FakeResp:
    def __init__(self, text: str) -> None:
        self._text = text

    def as_tool_output(self) -> str:
        return self._text


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_file_content(self, symbol_id: str) -> _FakeResp:
        self.calls.append(f"symbol:{symbol_id}")
        return _FakeResp("文件内容")


def test_file_content_工具名称正确():
    client = _FakeClient()
    tool = make_file_content_tool(client)
    assert tool.name == "get_file_content"
    for text in (
        "高成本兜底工具",
        "必须核对具体实现代码",
        "patch、symbol_context 和图谱工具都不足",
        "symbol_id",
        "METHOD/CONSTRUCTOR",
        "FIELD",
        "FRAMEWORK_ENTRYPOINT",
        "caller/callee",
        "listener/callback",
        "状态传播",
        "执行顺序",
        "影响范围",
        "source-to-sink",
        "优先使用 inspect_* 图谱工具",
    ):
        assert text in tool.description


def test_file_content_工具透传符号():
    client = _FakeClient()
    tool = make_file_content_tool(client)
    out = tool.invoke({"symbol_id": "java:demo.Service#run()"})
    assert out == "文件内容"
    assert client.calls == ["symbol:java:demo.Service#run()"]
