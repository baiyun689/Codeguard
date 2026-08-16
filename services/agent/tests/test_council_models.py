"""ReviewCouncil 内部状态协议测试。"""

from __future__ import annotations

from codeguard_agent.models.council import CandidateIssue
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
        "evidence_chain",
    }
