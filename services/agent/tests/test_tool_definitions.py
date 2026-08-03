"""工具定义的工程正确性:ReAct 工具集含 3 个工具(1 共享 + 2 专属),
名称正确,且各自调用透传到 ToolClient。不依赖真实 langchain agent / LLM。

跳过条件:未安装 langchain_core 时跳过(mock 环境最小依赖)。
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain_core")

from codeguard_agent.tools.definitions import (  # noqa: E402
    make_file_content_tool,
    make_metrics_tool,
    make_sensitive_apis_tool,
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

    def find_sensitive_apis(self) -> _FakeResp:
        self.calls.append("sensitive_apis")
        return _FakeResp("# 敏感 API 扫描\n未发现危险 API 调用")

    def get_code_metrics(self, file_path: str) -> _FakeResp:
        self.calls.append(f"metrics:{file_path}")
        return _FakeResp("# 代码度量\n| `foo()` | 3 | 10 | 1 | 1 | ✓ |")


def test_三个工具名称正确():
    client = _FakeClient()
    assert make_file_content_tool(client).name == "get_file_content"
    assert make_sensitive_apis_tool(client).name == "find_sensitive_apis"
    assert make_metrics_tool(client).name == "get_code_metrics"


def test_file_content_工具透传路径():
    client = _FakeClient()
    tool = make_file_content_tool(client)
    out = tool.invoke({"file_path": "src/App.java"})
    assert out == "文件内容"
    assert client.calls == ["file:src/App.java"]


def test_sensitive_apis_工具无入参_调用透传():
    client = _FakeClient()
    tool = make_sensitive_apis_tool(client)
    out = tool.invoke({})
    assert "敏感 API" in out
    assert client.calls == ["sensitive_apis"]


def test_metrics_工具透传路径():
    client = _FakeClient()
    tool = make_metrics_tool(client)
    out = tool.invoke({"file_path": "src/App.java"})
    assert "代码度量" in out
    assert client.calls == ["metrics:src/App.java"]
