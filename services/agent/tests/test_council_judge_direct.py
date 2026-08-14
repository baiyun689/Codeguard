"""DirectJudge(无证据链消融档)终审测试。"""

from __future__ import annotations

import json

from codeguard_agent.models.council import (
    CandidateDirectAssessment,
    CandidateIssue,
)
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import ReviewTask
from codeguard_agent.pipeline.council.judge import (
    direct_judge_candidates,
)
from codeguard_agent.pipeline.evidence.planner import (
    CandidateDossier,
    DossierAssembly,
    assemble_dossiers,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _dossier(
    candidate_id: str = "candidate-1",
    *,
    severity: Severity = Severity.WARNING,
    file: str = "src/Service.java",
    line: int = 10,
    issue_type: str = "logic",
    claim: str = "wrong condition",
) -> CandidateDossier:
    task = ReviewTask(
        id=f"{file}#h0",
        file=file,
        hunk_header=f"@@ -{line},1 +{line},1 @@",
        patch="+changed();",
        changed_lines=[line],
    )
    candidate = CandidateIssue(
        id=candidate_id,
        task_id=task.id,
        source_agent="behavior",
        file=file,
        line=line,
        type=issue_type,
        severity_proposal=severity,
        claim=claim,
        confidence=0.9,
    )
    return CandidateDossier(
        candidate=candidate,
        task=task,
        context_bundle=None,
        requests=(),
        notes=(),
    )


def _assembly(*dossiers: CandidateDossier) -> DossierAssembly:
    return DossierAssembly(tuple(dossiers), (), ())


# ── LLM test doubles ─────────────────────────────────────────────────────────


class _AssessmentStructured:
    def __init__(self, owner):
        self.owner = owner

    def invoke(self, messages):
        self.owner.calls += 1
        self.owner.messages = messages
        if isinstance(self.owner.assessment, Exception):
            raise self.owner.assessment
        return self.owner.assessment


class _AssessmentLLM:
    def __init__(self, assessment):
        self.assessment = assessment
        self.calls = 0
        self.messages = []

    def with_structured_output(self, schema, method):
        assert schema is CandidateDirectAssessment
        return _AssessmentStructured(self)


def _assessment(**updates):
    values = {
        "candidate_id": "C001",
        "action": "keep",
        "severity": Severity.WARNING,
        "reason": "patch supports the claim",
    }
    values.update(updates)
    return CandidateDirectAssessment(**values)


# ── tests ────────────────────────────────────────────────────────────────────


def test_mock_llm_keeps_all_with_proposed_severity():
    """llm 为 None(mock 路径):确定性保留,沿用候选提案级别。"""
    batch = direct_judge_candidates(
        _assembly(_dossier("c1", severity=Severity.CRITICAL)),
        judge_llm=None,
        structured_method="function_calling",
        max_retries=1,
    )
    assert len(batch.verdicts) == 1
    verdict = batch.verdicts[0]
    assert verdict.action == "keep"
    assert verdict.reason_code == "direct_assessment_missing"
    assert verdict.resolved_severity == Severity.CRITICAL
    assert len(batch.final_issues) == 1
    assert batch.final_issues[0].severity == Severity.CRITICAL


def test_direct_keep_uses_assessed_severity():
    llm = _AssessmentLLM(
        _assessment(action="keep", severity=Severity.CRITICAL)
    )
    batch = direct_judge_candidates(
        _assembly(_dossier("c1", severity=Severity.WARNING)),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert llm.calls == 1
    verdict = batch.verdicts[0]
    assert verdict.action == "keep"
    assert verdict.reason_code == "direct_judge_keep"
    assert verdict.resolved_severity == Severity.CRITICAL
    assert batch.final_issues[0].severity == Severity.CRITICAL


def test_direct_drop_emits_no_issue():
    llm = _AssessmentLLM(
        _assessment(action="drop", severity=Severity.INFO, reason="claim does not hold")
    )
    batch = direct_judge_candidates(
        _assembly(_dossier("c1")),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    verdict = batch.verdicts[0]
    assert verdict.action == "drop"
    assert verdict.reason_code == "direct_judge_drop"
    assert batch.final_issues == []


def test_unexpected_candidate_id_falls_back_to_keep():
    llm = _AssessmentLLM(
        _assessment(candidate_id="C999", action="drop", severity=Severity.INFO)
    )
    batch = direct_judge_candidates(
        _assembly(_dossier("c1", severity=Severity.WARNING)),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    verdict = batch.verdicts[0]
    assert verdict.action == "keep"
    assert verdict.reason_code == "direct_assessment_missing"
    assert batch.final_issues[0].severity == Severity.WARNING


def test_none_output_falls_back_to_keep():
    llm = _AssessmentLLM(None)
    batch = direct_judge_candidates(
        _assembly(_dossier("c1", severity=Severity.WARNING)),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    verdict = batch.verdicts[0]
    assert verdict.action == "keep"
    assert verdict.reason_code == "direct_assessment_missing"


def test_llm_exception_falls_back_to_keep():
    llm = _AssessmentLLM(RuntimeError("structured output failed"))
    batch = direct_judge_candidates(
        _assembly(_dossier("c1", severity=Severity.WARNING)),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    verdict = batch.verdicts[0]
    assert verdict.action == "keep"
    assert verdict.reason_code == "direct_assessment_missing"


def test_payload_has_no_evidence_fields():
    """DirectJudge 输入不得包含证据请求/发现字段(消融变量纯净)。"""
    llm = _AssessmentLLM(_assessment())
    direct_judge_candidates(
        _assembly(_dossier("c1")),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    _, user_message = llm.messages[-1]
    payload = json.loads(user_message)
    assert "severity_proposal" in payload["candidate"]
    assert payload["task_patch"] == "+changed();"
    assert "evidence" not in payload
    assert "requests" not in payload
    assert "findings" not in payload
    assert payload["candidate_alias"] == "C001"


def test_invalid_binding_drops():
    dossier = _dossier("c1")
    broken_candidate = dossier.candidate.model_copy(update={"task_id": "missing-task"})
    assembly = assemble_dossiers(
        [broken_candidate],
        [dossier.task],
        {},
        [],
        [],
    )
    assert len(assembly.failures) == 1
    batch = direct_judge_candidates(
        assembly,
        judge_llm=_AssessmentLLM(_assessment()),
        structured_method="function_calling",
        max_retries=1,
    )
    assert len(batch.verdicts) == 1
    assert batch.verdicts[0].action == "drop"
    assert batch.verdicts[0].reason_code == "invalid_candidate_binding"
    assert batch.final_issues == []
