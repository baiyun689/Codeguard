"""EvidenceTraceStep 与 Issue.evidence_chain 契约测试(ADR-046)。"""
import pytest
from pydantic import ValidationError

from codeguard_agent.models.schemas import (
    EvidenceTraceStep,
    Issue,
    Severity,
)


def test_issue_accepts_evidence_chain():
    issue = Issue(
        severity=Severity.WARNING, file="A.java", line=1, type="t", message="m",
        evidence_chain=[
            EvidenceTraceStep(
                tool="get_file_content",
                args={"file_path": "A.java"},
                located="int x = 1;",
            )
        ],
    )
    assert issue.evidence_chain[0].tool == "get_file_content"
    assert issue.evidence_chain[0].args == {"file_path": "A.java"}
    assert issue.evidence_chain[0].located == "int x = 1;"


def test_issue_defaults_to_empty_chain():
    issue = Issue(severity=Severity.INFO, file="A.java", type="t", message="m")
    assert issue.evidence_chain == []


def test_trace_step_rejects_unknown_tool():
    with pytest.raises(ValidationError):
        EvidenceTraceStep(tool="rm_rf", args={}, located="x")


def test_trace_step_defaults():
    step = EvidenceTraceStep(tool="get_file_content", args={})
    assert step.located == ""
    assert step.args == {}
