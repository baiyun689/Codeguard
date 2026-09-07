"""Java 工具服务的同步 HTTP 客户端 + 会话生命周期。

保持**同步**(httpx.Client 而非 AsyncClient):阶段 3 的 ReAct 在现有线程池里 fan-out,
不引入 async(见 ROADMAP "async 留到 chunking 再切" 的岔路口、design.md D4)。

职责边界:本模块只发请求、解析统一信封;真正的 symbol 源码读取与安全护栏都在 Java 侧
(design.md D0:Python 编排、Java 护栏)。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from html import unescape

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

    def __init__(
        self,
        base_url: str,
        session_id: str,
        timeout: float = 30.0,
        revision: str = "",
        token: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._session_id = session_id
        self._revision = revision
        self._token = token
        self._client = httpx.Client(timeout=timeout)

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def revision(self) -> str:
        """本会话绑定的仓库 revision(证据账本据此做内容寻址,见 Evidence Ledger 设计)。"""
        return self._revision

    def _post_tool(self, name: str, payload: dict) -> ToolResponse:
        """调用某个工具:POST /api/v1/tools/{name},带 X-Session-Id。"""
        try:
            resp = self._client.post(
                f"{self._base_url}/api/v1/tools/{name}",
                headers={
                    "X-Session-Id": self._session_id,
                    "X-Codeguard-Tool-Token": self._token,
                },
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

    def get_file_content(
        self,
        symbol_id: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        cursor: str | None = None,
    ) -> ToolResponse:
        """读取一个已由图谱解析出的 symbol 源码片段。

        源码工具不再接受任意文件路径；Gateway 会根据 snapshot 中的稳定
        ``symbol_id`` 解析方法、类型或字段的声明范围，并施加大小护栏。
        """
        query: dict[str, object] = {"symbol_id": unescape(symbol_id)}
        if start_line is not None:
            query["start_line"] = start_line
        if end_line is not None:
            query["end_line"] = end_line
        if cursor is not None:
            query["cursor"] = cursor
        return self._post_tool("get_file_content", {"query": json.dumps(query, ensure_ascii=False)})

    def resolve_change_context(self, changes: list[dict]) -> ToolResponse:
        """批量把变更文件/行解析为稳定图谱符号。"""
        return self._post_tool(
            "resolve_change_context",
            {"query": json.dumps({"changes": changes}, ensure_ascii=False)},
        )

    def inspect_change_impact(
        self,
        symbol_id: str,
        *,
        max_depth: int | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> ToolResponse:
        query: dict[str, object] = {"symbol_id": unescape(symbol_id)}
        if max_depth is not None:
            query["max_depth"] = max_depth
        if limit is not None:
            query["limit"] = limit
        if cursor is not None:
            query["cursor"] = cursor
        payload = unescape(symbol_id) if not any(
            value is not None for value in (max_depth, limit, cursor)
        ) else json.dumps(query, ensure_ascii=False)
        return self._post_tool("inspect_change_impact", {"query": payload})

    def inspect_structure(
        self,
        symbol_id: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> ToolResponse:
        query: dict[str, object] = {"symbol_id": unescape(symbol_id)}
        if limit is not None:
            query["limit"] = limit
        if cursor is not None:
            query["cursor"] = cursor
        payload = unescape(symbol_id) if not any(
            value is not None for value in (limit, cursor)
        ) else json.dumps(query, ensure_ascii=False)
        return self._post_tool("inspect_structure", {"query": payload})

    def inspect_path(
        self,
        symbol_id: str,
        path_kind: str,
        max_depth: int = 3,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> ToolResponse:
        """查询有界下游行为或安全路径。"""
        symbol_id = unescape(symbol_id)
        if path_kind not in {"behavior", "security"}:
            return ToolResponse(success=False, error="invalid_path_kind")
        if not isinstance(max_depth, int) or isinstance(max_depth, bool) or not 1 <= max_depth <= 3:
            return ToolResponse(success=False, error="invalid_max_depth")
        return self._post_tool(
            "inspect_path",
            {
                "query": json.dumps(
                    {
                        "symbol_id": symbol_id,
                        "path_kind": path_kind,
                    "max_depth": max_depth,
                    **({"limit": limit} if limit is not None else {}),
                    **({"cursor": cursor} if cursor is not None else {}),
                    },
                    ensure_ascii=False,
                )
            },
        )

    def delete_session(self) -> None:
        """请求服务端释放本会话(复用同一连接)。"""
        self._client.delete(
            f"{self._base_url}/api/v1/tools/session/{self._session_id}",
            headers={"X-Codeguard-Tool-Token": self._token},
        )

    def close(self) -> None:
        self._client.close()


def create_tool_session(
    base_url: str,
    repo_path: str,
    timeout: float = 30.0,
    revision: str = "",
    token: str = "",
) -> ToolClient:
    """在 Java 工具服务上创建会话,返回绑定该会话的 ToolClient。
    repo_path 应为绝对路径(Java 侧据此创建受限 ProjectSnapshot)。
    失败时抛 RuntimeError,由调用方决定是否回退到无工具直连。
    """
    if not token:
        raise RuntimeError("CODEGUARD_TOOL_SERVER_TOKEN 未配置")
    normalized = base_url.rstrip("/")
    payload = {
        "repo_path": repo_path,
        "revision": revision,
    }
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(
            f"{normalized}/api/v1/tools/session",
            headers={"X-Codeguard-Tool-Token": token},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    if not data.get("success"):
        raise RuntimeError(f"创建工具会话失败: {data.get('error', 'unknown error')}")
    session_id = data.get("session_id")
    if not session_id:
        raise RuntimeError("创建工具会话失败:返回缺少 session_id")
    return ToolClient(normalized, str(session_id), timeout=timeout, revision=revision, token=token)


def destroy_tool_session(client: ToolClient) -> None:
    """销毁服务端会话,并关闭本地 HTTP 连接(无论销毁是否成功都关闭本地连接)。"""
    try:
        client.delete_session()
    except Exception as exc:  # noqa: BLE001 销毁失败不致命:会话本就有 TTL 会自动回收
        logger.warning("销毁工具会话失败(将由服务端 TTL 兜底回收): %s", exc)
    finally:
        client.close()
