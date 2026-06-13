"""摘要软分派与按域裁剪的工程正确性测试。

这两段都是确定性逻辑:file_focus 如何归一化(含"未分派→全发"兜底)、裁剪 diff 何时采用何时回退。
LLM 产出的 file_focus 本身不确定(由 evals 量化),但归一化与裁剪的行为可以死磕。
"""

from __future__ import annotations

from codeguard_agent.pipeline.stages.reviewer_stage import _effective_diff
from codeguard_agent.pipeline.stages.summary import _normalise_file_groups


# ---------------------------------------------------------------------------
# _normalise_file_groups:软路由的兜底
# ---------------------------------------------------------------------------


def test_键模糊匹配到三个规范维度():
    focus = {"security": ["A.java"], "logic-review": ["B.java"], "quality_check": ["C.java"]}
    groups = _normalise_file_groups(focus, ["A.java", "B.java", "C.java"])
    assert groups["security"] == ["A.java"]
    assert groups["logic"] == ["B.java"]
    assert groups["quality"] == ["C.java"]


def test_未分派文件默认归入所有维度():
    # D.java 没被任何维度分派 → 兜底进全部三个维度
    focus = {"security": ["A.java"]}
    groups = _normalise_file_groups(focus, ["A.java", "D.java"])
    assert "D.java" in groups["security"]
    assert groups["logic"] == ["D.java"]
    assert groups["quality"] == ["D.java"]


def test_空focus退化为全发():
    groups = _normalise_file_groups({}, ["A.java", "B.java"])
    for name in ("security", "logic", "quality"):
        assert groups[name] == ["A.java", "B.java"]


def test_臆造路径被过滤():
    # LLM 给了一个不在本次变更里的文件 → 丢弃
    focus = {"security": ["A.java", "Ghost.java"]}
    groups = _normalise_file_groups(focus, ["A.java"])
    assert groups["security"] == ["A.java"]


def test_无法识别的键被忽略():
    focus = {"performance": ["A.java"]}  # 不是三维度之一
    groups = _normalise_file_groups(focus, ["A.java"])
    # A.java 未被有效分派 → 兜底全发
    assert groups["security"] == ["A.java"]
    assert groups["logic"] == ["A.java"]
    assert groups["quality"] == ["A.java"]


# ---------------------------------------------------------------------------
# _effective_diff:裁剪只在显著更小时采用,否则回退整份
# ---------------------------------------------------------------------------


def _big(n: int) -> str:
    return "x" * n


def test_裁剪显著更小时采用():
    full = _big(1000)
    file_diffs = {"A.java": _big(100), "B.java": _big(800)}
    # 只关心 A.java(100 字符,远小于整份 1000 的 85%)
    out = _effective_diff(full, file_diffs, ["A.java"])
    assert out == file_diffs["A.java"]


def test_裁剪收益不足时回退整份():
    full = _big(1000)
    file_diffs = {"A.java": _big(900)}  # 900 >= 1000*0.85,收益不足
    out = _effective_diff(full, file_diffs, ["A.java"])
    assert out == full


def test_无file_diffs或未分派时用整份():
    full = _big(1000)
    assert _effective_diff(full, {}, ["A.java"]) == full
    assert _effective_diff(full, {"A.java": _big(10)}, None) == full


def test_分派文件无对应diff片段时回退整份():
    full = _big(1000)
    file_diffs = {"A.java": _big(10)}
    # 关心的是 B.java,但 file_diffs 里没有它 → 裁剪结果空 → 回退整份
    out = _effective_diff(full, file_diffs, ["B.java"])
    assert out == full
