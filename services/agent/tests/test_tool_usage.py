"""工具使用画像 summarize_tool_usage 的单测(纯函数,不碰网络/管线)。

重点验证 ADR-022 关心的判别力:能否如实区分"读到 callers 段"与"只看了普通地图/没调工具"。
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


def test_repomap_caller_section_detected():
    # 地图返回里含"直接调用方"表头 → callers 段被读到。
    trace = [
        _FakeCtx(
            tool="get_repo_map",
            args="{}",
            content="# 代码地图\nfoo()\n# 直接调用方(callers of changed code)\nBar.call()",
        )
    ]
    u = summarize_tool_usage(trace)
    assert u.repomap_called is True
    assert u.repomap_caller_section_read is True
    assert u.tools_used == ["get_repo_map"]


def test_repomap_without_caller_section():
    # 调了 get_repo_map 但返回里没有 callers 段 → 标志为 False(不能误记成读到了)。
    trace = [_FakeCtx(tool="get_repo_map", args="{}", content="# 代码地图\nfoo()")]
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
    # 入参不是合法 JSON 时回退原串,不抛断。
    trace = [_FakeCtx(tool="get_file_content", args="not-json", content="x")]
    u = summarize_tool_usage(trace)
    assert u.files_read == ["not-json"]


def test_structured_response_sentinel_excluded():
    # create_agent 的结构化输出伪工具(ReviewResult)不是真工具,必须从画像剔除,
    # 否则虚高 tool_calls、污染 tools_used(实测 caller 案踩到的坑)。
    trace = [
        _FakeCtx(tool="get_repo_map", args="{}", content="# 直接调用方(callers)\nBar.call()"),
        _FakeCtx(tool="ReviewResult", args='{"issues": []}', content="结构化结果,非工具上下文"),
        _FakeCtx(tool="ReviewResult", args='{"issues": [1]}', content="另一审查员的结构化结果"),
    ]
    u = summarize_tool_usage(trace)
    assert u.tools_used == ["get_repo_map"]  # ReviewResult 不计入
    assert u.tool_calls == 1  # 只数真工具
    assert u.repomap_caller_section_read is True
