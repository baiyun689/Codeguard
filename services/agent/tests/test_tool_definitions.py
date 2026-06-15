"""工具定义的工程正确性:ReAct 工具集含 get_repo_map + get_file_content 两个工具,
名称正确,且各自调用透传到 ToolClient。不依赖真实 langchain agent / LLM。

跳过条件:未安装 langchain_core 时跳过(mock 环境最小依赖)。
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain_core")

from codeguard_agent.tools.definitions import (  # noqa: E402
    make_file_content_tool,
    make_repo_map_tool,
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

    def get_repo_map(self) -> _FakeResp:
        self.calls.append("repo_map")
        return _FakeResp("# Repo map")


def test_两个工具名称正确():
    client = _FakeClient()
    repo_map = make_repo_map_tool(client)
    file_content = make_file_content_tool(client)
    assert repo_map.name == "get_repo_map"
    assert file_content.name == "get_file_content"


def test_repo_map_工具无入参_调用透传():
    client = _FakeClient()
    tool = make_repo_map_tool(client)
    out = tool.invoke({})
    assert "Repo map" in out
    assert client.calls == ["repo_map"]


def test_file_content_工具透传路径():
    client = _FakeClient()
    tool = make_file_content_tool(client)
    out = tool.invoke({"file_path": "src/App.java"})
    assert out == "文件内容"
    assert client.calls == ["file:src/App.java"]
