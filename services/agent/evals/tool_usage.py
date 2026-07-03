"""从管线工具上下文 trace 提炼"工具使用画像"(评测可观测性)。

回答 ADR-022 没答上的问题:审查员到底有没有调工具、有没有真用到 find_callers 获取
调用方信息——还是纯靠 diff 推理蒙对。纯函数,吃 GatheredContext 列表
(或任何带 ``.tool`` / ``.args`` / ``.content`` 属性的对象),与管线/网络解耦,可独立单测。

注意:输入是管线**去重后**的 gathered_context(见 ReviewerStage._dedup_context),
故 tool_calls 是"去重后取得有效上下文的调用条数",非原始调用次数(见 ToolUsage 文档)。
"""

from __future__ import annotations

import json
from typing import Any

from codeguard_agent.models.schemas import ReviewResult

from evals.schema import ToolUsage

# find_callers 返回的调用方标记:输出中包含"find_callers"表头即说明调用了调用方查询工具。
# find_callers 永远返回调用方信息(有则列表,无则"未找到"),地图输出中含"调用方"标记说明读到了。
_CALLER_SECTION_MARKER = "find_callers"

# create_agent(response_format=ReviewResult) 把"产出结构化结果"实现为一次同名工具调用,
# 它会以 ToolMessage 形式混进 gathered_context。那不是真去取外部上下文的工具,
# 统计画像时必须剔除(否则虚高 tool_calls、污染 tools_used)。用类名保持与 response_format 同步。
_STRUCTURED_SENTINELS = {ReviewResult.__name__}


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
    先剔除结构化输出伪工具(ReviewResult),只在"真去取上下文的工具"上统计。
    """
    trace = [t for t in trace if getattr(t, "tool", "") not in _STRUCTURED_SENTINELS]
    tools = sorted({t.tool for t in trace if getattr(t, "tool", "")})
    repomap_called = "find_callers" in tools
    caller_read = any(
        getattr(t, "tool", "") == "find_callers"
        and _CALLER_SECTION_MARKER in (getattr(t, "content", "") or "")
        for t in trace
    )
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
        repomap_called=repomap_called,
        repomap_caller_section_read=caller_read,
        files_read=files,
    )
