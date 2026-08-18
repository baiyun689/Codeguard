"""从管线工具上下文 trace 提炼"工具使用画像"(评测可观测性)。

回答 ADR-022 没答上的问题:审查员到底有没有调工具、有没有真读到 diff 之外的上下文——
还是纯靠 diff 推理蒙对。纯函数,吃 GatheredContext 形状的对象
(带 ``.tool`` / ``.args`` / ``.content`` 属性),与管线/网络解耦,可独立单测。

注意:输入是编排器从证据 Artifact 派生的画像(见 orchestrator._artifact_tool_profile,
仅首次真实执行 EXECUTED 的 TOOL_CALL、按 (tool, args) 去重),故 tool_calls 是
"去重后取得有效上下文的调用条数",非原始调用次数(见 ToolUsage 文档)。
"""

from __future__ import annotations

import json
from typing import Any

from evals.schema import ToolUsage


def _file_from_args(args: Any) -> str:
    """从 get_file_content 的入参摘要里取出文件路径。

    args 通常是 ``_summarize_args`` 产出的 JSON 串(如 ``{"file_path": "a/B.java"}``);
    解析失败则回退原串,保证健壮(画像是锦上添花,不该因脏数据抛断)。
    """
    if not args:
        return ""
    try:
        obj = json.loads(args)
        if isinstance(obj, dict):
            return str(obj.get("file_path") or obj.get("path") or "").strip()
    except (json.JSONDecodeError, TypeError):
        pass
    return str(args).strip()


def summarize_tool_usage(trace: list[Any]) -> ToolUsage:
    """把一条用例的工具上下文 trace 汇成 ToolUsage 画像。

    空 trace 返回全空画像(tool_calls=0);调用方(run_once)据此决定是否落 None。
    """
    tools = sorted({t.tool for t in trace if getattr(t, "tool", "")})
    files = sorted(
        {
            _file_from_args(getattr(t, "args", ""))
            for t in trace
            if getattr(t, "tool", "") == "get_file_content"
        }
        - {""}
    )
    return ToolUsage(
        tool_calls=len(trace),
        tools_used=tools,
        files_read=files,
    )
