"""ReviewCouncil 内部状态协议测试。"""

from __future__ import annotations

from codeguard_agent.models.council import CandidateIssue, EvidenceRequest
from codeguard_agent.models.schemas import Issue, Severity


def test_candidate_from_issue_records_source_agent_and_category_mapping():
    issue = Issue(
        severity=Severity.WARNING,
        file="src/UserService.java",
        line=0,
        type="missing-auth-check",
        message="缺少权限校验",
        confidence=0.7,
    )

    candidate = CandidateIssue.from_issue(
        issue,
        source_agent="threat_model",
        category="security",
        index=1,
    )

    assert candidate.source_agent == "threat_model"
    assert candidate.category == "security"
    assert candidate.evidence_status == "partial"
    assert candidate.needs_evidence is True
    assert "find_sensitive_apis" in candidate.evidence_requests[0].preferred_tools
    assert "get_file_content" in candidate.evidence_requests[0].preferred_tools
    assert candidate.evidence_requests[0].question
    assert candidate.evidence_requests[0].reason


def test_evidence_request_carries_preferred_tools_and_open_semantics():
    """EvidenceRequest 不再有 kind 字段，改用 preferred_tools 表达证据需求。"""
    request = EvidenceRequest(
        candidate_id="c1",
        target="NewPaymentFlowConfig",
        question="确认该 feature flag 在生产环境是否默认开启",
        reason="如果生产默认开启，行为变更影响范围更大",
        preferred_tools=["get_file_content", "find_callers"],
    )

    dumped = request.model_dump()
    assert "kind" not in dumped
    assert dumped["question"] == "确认该 feature flag 在生产环境是否默认开启"
    assert dumped["reason"] == "如果生产默认开启，行为变更影响范围更大"
    assert dumped["preferred_tools"] == ["get_file_content", "find_callers"]


def test_from_issue_dispatches_preferred_tools_by_source_agent():
    """各 source_agent 产出正确的默认 preferred_tools。"""
    issue = Issue(
        severity=Severity.WARNING,
        file="A.java",
        line=0,
        type="t",
        message="m",
        confidence=0.5,
    )

    threat = CandidateIssue.from_issue(issue, source_agent="threat_model", index=1)
    assert threat.evidence_requests[0].preferred_tools == ["find_sensitive_apis", "get_file_content"]

    behavior = CandidateIssue.from_issue(issue, source_agent="behavior", index=1)
    assert behavior.evidence_requests[0].preferred_tools == ["find_callers", "get_file_content"]

    maintain = CandidateIssue.from_issue(issue, source_agent="maintainability", index=1)
    assert maintain.evidence_requests[0].preferred_tools == ["get_code_metrics", "get_file_content"]


def test_from_issue_high_confidence_no_evidence_needed():
    """高置信度 + 有行号的候选不需要证据补充。"""
    issue = Issue(
        severity=Severity.WARNING,
        file="A.java",
        line=42,
        type="t",
        message="m",
        confidence=0.95,
    )
    candidate = CandidateIssue.from_issue(issue, source_agent="threat_model", index=1)
    assert candidate.needs_evidence is False
    assert candidate.evidence_requests == []
    assert candidate.evidence_status == "sufficient"
