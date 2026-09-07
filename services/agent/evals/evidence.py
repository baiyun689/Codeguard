"""共享的用户可读证据位置匹配规则。

评测的案例匹配和回放分析必须使用同一套结构化位置规则。文本根因只用于展示，
不能因为模型在根因里猜中了文件名就伪造一条证据命中。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


_LINE_ANCHOR = re.compile(r"^(.+?):(\d+)$")


def normalize(value: object) -> str:
    """统一路径、大小写和空白，供锚点和报告字段比较。"""

    return re.sub(r"\s+", "", str(value or "").replace("\\", "/").strip().lower())


def file_matches(reported_file: object, expected_file: object) -> bool:
    """按 basename 或路径后缀匹配文件，兼容不同仓库根目录。"""

    reported = normalize(reported_file)
    expected = normalize(expected_file)
    if not reported or not expected:
        return False
    reported_base = reported.rsplit("/", 1)[-1]
    expected_base = expected.rsplit("/", 1)[-1]
    return (
        reported_base == expected_base
        or reported.endswith(expected)
        or expected.endswith(reported)
    )


def _value(location: object, key: str, default: Any = "") -> Any:
    if isinstance(location, Mapping):
        return location.get(key, default)
    return getattr(location, key, default)


def _line_hit(location: object, anchor_line: int, tolerance: int) -> bool:
    start = int(_value(location, "start_line", 0) or 0)
    end = int(_value(location, "end_line", start) or start)
    if start <= anchor_line <= max(start, end):
        return True
    return abs(start - anchor_line) <= tolerance


def _anchor_hit(location: object, anchor: str, tolerance: int) -> bool:
    normalized = normalize(anchor)
    if not normalized:
        return False
    match = _LINE_ANCHOR.match(normalized)
    location_file = _value(location, "file", "")
    if match:
        return file_matches(location_file, match.group(1)) and _line_hit(
            location, int(match.group(2)), tolerance
        )
    searchable = " ".join(
        str(_value(location, key, ""))
        for key in ("file", "symbol", "relation")
    )
    return normalized in normalize(searchable)


def _is_external(location: object, expected_file: object) -> bool:
    kind = str(_value(location, "kind", ""))
    return kind != "changed_code" and not file_matches(
        _value(location, "file", ""), expected_file
    )


def evidence_matches(
    *,
    locations: Iterable[object],
    anchors: Iterable[object],
    evidence_scope: str = "local",
    expected_file: object = "",
    tolerance: int = 3,
) -> bool:
    """判断结构化 ``EvidenceLocation`` 是否命中标注锚点。

    ``root_cause`` 不在参数中：根因是展示文本，不是可验证的来源。对于跨文件
    标答，命中的锚点和外部位置必须是同一个 location，避免“猜中目标文件名 +
    另一个无关路径”被误计为证据。
    """

    location_list = list(locations)
    anchor_list = [str(anchor) for anchor in anchors if str(anchor).strip()]
    if not anchor_list:
        return False
    tolerance = max(0, int(tolerance or 0))
    for anchor in anchor_list:
        for location in location_list:
            if not _anchor_hit(location, anchor, tolerance):
                continue
            if evidence_scope == "cross_file" and not _is_external(location, expected_file):
                continue
            return True
    return False


__all__ = ["evidence_matches", "file_matches", "normalize"]
