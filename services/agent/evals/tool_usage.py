"""从工具证据提取调用次数、所读符号及跨 diff 访问情况。

输入为包含 tool、args、content 属性的工具记录，按工具和参数去重。
只统计首次实际执行的工具证据，不重复计算缓存复用，不发起网络请求。
"""

from __future__ import annotations
import json
from typing import Any
from evals.schema import ToolUsage


def _symbol_from_args(args: Any) -> str:
    """从 get_file_content 的入参摘要里取出稳定 symbol_id。

    args 通常是 ``_summarize_args`` 产出的 JSON 串(如 ``{"symbol_id": "java:A#m()"}``);
    解析失败则回退原串,保证健壮(画像是锦上添花,不该因脏数据抛断)。
    """
    if not args:
        return ""
    try:
        obj = json.loads(args)
        if isinstance(obj, dict):
            return str(obj.get("symbol_id") or "").strip()
    except (json.JSONDecodeError, TypeError):
        pass
    return str(args).strip()


def summarize_tool_usage(trace: list[Any]) -> ToolUsage:
    """把一条用例的工具上下文 trace 汇成 ToolUsage 画像。

    空 trace 返回全空画像(tool_calls=0);调用方(run_once)据此决定是否落 None。
    """
    tools = sorted({t.tool for t in trace if getattr(t, "tool", "")})
    symbols = sorted(
        {
            _symbol_from_args(getattr(t, "args", ""))
            for t in trace
            if getattr(t, "tool", "") == "read_symbol"
        }
        - {""}
    )
    return ToolUsage(tool_calls=len(trace), tools_used=tools, symbols_read=symbols)
