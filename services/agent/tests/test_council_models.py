"""ReviewCouncil 内部状态协议测试。"""

from __future__ import annotations

from codeguard_agent.models.council import (
    CandidateIssue,
    EvidenceRequest,
    build_evidence_requests,
)
from codeguard_agent.models.schemas import Issue, Severity


def test_candidate_requires_task_id():
    issue = Issue(
        severity=Severity.WARNING, file="A.java", line=1, type="t",
        message="m", confidence=0.9,
    )
    candidate = CandidateIssue.from_issue(
        issue, source_agent="threat_model", index=1, task_id="A.java#h0"
    )
    assert candidate.task_id == "A.java#h0"


def test_candidate_contains_only_the_candidate_claim():
    issue = Issue(
        severity=Severity.WARNING,
        file="src/UserService.java",
        line=0,
        type="missing-auth-check",
        message="缺少权限校验",
        confidence=0.7,
    )

    candidate = CandidateIssue.from_issue(
        issue, source_agent="threat_model", index=1, task_id="src/UserService.java#h0"
    )

    assert set(candidate.model_dump()) == {
        "id",
        "task_id",
        "source_agent",
        "file",
        "line",
        "type",
        "severity_proposal",
        "claim",
        "suggestion",
        "confidence",
    }


def test_evidence_request_id_is_stable_for_the_same_semantics():
    first = EvidenceRequest(
        candidate_id="c1",
        target="A.java",
        question="确认保护逻辑",
        preferred_tools=["get_file_content"],
    )
    second = EvidenceRequest(
        candidate_id="c1",
        target="A.java",
        question="确认保护逻辑",
        preferred_tools=["get_file_content"],
    )
    different = EvidenceRequest(
        candidate_id="c1",
        target="A.java",
        question="确认调用方",
        preferred_tools=["find_callers"],
    )

    assert first.id == second.id
    assert first.id != different.id


def test_build_evidence_requests_dispatches_tools_by_source_agent():
    issue = Issue(
        severity=Severity.WARNING,
        file="A.java",
        line=0,
        type="t",
        message="m",
        confidence=0.5,
    )
    expected = {
        "threat_model": ["find_sensitive_apis", "get_file_content"],
        "behavior": ["find_callers", "get_file_content"],
        "maintainability": ["get_code_metrics", "get_file_content"],
    }

    for source_agent, tools in expected.items():
        candidate = CandidateIssue.from_issue(
            issue, source_agent=source_agent, index=1, task_id="A.java#h0"
        )
        requests = build_evidence_requests(candidate)
        assert len(requests) == 1
        assert requests[0].preferred_tools == tools


def test_build_evidence_requests_skips_located_high_confidence_candidate():
    issue = Issue(
        severity=Severity.WARNING,
        file="A.java",
        line=42,
        type="t",
        message="m",
        confidence=0.95,
    )
    candidate = CandidateIssue.from_issue(
        issue, source_agent="threat_model", index=1, task_id="A.java#h0"
    )
    assert build_evidence_requests(candidate) == []
