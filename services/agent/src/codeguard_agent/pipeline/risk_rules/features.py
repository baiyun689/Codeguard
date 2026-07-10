"""Deterministic text features extracted from one review task patch."""

from __future__ import annotations

import re
from dataclasses import dataclass

from codeguard_agent.models.tasks import ReviewTask


_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True)
class DiffFeatures:
    path: str
    added_lines: tuple[tuple[int, str], ...]
    deleted_lines: tuple[str, ...]
    context_lines: tuple[str, ...]
    has_added: bool
    has_deleted: bool
    has_changed: bool


def extract_features(task: ReviewTask) -> DiffFeatures:
    """Extract added, deleted, and context text from one unified-diff task."""
    added: list[tuple[int, str]] = []
    deleted: list[str] = []
    context: list[str] = []
    header_match = _HUNK_HEADER.match(task.hunk_header)
    new_line = int(header_match.group(1)) if header_match else 0
    fallback = header_match is None
    in_hunk = True
    changed_line_index = 0
    for line in task.patch.splitlines():
        match = _HUNK_HEADER.match(line)
        if match:
            new_line = int(match.group(1))
            in_hunk = True
            fallback = False
            continue
        if not in_hunk:
            continue
        if line.startswith("@@") or line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("\\ No newline at end of file"):
            continue
        if line.startswith("+"):
            if fallback and changed_line_index < len(task.changed_lines):
                new_line = task.changed_lines[changed_line_index]
            changed_line_index += 1
            added.append((new_line, line[1:]))
            new_line += 1
        elif line.startswith("-"):
            deleted.append(line[1:])
        elif line.startswith(" "):
            context.append(line[1:])
            new_line += 1

    has_added = bool(added)
    has_deleted = bool(deleted)
    return DiffFeatures(
        path=task.file,
        added_lines=tuple(added),
        deleted_lines=tuple(deleted),
        context_lines=tuple(context),
        has_added=has_added,
        has_deleted=has_deleted,
        has_changed=has_added and has_deleted,
    )
