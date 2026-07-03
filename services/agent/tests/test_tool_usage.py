"""工具使用画像 summarize_tool_usage 的单测(纯函数,不碰网络/管线)。

重点验证 ADR-022 关心的判别力:能否如实区分"读到 callers 信息"与"只看了普通内容/没调工具"。
find_callers 取代了原 get_repo_map 的调用方追踪能力。
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
    assert u.repomap_called is False
    assert u.repomap_caller_section_read is False
    assert u.files_read == []


def test_find_callers_detected_as_caller_info():
    trace = [
        _FakeCtx(
            tool="find_callers",
            args='{"query": "src/Foo.java#bar"}',
            content="# find_callers 查询结果\n| 1 | Payment.java:234 | `price = calc()` |",
        )
    ]
    u = summarize_tool_usage(trace)
    assert u.repomap_called is True
    assert u.repomap_caller_section_read is True
    assert "find_callers" in u.tools_used


def test_find_callers_empty_result_still_called():
    trace = [_FakeCtx(tool="find_callers", args='{"query": "src/X.java#y"}', content="未找到直接调用方")]
    u = summarize_tool_usage(trace)
    assert u.repomap_called is True
    assert u.repomap_caller_section_read is False


def test_files_read_parsed_and_deduped_sorted():
    trace = [
        _FakeCtx(tool="get_file_content", args='{"file_path": "src/B.java"}', content="..."),
        _FakeCtx(tool="get_file_content", args='{"file_path": "src/A.java"}', content="..."),
        _FakeCtx(tool="get_file_content", args='{"file_path": "src/A.java"}', content="..."),
    ]
    u = summarize_tool_usage(trace)
    assert u.files_read == ["src/A.java", "src/B.java"]
    assert u.tool_calls == 3
    assert u.repomap_called is False


def test_malformed_args_falls_back_to_raw_string():
    trace = [_FakeCtx(tool="get_file_content", args="not-json", content="x")]
    u = summarize_tool_usage(trace)
    assert u.files_read == ["not-json"]


def test_structured_response_sentinel_excluded():
    trace = [
        _FakeCtx(tool="find_callers", args='{"query": "a#b"}', content="# find_callers\n| 1 | X.java:1 | `...` |"),
        _FakeCtx(tool="ReviewResult", args='{"issues": []}', content="结构化结果,非工具上下文"),
        _FakeCtx(tool="ReviewResult", args='{"issues": [1]}', content="另一审查员的结构化结果"),
    ]
    u = summarize_tool_usage(trace)
    assert "find_callers" in u.tools_used
    assert u.tool_calls == 1  # 只数真工具
    assert u.repomap_caller_section_read is True
