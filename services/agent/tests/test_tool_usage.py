"""验证工具使用统计，区分实际跨 diff 源码访问与普通内容读取。"""

from __future__ import annotations
from dataclasses import dataclass
from evals.tool_usage import summarize_tool_usage


@dataclass
class _FakeCtx:
    """仿 engines.GatheredContext(只需 tool/args/content 三个属性)。"""

    tool: str
    args: str
    content: str


def test_empty_trace_is_all_blank():
    u = summarize_tool_usage([])
    assert u.tool_calls == 0
    assert u.tools_used == []
    assert u.symbols_read == []


def test_symbols_read_parsed_and_deduped_sorted():
    trace = [
        _FakeCtx(tool="read_symbol", args='{"symbol_id": "java:B#n()"}', content="..."),
        _FakeCtx(tool="read_symbol", args='{"symbol_id": "java:A#m()"}', content="..."),
        _FakeCtx(tool="read_symbol", args='{"symbol_id": "java:A#m()"}', content="..."),
    ]
    u = summarize_tool_usage(trace)
    assert u.symbols_read == ["java:A#m()", "java:B#n()"]
    assert u.tool_calls == 3


def test_malformed_args_falls_back_to_raw_string():
    trace = [_FakeCtx(tool="read_symbol", args="not-json", content="x")]
    u = summarize_tool_usage(trace)
    assert u.symbols_read == ["not-json"]
