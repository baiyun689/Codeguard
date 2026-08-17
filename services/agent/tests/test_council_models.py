"""ReviewCouncil 内部状态协议测试。"""

from __future__ import annotations

from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.evidence import EvidenceRef
from codeguard_agent.models.schemas import EvidenceRole, Severity


def test_candidate_requires_task_id():
    candidate = CandidateIssue(
        id="c1",
        task_id="A.java#h0",
        source_agent="threat_model",
        file="A.java",
        line=1,
        type="t",
        severity_proposal=Severity.WARNING,
        claim="m",
        confidence=0.9,
    )
    assert candidate.task_id == "A.java#h0"


def test_candidate_contains_only_the_candidate_claim():
    candidate = CandidateIssue(
        id="c1",
        task_id="src/UserService.java#h0",
        source_agent="threat_model",
        file="src/UserService.java",
        line=0,
        type="missing-auth-check",
        severity_proposal=Severity.WARNING,
        claim="缺少权限校验",
        confidence=0.7,
        evidence_refs=[
            EvidenceRef(
                artifact_id="ev-patch", declared_role=EvidenceRole.MECHANISM,
                auto_bound=True,
            )
        ],
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
        "evidence_refs",
        "evidence_ref_errors",
    }
