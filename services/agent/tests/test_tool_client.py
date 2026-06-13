"""ToolClient 的工程正确性测试:统一信封解析 + 会话生命周期 + 错误映射。

用 httpx.MockTransport 打桩 Java 工具服务,不依赖真实服务进程。
"""

from __future__ import annotations

import httpx

from codeguard_agent.tools.tool_client import (
    ToolClient,
    ToolResponse,
    create_tool_session,
    destroy_tool_session,
)


def _mock_client(handler) -> ToolClient:
    client = ToolClient("http://toolserver", "sess-1")
    # 替换内部 httpx.Client 为带 MockTransport 的实例(绕过真实网络)。
    client._client = httpx.Client(transport=httpx.MockTransport(handler))  # noqa: SLF001
    return client


def test_成功信封_映射为_result():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Session-Id"] == "sess-1"
        assert request.url.path == "/api/v1/tools/get_file_content"
        return httpx.Response(200, json={"success": True, "result": "文件内容"})

    resp = _mock_client(handler).get_file_content("src/App.java")
    assert resp.success is True
    assert resp.result == "文件内容"
    assert resp.as_tool_output() == "文件内容"


def test_失败信封_映射为_error_并加前缀():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "error": "文件不在审查范围内"})

    resp = _mock_client(handler).get_file_content("src/Other.java")
    assert resp.success is False
    assert resp.as_tool_output() == "Error: 文件不在审查范围内"


def test_网络异常_收敛为失败信封_不抛出():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    resp = _mock_client(handler).get_file_content("src/App.java")
    assert resp.success is False
    assert resp.as_tool_output().startswith("Error:")


def test_创建会话失败_抛出_runtimeerror(monkeypatch):
    class _FakeResp:
        def raise_for_status(self):  # noqa: D401
            return None

        def json(self):
            return {"success": False, "error": "缺少 repo_path"}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return _FakeResp()

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    try:
        create_tool_session("http://toolserver", "/repo", ["a.java"])
        assert False, "应抛出 RuntimeError"
    except RuntimeError as e:
        assert "缺少 repo_path" in str(e)


def test_销毁会话_即使删除失败也关闭本地连接():
    closed = {"v": False}

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("server gone")

    client = _mock_client(handler)
    orig_close = client._client.close  # noqa: SLF001

    def _close():
        closed["v"] = True
        orig_close()

    client._client.close = _close  # noqa: SLF001
    destroy_tool_session(client)  # 不应抛出
    assert closed["v"] is True


def test_tool_response_默认空输出():
    assert ToolResponse(success=True).as_tool_output() == ""
    assert ToolResponse(success=False).as_tool_output() == "Error: unknown error"
