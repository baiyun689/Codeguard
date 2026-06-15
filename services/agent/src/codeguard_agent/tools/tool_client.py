"""Java 工具服务的同步 HTTP 客户端 + 会话生命周期。

保持**同步**(httpx.Client 而非 AsyncClient):阶段 3 的 ReAct 在现有线程池里 fan-out,
不引入 async(见 ROADMAP "async 留到 chunking 再切" 的岔路口、design.md D4)。

职责边界:本模块只发请求、解析统一信封;真正的文件读取与安全护栏都在 Java 侧
(design.md D0:Python 编排、Java 护栏)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger("codeguard")


@dataclass(frozen=True)
class ToolResponse:
    """工具调用的统一信封解析结果。"""

    success: bool
    result: str | None = None
    error: str | None = None

    def as_tool_output(self) -> str:
        """转成给 LLM Agent 看的字符串:成功给 result,失败显式标注 Error 让 Agent 能感知并调整。"""
        if self.success:
            return self.result or ""
        return f"Error: {self.error or 'unknown error'}"


class ToolClient:
    """绑定到某个工具会话的客户端。

    一次审查创建一个会话,会话内的多个并行审查员共享同一个 ToolClient
    (httpx.Client 线程安全,工具均为只读,共享安全)。
    """

    def __init__(self, base_url: str, session_id: str, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._session_id = session_id
        self._client = httpx.Client(timeout=timeout)

    @property
    def session_id(self) -> str:
        return self._session_id

    def _post_tool(self, name: str, payload: dict) -> ToolResponse:
        """调用某个工具:POST /api/v1/tools/{name},带 X-Session-Id。"""
        try:
            resp = self._client.post(
                f"{self._base_url}/api/v1/tools/{name}",
                headers={"X-Session-Id": self._session_id},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return ToolResponse(
                success=bool(data.get("success")),
                result=data.get("result"),
                error=data.get("error"),
            )
        except Exception as exc:  # noqa: BLE001 网络/服务异常统一收敛成失败信封,不让单次工具失败炸掉 Agent
            logger.warning("工具调用 %s 失败: %s", name, exc)
            return ToolResponse(success=False, error=str(exc))

    def get_file_content(self, file_path: str) -> ToolResponse:
        return self._post_tool("get_file_content", {"file_path": file_path})

    def get_repo_map(self) -> ToolResponse:
        """获取与本次改动相关的签名级代码地图(无入参,由会话的 diff 种子驱动)。"""
        return self._post_tool("get_repo_map", {})

    def delete_session(self) -> None:
        """请求服务端释放本会话(复用同一连接)。"""
        self._client.delete(f"{self._base_url}/api/v1/tools/session/{self._session_id}")

    def close(self) -> None:
        self._client.close()


def create_tool_session(
    base_url: str,
    repo_path: str,
    allowed_files: list[str],
    timeout: float = 30.0,
) -> ToolClient:
    """在 Java 工具服务上创建会话,返回绑定该会话的 ToolClient。

    repo_path 应为绝对路径(Java 侧据此解析文件相对路径并做沙箱校验)。
    失败时抛 RuntimeError,由调用方决定是否回退到无工具直连。
    """
    normalized = base_url.rstrip("/")
    payload = {"repo_path": repo_path, "allowed_files": allowed_files}
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(f"{normalized}/api/v1/tools/session", json=payload)
        resp.raise_for_status()
        data = resp.json()

    if not data.get("success"):
        raise RuntimeError(f"创建工具会话失败: {data.get('error', 'unknown error')}")
    session_id = data.get("session_id")
    if not session_id:
        raise RuntimeError("创建工具会话失败:返回缺少 session_id")
    return ToolClient(normalized, str(session_id), timeout=timeout)


def destroy_tool_session(client: ToolClient) -> None:
    """销毁服务端会话,并关闭本地 HTTP 连接(无论销毁是否成功都关闭本地连接)。"""
    try:
        client.delete_session()
    except Exception as exc:  # noqa: BLE001 销毁失败不致命:会话本就有 TTL 会自动回收
        logger.warning("销毁工具会话失败(将由服务端 TTL 兜底回收): %s", exc)
    finally:
        client.close()
