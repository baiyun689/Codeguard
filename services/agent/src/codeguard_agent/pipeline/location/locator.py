"""候选定位护栏：把 Reviewer 的位置线索绑定到 task diff 新增行。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, Field

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.schemas import DiscoveredIssue
from codeguard_agent.models.tasks import ReviewTask
from codeguard_agent.pipeline.prompting import render_prompt_template

LocationStatus = Literal[
    "verified", "corrected", "relocated", "deletion_anchor", "file_level"
]

_HUNK_HEADER = re.compile(
    r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@"
)
_PROMPT_FILE = Path(__file__).resolve().parents[2] / "prompts" / "candidate-relocation.txt"
_RELOCATION_BATCH_SIZE = 8


class _RelocationItem(BaseModel):
    candidate_id: str = Field(description="需要重新定位的候选 ID")
    location_snippet: str = Field(
        default="",
        description="从当前 patch 新增行原样复制的唯一连续代码片段；无法确定时返回空字符串",
    )


class _RelocationResponse(BaseModel):
    locations: list[_RelocationItem] = Field(
        default_factory=list, description="每个候选对应的重新定位结果"
    )


@dataclass(frozen=True)
class LocationRecord:
    """单个候选的定位结果诊断。"""

    index: int
    status: LocationStatus
    original_line: int
    resolved_line: int
    reason: str


@dataclass(frozen=True)
class LocationBatch:
    """一个 task 内候选的稳定定位结果。"""

    issues: tuple[DiscoveredIssue, ...]
    records: tuple[LocationRecord, ...]
    trace: tuple[tuple[str, str], ...] = ()


def _canonical_lines(text: str) -> tuple[str, ...]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized:
        return ()
    return tuple(line.rstrip() for line in normalized.splitlines())


def _norm_path(path: str) -> str:
    return path.replace("\\", "/").lower()


def _added_runs(
    patch: str,
    *,
    fallback_file: str,
) -> dict[str, tuple[tuple[tuple[int, str], ...], ...]]:
    """按文件解析各 hunk 的连续新增行，并保留 new-side 绝对行号。"""
    runs_by_file: dict[str, list[tuple[tuple[int, str], ...]]] = {}
    current: list[tuple[int, str]] = []
    current_file = "" if fallback_file == "<whole-diff>" else _norm_path(fallback_file)
    new_line: int | None = None

    def flush() -> None:
        nonlocal current
        if current and current_file:
            runs_by_file.setdefault(current_file, []).append(tuple(current))
        current = []

    for raw in patch.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith("diff --git "):
            flush()
            current_file = "" if fallback_file == "<whole-diff>" else _norm_path(fallback_file)
            new_line = None
            continue
        if raw.startswith("+++ b/"):
            flush()
            current_file = _norm_path(raw[len("+++ b/") :].split("\t", 1)[0])
            continue
        match = _HUNK_HEADER.match(raw)
        if match:
            flush()
            new_line = int(match.group("start"))
            continue
        if new_line is None:
            continue
        if raw.startswith("@@"):
            flush()
            new_line = None
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            current.append((new_line, raw[1:].rstrip()))
            new_line += 1
            continue
        flush()
        if raw.startswith("-") and not raw.startswith("---"):
            continue
        if raw.startswith(" "):
            new_line += 1
    flush()
    return {file: tuple(runs) for file, runs in runs_by_file.items()}


def _runs_for_file(
    runs_by_file: dict[str, tuple[tuple[tuple[int, str], ...], ...]],
    file: str,
) -> tuple[tuple[tuple[int, str], ...], ...]:
    normalized = _norm_path(file)
    if normalized in runs_by_file:
        return runs_by_file[normalized]
    basename = normalized.rsplit("/", 1)[-1]
    matches = [
        runs
        for path, runs in runs_by_file.items()
        if path.rsplit("/", 1)[-1] == basename
    ]
    return matches[0] if len(matches) == 1 else ()


def _match_snippet(snippet: str, runs: Sequence[Sequence[tuple[int, str]]]) -> int | None:
    lines = _canonical_lines(snippet)
    if not 1 <= len(lines) <= 5:
        return None
    matches: list[int] = []
    for run in runs:
        if len(run) < len(lines):
            continue
        for start in range(len(run) - len(lines) + 1):
            if tuple(item[1] for item in run[start : start + len(lines)]) == lines:
                matches.append(run[start][0])
    return matches[0] if len(matches) == 1 else None


def locate_issues(
    issues: Sequence[DiscoveredIssue],
    task: ReviewTask,
    *,
    llm: Any,
    structured_method: str,
    max_retries: int,
) -> LocationBatch:
    """验证并修正候选位置；复杂 fallback 对调用方保持隐藏。"""
    runs_by_file = _added_runs(task.patch, fallback_file=task.file)
    located: list[DiscoveredIssue | None] = [None] * len(issues)
    records: list[LocationRecord | None] = [None] * len(issues)
    unresolved: list[int] = []
    trace: list[tuple[str, str]] = []
    for index, issue in enumerate(issues):
        runs = _runs_for_file(runs_by_file, issue.file)
        changed_lines = {line for run in runs for line, _text in run}
        deletion_anchor_lines = {
            anchor.anchor_line for anchor in task.deletion_anchors
        }
        original_line = issue.line
        matched_line = _match_snippet(issue.location_snippet, runs)
        if matched_line is not None:
            status: LocationStatus = (
                "verified" if matched_line == original_line else "corrected"
            )
            located[index] = issue.model_copy(update={"line": matched_line})
            records[index] = LocationRecord(
                index=index,
                status=status,
                original_line=original_line,
                resolved_line=matched_line,
                reason="unique_added_snippet",
            )
            trace.append((
                "location_verified" if status == "verified" else "location_corrected",
                json.dumps(
                    {"index": index, "original_line": original_line, "line": matched_line},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ))
            continue
        if original_line in changed_lines:
            located[index] = issue
            records[index] = LocationRecord(
                index=index,
                status="verified",
                original_line=original_line,
                resolved_line=original_line,
                reason="reported_added_line",
            )
            trace.append((
                "location_verified",
                json.dumps(
                    {"index": index, "original_line": original_line, "line": original_line},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ))
            continue
        if original_line in deletion_anchor_lines and not issue.location_snippet.strip():
            located[index] = issue
            records[index] = LocationRecord(
                index=index,
                status="deletion_anchor",
                original_line=original_line,
                resolved_line=original_line,
                reason="reported_deletion_anchor",
            )
            trace.append((
                "location_deletion_anchor",
                json.dumps(
                    {"index": index, "line": original_line, "task_id": task.id},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ))
            continue
        unresolved.append(index)

    if llm is not None and unresolved:
        structured = llm.with_structured_output(
            _RelocationResponse,
            method=structured_method,
        )
        system_prompt = _PROMPT_FILE.read_text(encoding="utf-8")
        for offset in range(0, len(unresolved), _RELOCATION_BATCH_SIZE):
            chunk = unresolved[offset : offset + _RELOCATION_BATCH_SIZE]
            candidate_ids = {index: f"L{index + 1:03d}" for index in chunk}
            payload = {
                "task": {"id": task.id, "file": task.file, "patch": task.patch},
                "candidates": [
                    {
                        "candidate_id": candidate_ids[index],
                        "file": issues[index].file,
                        "line": issues[index].line,
                        "location_snippet": issues[index].location_snippet,
                        "type": issues[index].type,
                        "message": issues[index].message,
                        "suggestion": issues[index].suggestion,
                    }
                    for index in chunk
                ],
            }
            trace.append((
                "relocation_started",
                json.dumps(
                    {"candidate_ids": list(candidate_ids.values()), "task_id": task.id},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ))
            try:
                result = invoke_with_retry(
                    structured,
                    [
                        ("system", system_prompt),
                        (
                            "user",
                            render_prompt_template(
                                (_PROMPT_FILE.parent / "candidate-relocation-user.txt").read_text(
                                    encoding="utf-8"
                                ),
                                {"payload": json.dumps(payload, ensure_ascii=False)},
                            ),
                        ),
                    ],
                    max_retries=max_retries,
                )
                if result is not None and not isinstance(result, _RelocationResponse):
                    result = _RelocationResponse.model_validate(result)
            except Exception:  # noqa: BLE001 定位失败必须退化为文件级候选
                result = None
            returned = {
                item.candidate_id: item.location_snippet
                for item in (result.locations if result is not None else [])
            }
            relocated_ids: list[str] = []
            for index in chunk:
                issue = issues[index]
                snippet = returned.get(candidate_ids[index], "")
                runs = _runs_for_file(runs_by_file, issue.file)
                matched_line = _match_snippet(snippet, runs)
                if matched_line is None:
                    continue
                located[index] = issue.model_copy(
                    update={"line": matched_line, "location_snippet": snippet}
                )
                records[index] = LocationRecord(
                    index=index,
                    status="relocated",
                    original_line=issue.line,
                    resolved_line=matched_line,
                    reason="llm_snippet_verified",
                )
                relocated_ids.append(candidate_ids[index])
            trace.append((
                "relocation_completed",
                json.dumps(
                    {
                        "relocated_candidate_ids": relocated_ids,
                        "requested": len(chunk),
                        "task_id": task.id,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ))

    for index in unresolved:
        if located[index] is not None:
            continue
        issue = issues[index]
        located[index] = issue.model_copy(update={"line": 0})
        records[index] = LocationRecord(
            index=index,
            status="file_level",
            original_line=issue.line,
            resolved_line=0,
            reason="location_unresolved",
        )
        trace.append((
            "location_unresolved",
            json.dumps(
                {"index": index, "original_line": issue.line, "task_id": task.id},
                ensure_ascii=False,
                sort_keys=True,
            ),
        ))

    return LocationBatch(
        tuple(item for item in located if item is not None),
        tuple(item for item in records if item is not None),
        tuple(trace),
    )


__all__ = ["LocationBatch", "LocationRecord", "locate_issues"]
