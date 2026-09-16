from __future__ import annotations
import threading
import time
from types import SimpleNamespace
import pytest
from codeguard_agent.models.tasks import (
    ReviewerKind,
    ReviewTask,
    TaskSelection,
    TaskSymbolContext,
)
from codeguard_agent.models.evidence import EvidenceCatalog
from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks.symbols import ResolvedSymbol, SymbolResolutionStatus
from codeguard_agent.pipeline.controlled.assessment import collapse_candidate_duplicates
from codeguard_agent.pipeline.orchestration.graph import _assemble_state_dossiers
from codeguard_agent.tools.tool_client import ToolResponse


class _GraphClient:
    def __init__(self) -> None:
        self.path_calls: list[tuple[str, str, int]] = []

    def inspect_path(
        self, symbol_id: str, path_kind: str, max_depth: int = 3
    ) -> ToolResponse:
        self.path_calls.append((symbol_id, path_kind, max_depth))
        return ToolResponse(
            True,
            '{"schema_version":2,"outcome":"found","coverage":"complete","source_scope":"MAIN","subject_symbol_id":"s1","symbols":[{"id":"s1","kind":"METHOD","source_set":"MAIN"},{"id":"s2","kind":"METHOD","source_set":"MAIN"}],"relationships":[{"sourceId":"s1","targetId":"s2","kind":"CALLS","source_set":"MAIN","resolution":"RESOLVED"}],"unresolved_relationships":[],"unresolved_count":0,"limitations":[]}',
        )

    def inspect_structure(self, symbol_id: str) -> ToolResponse:
        return ToolResponse(False, error="not_used")

    def read_symbol(self, symbol_id: str, **kwargs) -> ToolResponse:
        return ToolResponse(True, "class A { void changed() { call(); } }")


class _Structured:
    def __init__(self, result):
        self.result = result

    def invoke(self, _messages):
        return self.result


class _TriageLLM:
    def __init__(self, result):
        self.result = result

    def with_structured_output(self, _schema, method=None):
        return _Structured(self.result)

    def inspect_change_impact(self, symbol_id: str) -> ToolResponse:
        return ToolResponse(False, error="not_used")

    def read_symbol(self, symbol_id: str, **kwargs) -> ToolResponse:
        return ToolResponse(False, error="not_used")


def _context() -> TaskSymbolContext:
    return TaskSymbolContext(
        task_id="A.java#h0",
        status=SymbolResolutionStatus.RESOLVED,
        symbols=(
            ResolvedSymbol(
                file="A.java",
                symbol_id="s1",
                kind="METHOD",
                start_line=1,
                end_line=5,
                source_set="MAIN",
            ),
        ),
    )


def test_candidate_context_is_rehydrated_after_state_serialization():
    """验证不对外序列化的候选字段仍可传入裁决阶段。"""
    task = ReviewTask(
        id="A.java#h0", file="A.java", patch="+return value;", changed_lines=[2]
    )
    candidate = CandidateIssue(
        id="seed-context",
        task_id=task.id,
        source_agent="behavior",
        file=task.file,
        line=2,
        type="return-contract",
        claim="返回结果的状态传播可能改变",
        mechanism="局部状态更新后返回表达式改变",
        impact="调用方可观察的状态属性可能改变",
        impact_locale="返回值",
        claim_type="behavior",
    )
    serialized_shell = CandidateIssue.model_validate(candidate.model_dump())
    assembly = _assemble_state_dossiers(
        {
            "candidate_issues": [serialized_shell],
            "review_tasks": [task],
            "task_symbol_contexts": {},
            "controlled_candidate_contexts": {
                candidate.id: {
                    "mechanism": candidate.mechanism,
                    "impact": candidate.impact,
                    "impact_locale": candidate.impact_locale,
                    "claim_type": candidate.claim_type,
                }
            },
        }
    )
    assert len(assembly.dossiers) == 1
    assert assembly.dossiers[0].candidate.mechanism == candidate.mechanism
    assert assembly.dossiers[0].candidate.impact == candidate.impact
    assert assembly.dossiers[0].candidate.impact_locale == candidate.impact_locale
    assert assembly.dossiers[0].candidate.claim_type == candidate.claim_type


def test_controlled_candidate_reducer_collapses_same_lifecycle_seam_across_reviewers():
    common = {
        "task_id": "A.java#h0",
        "file": "A.java",
        "line": 4,
        "type": "controlled_review_finding",
        "confidence": 0.5,
    }
    behavior = CandidateIssue(
        id="behavior-1",
        source_agent="behavior",
        claim="register context 在 doOpenInterceptors listener 之前可见性改变",
        **common,
    )
    maintainability = CandidateIssue(
        id="maint-1",
        source_agent="maintainability",
        claim="register context 从 doOpenInterceptable 主干移到 running 检查之后",
        **common,
    )
    reduced, collapsed = collapse_candidate_duplicates([maintainability, behavior])
    assert collapsed == 1
    assert [item.id for item in reduced] == ["behavior-1"]


def test_controlled_candidate_reducer_collapses_cross_type_same_changed_expression():
    common = {"task_id": "A.java#h0", "file": "A.java", "line": 4, "confidence": 0.5}
    first = CandidateIssue(
        id="threat-1",
        source_agent="threat_model",
        type="state_cleanup_effective_change",
        claim="return context 改为 return doOpenInternal，removeAttribute 后的状态可能被新对象丢弃",
        **common,
    )
    second = CandidateIssue(
        id="behavior-1",
        source_agent="behavior",
        type="controlled_review_finding",
        claim="return context 改为 return doOpenInternal，可能丢失已有状态",
        **common,
    )
    reduced, collapsed = collapse_candidate_duplicates([first, second])
    assert collapsed == 1
    assert [item.id for item in reduced] == ["behavior-1"]
