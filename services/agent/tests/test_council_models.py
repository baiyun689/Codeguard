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
    assert candidate.evidence_requests[0].kind == "related_snippet"
    assert candidate.evidence_requests[0].question
    assert candidate.evidence_requests[0].reason


def test_open_question_evidence_request_carries_open_semantics():
    request = EvidenceRequest(
        candidate_id="c1",
        kind="open_question",
        target="NewPaymentFlowConfig",
        question="确认该 feature flag 在生产环境是否默认开启",
        reason="如果生产默认开启，行为变更影响范围更大",
        preferred_tools=["get_file_content", "find_callers"],
    )

    dumped = request.model_dump()
    assert dumped["kind"] == "open_question"
    assert dumped["question"] == "确认该 feature flag 在生产环境是否默认开启"
    assert dumped["reason"] == "如果生产默认开启，行为变更影响范围更大"
    assert dumped["preferred_tools"] == ["get_file_content", "find_callers"]
