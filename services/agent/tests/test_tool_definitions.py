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

    def get_file_content(self, file_path: str) -> _FakeResp:
        self.calls.append(f"file:{file_path}")
        return _FakeResp("文件内容")


def test_file_content_工具名称正确():
    client = _FakeClient()
    assert make_file_content_tool(client).name == "get_file_content"


def test_file_content_工具透传路径():
    client = _FakeClient()
    tool = make_file_content_tool(client)
    out = tool.invoke({"file_path": "src/App.java"})
    assert out == "文件内容"
    assert client.calls == ["file:src/App.java"]
