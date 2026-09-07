"""最终 Issue 的用户可读证据投影与评测证据门槛测试。"""

from __future__ import annotations

import json

from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.evidence import (
    ArtifactAvailability,
    CandidateVerification,
    EvidenceArtifact,
    EvidenceCaptureMode,
    EvidenceRef,
    EvidenceSourceKind,
    EvidenceValidationStatus,
    VerifiedEvidence,
)
from codeguard_agent.models.schemas import EvidenceLocation, EvidenceRole, Issue, ReviewResult, Severity
from codeguard_agent.models.tasks import ResolvedSymbol, SymbolResolutionStatus, TaskSymbolContext
from codeguard_agent.pipeline.evidence.presentation import enrich_candidate_for_issue
from codeguard_agent.report import render_review_report
from evals.matcher import _build_outcome
from evals.schema import EvalCase, ExpectedIssue


REVISION = "base:head"


def _candidate() -> CandidateIssue:
    return CandidateIssue(
        id="candidate-1",
        task_id="task-1",
        source_agent="behavior",
        file="src/Entry.java",
        line=10,
        type="状态传播",
        claim="清理后返回了新的状态对象 [证据编号 T01]",
        evidence_observation="缓存状态没有继续传递 [证据编号 C01]",
        suggestion="恢复原来的返回路径 [证据编号 T01]",
        evidence_refs=[EvidenceRef(artifact_id="artifact", declared_role=EvidenceRole.MECHANISM)],
    )


def _context() -> TaskSymbolContext:
    return TaskSymbolContext(
        task_id="task-1",
        status=SymbolResolutionStatus.RESOLVED,
        symbols=(
            ResolvedSymbol(
                file="src/Entry.java",
                symbol_id="java:demo.Entry#run()",
                kind="METHOD",
                start_line=1,
                end_line=20,
                signature="Entry#run()",
                source_set="MAIN",
            ),
        ),
    )


def _artifact(tool: str, payload: str, arguments: dict[str, str]) -> EvidenceArtifact:
    return EvidenceArtifact.build(
        task_id="task-1",
        reviewer="behavior",
        revision=REVISION,
        source_kind=EvidenceSourceKind.TOOL_CALL,
        tool=tool,
        arguments=arguments,
        payload=payload,
        availability=ArtifactAvailability.AVAILABLE,
        capture_mode=EvidenceCaptureMode.EXECUTED,
    )


def test_最终_issue_来源由已验证源码元数据生成且不泄漏内部编号():
    artifact = _artifact(
        "get_file_content",
        "symbol_id: java:demo.Store#clear()\nfile: src/Store.java\nlines: 30-42\n\nstore.clear();",
        {"symbol_id": "java:demo.Store#clear()"},
    )
    verification = CandidateVerification(
        candidate_id="candidate-1",
        grounding_status="grounded",
        eligible_for_judge=True,
        valid_evidence=[
            VerifiedEvidence(
                artifact_id=artifact.id,
                source_kind=artifact.source_kind,
                tool=artifact.tool,
                arguments=artifact.arguments,
                content=artifact.payload,
                validation_status=EvidenceValidationStatus.VALID,
            )
        ],
    )
    enriched = enrich_candidate_for_issue(
        _candidate(), symbol_context=_context(), verification=verification, artifacts={artifact.id: artifact}
    )
    issue = enriched.to_issue(Severity.WARNING)
    assert any(item.file == "src/Store.java" and item.start_line == 30 for item in issue.evidence_locations)
    assert "src/Store.java" in issue.root_cause
    assert "T01" not in issue.message + issue.root_cause + issue.suggestion
    assert "C01" not in issue.message + issue.root_cause + issue.suggestion


def test_无验证证据时不生成已验证根因但保留变更位置():
    enriched = enrich_candidate_for_issue(_candidate(), symbol_context=_context())
    assert enriched.root_cause == ""
    assert any(
        item.kind == "changed_code" and item.file == "src/Entry.java"
        for item in enriched.evidence_locations
    )


def test_图谱证据生成跨文件来源位置和关系():
    payload = json.dumps(
        {
            "subject_symbol_id": "java:demo.Entry#run()",
            "symbols": [
                {"id": "java:demo.Entry#run()", "file": "src/Entry.java", "startLine": 1, "endLine": 20},
                {"id": "java:demo.Listener#open()", "file": "src/Listener.java", "startLine": 4, "endLine": 12},
            ],
            "relationships": [
                {"sourceId": "java:demo.Entry#run()", "targetId": "java:demo.Listener#open()", "kind": "CALLS"}
            ],
        },
        ensure_ascii=False,
    )
    artifact = _artifact("inspect_path", payload, {"symbol_id": "java:demo.Entry#run()"})
    verification = CandidateVerification(
        candidate_id="candidate-1",
        grounding_status="grounded",
        eligible_for_judge=True,
        valid_evidence=[
            VerifiedEvidence(
                artifact_id=artifact.id,
                source_kind=artifact.source_kind,
                tool=artifact.tool,
                arguments=artifact.arguments,
                content=artifact.payload,
                validation_status=EvidenceValidationStatus.VALID,
            )
        ],
    )
    enriched = enrich_candidate_for_issue(
        _candidate(), symbol_context=_context(), verification=verification, artifacts={artifact.id: artifact}
    )
    related = [item for item in enriched.evidence_locations if item.kind == "related_path"]
    assert related and related[0].file == "src/Listener.java"
    assert "→" in related[0].relation


def test_最终报告展示根因和来源但不展示账本编号():
    issue = Issue(
        severity=Severity.WARNING,
        file="src/Entry.java",
        line=10,
        type="状态传播",
        message="清理后的状态没有继续传递",
        root_cause="返回路径改动导致状态丢失",
        evidence_locations=[
            EvidenceLocation(
                file="src/Store.java",
                symbol="Store#clear()",
                start_line=30,
                end_line=42,
                kind="root_cause",
            )
        ],
        confidence=1.0,
    )
    text = render_review_report(
        ReviewResult(issues=[issue]),
        repo="demo",
        base="HEAD",
        model="test",
        duration_s=1,
        diff_text="",
    )
    assert "**根因**:返回路径改动导致状态丢失" in text
    assert "src/Store.java:30-42 · `Store#clear()`" in text
    assert "T01" not in text and "C01" not in text


def test_评测证据门槛把无来源的语义猜测记为_fn和_fp():
    expected = ExpectedIssue(
        type_keywords=["状态传播"],
        file="src/Entry.java",
        line=10,
        evidence_anchors=["Store.java:30"],
    )
    case = EvalCase(id="evidence", category="test", diff="diff", evidence_required=True, expected=[expected])
    guessed = Issue(
        severity=Severity.WARNING,
        file="src/Entry.java",
        line=10,
        type="状态传播",
        message="缓存状态没有继续传递",
        confidence=1.0,
    )
    outcome = _build_outcome(case, [guessed], {0: 0}, "rule")
    assert (outcome.true_positives, outcome.false_negatives, outcome.false_positives) == (0, 1, 1)
    assert outcome.evidence_missing_hits == 1


def test_评测证据门槛接受用户可读来源位置():
    expected = ExpectedIssue(
        type_keywords=["状态传播"],
        file="src/Entry.java",
        line=10,
        evidence_anchors=["Store.java:30"],
    )
    case = EvalCase(id="evidence", category="test", diff="diff", evidence_required=True, expected=[expected])
    grounded = Issue(
        severity=Severity.WARNING,
        file="src/Entry.java",
        line=10,
        type="状态传播",
        message="缓存状态没有继续传递",
        root_cause="来源于 Store.java:30 的 clear()",
        evidence_locations=[
            EvidenceLocation(file="src/Store.java", symbol="Store#clear()", start_line=30, end_line=42)
        ],
        confidence=1.0,
    )
    outcome = _build_outcome(case, [grounded], {0: 0}, "rule")
    assert (outcome.true_positives, outcome.false_negatives) == (1, 0)
    assert (outcome.evidence_checked, outcome.evidence_backed_hits) == (1, 1)


def test_根因文本猜中锚点但结构化来源不匹配仍失败():
    expected = ExpectedIssue(
        type_keywords=["状态传播"],
        file="src/Entry.java",
        line=10,
        evidence_anchors=["Store.java:30"],
    )
    case = EvalCase(id="evidence-text", category="test", diff="diff", evidence_required=True, expected=[expected])
    guessed = Issue(
        severity=Severity.WARNING,
        file="src/Entry.java",
        line=10,
        type="状态传播",
        message="状态传播",
        root_cause="已确认来源 Store.java:30",
        evidence_locations=[
            EvidenceLocation(file="src/Other.java", symbol="Other#run()", start_line=30, end_line=30)
        ],
    )
    assert _build_outcome(case, [guessed], {0: 0}, "rule").true_positives == 0


def test_跨文件证据不能只用变更位置():
    expected = ExpectedIssue(
        type_keywords=["状态传播"],
        file="src/Entry.java",
        line=10,
        evidence_anchors=["Store.java"],
        evidence_scope="cross_file",
    )
    case = EvalCase(id="cross", category="test", diff="diff", evidence_required=True, expected=[expected])
    changed_only = Issue(
        severity=Severity.WARNING,
        file="src/Entry.java",
        line=10,
        type="状态传播",
        message="状态传播",
        evidence_locations=[
            EvidenceLocation(file="src/Entry.java", start_line=10, end_line=10, kind="changed_code")
        ],
        confidence=1.0,
    )
    outcome = _build_outcome(case, [changed_only], {0: 0}, "rule")
    assert outcome.true_positives == 0


def test_跨文件根因文本猜中目标但来源位置无关仍失败():
    expected = ExpectedIssue(
        type_keywords=["状态传播"],
        file="src/Entry.java",
        line=10,
        evidence_anchors=["Store.java"],
        evidence_scope="cross_file",
    )
    case = EvalCase(id="cross-text", category="test", diff="diff", evidence_required=True, expected=[expected])
    guessed = Issue(
        severity=Severity.WARNING,
        file="src/Entry.java",
        line=10,
        type="状态传播",
        message="状态传播",
        root_cause="调用 Store.java 的 clear() 导致状态丢失",
        evidence_locations=[
            EvidenceLocation(file="src/Other.java", symbol="Other#run()", start_line=30, end_line=30, kind="related_path")
        ],
    )
    assert _build_outcome(case, [guessed], {0: 0}, "rule").true_positives == 0


def test_跨文件证据接受另一个文件的已验证来源():
    expected = ExpectedIssue(
        type_keywords=["状态传播"],
        file="src/Entry.java",
        line=10,
        evidence_anchors=["Store.java", "Store#clear"],
        evidence_scope="cross_file",
    )
    case = EvalCase(id="cross", category="test", diff="diff", evidence_required=True, expected=[expected])
    grounded = Issue(
        severity=Severity.WARNING,
        file="src/Entry.java",
        line=10,
        type="状态传播",
        message="状态传播",
        evidence_locations=[
            EvidenceLocation(file="src/Entry.java", start_line=10, end_line=10, kind="changed_code"),
            EvidenceLocation(file="src/Store.java", symbol="Store#clear()", start_line=30, end_line=42, kind="related_path"),
        ],
        confidence=1.0,
    )
    outcome = _build_outcome(case, [grounded], {0: 0}, "rule")
    assert outcome.true_positives == 1
