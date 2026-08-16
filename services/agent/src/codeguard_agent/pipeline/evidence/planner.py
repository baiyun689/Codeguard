"""候选、任务与证据请求的纯组装层。

Dossier 组装只做确定性绑定，不产生证据请求——请求由 graph 节点
在取证阶段经 verifier 重放/配方生成。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.tasks import ReviewTask, TaskContextBundle
from codeguard_agent.pipeline.risk import task_prep

if TYPE_CHECKING:
    from codeguard_agent.pipeline.council.dedup import CandidateGroup


@dataclass(frozen=True)
class CandidateDossier:
    """规划单个候选所需的只读快照，不进入 graph State。"""

    candidate: CandidateIssue
    task: ReviewTask
    context_bundle: TaskContextBundle | None
    candidate_group: CandidateGroup | None = None


@dataclass(frozen=True)
class CandidateBindingFailure:
    """无法安全绑定到唯一 task 的候选。"""

    candidate: CandidateIssue
    reason: str


@dataclass(frozen=True)
class DossierAssembly:
    """按候选稳定顺序组装的有效 dossier 与显式失败。"""

    dossiers: tuple[CandidateDossier, ...]
    failures: tuple[CandidateBindingFailure, ...]
    trace: tuple[tuple[str, str], ...]


def _stable_json(detail: dict[str, object]) -> str:
    return json.dumps(detail, ensure_ascii=False, sort_keys=True)


def assemble_dossiers(
    candidates: Sequence[CandidateIssue],
    tasks: Sequence[ReviewTask],
    bundles: Mapping[str, TaskContextBundle],
    candidate_groups: Sequence[CandidateGroup] = (),
) -> DossierAssembly:
    """把 graph state 关联为候选级只读快照，并显式保留绑定失败。"""
    tasks_by_id: dict[str, list[ReviewTask]] = {}
    for task in tasks:
        tasks_by_id.setdefault(task.id, []).append(task)
    groups_by_candidate = {
        member.id: group
        for group in candidate_groups
        for member in group.members
    }

    dossiers: list[CandidateDossier] = []
    failures: list[CandidateBindingFailure] = []
    trace: list[tuple[str, str]] = []
    for candidate in candidates:
        matches = tasks_by_id.get(candidate.task_id, [])
        if len(matches) != 1:
            reason = "missing_task" if not matches else "ambiguous_task"
            failures.append(CandidateBindingFailure(candidate, reason))
            trace.append(
                (
                    "candidate_binding_failed",
                    _stable_json(
                        {
                            "candidate_id": candidate.id,
                            "task_id": candidate.task_id,
                            "reason": reason,
                        }
                    ),
                )
            )
            continue
        task = matches[0]
        if not task_prep.file_matches_task(candidate.file, task):
            reason = "file_mismatch"
            failures.append(CandidateBindingFailure(candidate, reason))
            trace.append(
                (
                    "candidate_binding_failed",
                    _stable_json(
                        {
                            "candidate_id": candidate.id,
                            "task_id": candidate.task_id,
                            "reason": reason,
                        }
                    ),
                )
            )
            continue
        dossiers.append(
            CandidateDossier(
                candidate=candidate,
                task=task,
                context_bundle=bundles.get(task.id),
                candidate_group=groups_by_candidate.get(candidate.id),
            )
        )
    return DossierAssembly(tuple(dossiers), tuple(failures), tuple(trace))


__all__ = [
    "CandidateBindingFailure",
    "CandidateDossier",
    "DossierAssembly",
    "assemble_dossiers",
]
