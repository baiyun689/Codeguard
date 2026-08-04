"""工具使用画像 summarize_tool_usage 的单测(纯函数,不碰网络/管线)。

重点验证 ADR-022 关心的判别力:能否如实区分"真调工具导航(读到了 diff 之外的文件)"
与"只看了普通内容/没调工具"。
"""

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
    assert u.files_read == []


def test_files_read_parsed_and_deduped_sorted():
    trace = [
        _FakeCtx(tool="get_file_content", args='{"file_path": "src/B.java"}', content="..."),
        _FakeCtx(tool="get_file_content", args='{"file_path": "src/A.java"}', content="..."),
        _FakeCtx(tool="get_file_content", args='{"file_path": "src/A.java"}', content="..."),
    ]
    u = summarize_tool_usage(trace)
    assert u.files_read == ["src/A.java", "src/B.java"]
    assert u.tool_calls == 3


def test_malformed_args_falls_back_to_raw_string():
    trace = [_FakeCtx(tool="get_file_content", args="not-json", content="x")]
    u = summarize_tool_usage(trace)
    assert u.files_read == ["not-json"]


def test_structured_response_sentinel_excluded():
    trace = [
        _FakeCtx(tool="inspect_structure", args='{"symbol_id": "java:demo.Service#run()"}', content="符号事实"),
        _FakeCtx(tool="ReviewResult", args='{"issues": []}', content="结构化结果,非工具上下文"),
        _FakeCtx(tool="ReviewResult", args='{"issues": [1]}', content="另一审查员的结构化结果"),
    ]
    u = summarize_tool_usage(trace)
    assert "inspect_structure" in u.tools_used
    assert u.tool_calls == 1  # 只数真工具
