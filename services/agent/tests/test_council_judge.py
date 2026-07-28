"""Evidence-gate + synthesis + severity-policy CouncilJudge tests."""

from __future__ import annotations

import json
from dataclasses import replace

from codeguard_agent.models.council import (
    CandidateEvidenceAssessment,
    CandidateIssue,
    EvidenceFinding,
    EvidenceNote,
    EvidenceRequest,
    SeverityFactorAssessment,
)
from codeguard_agent.models.schemas import Issue, Severity
from codeguard_agent.models.tasks import ReviewTask, RiskTag
from codeguard_agent.pipeline.council.dedup import CandidateGroup
from codeguard_agent.pipeline.evidence.planner import CandidateDossier
from codeguard_agent.pipeline.council.judge import (
    JudgeBatch,
    _emit_supported_issues,
    judge_candidates as _judge_impl,
)
from codeguard_agent.pipeline.evidence.rules import STRATEGIES_BY_ID, strategies_for
from codeguard_agent.pipeline.council.severity import policy_for


# ── helpers ──────────────────────────────────────────────────────────────────


def _finding(
    relation: str = "supports",
    strength: str = "direct",
    *,
    evidence_id: str = "E1",
    source: str = "task_patch",
    observation: str = "observed",
    limitation: str = "",
) -> EvidenceFinding:
    return EvidenceFinding(
        evidence_id=evidence_id,
        source=source,
        observation=observation,
        relation=relation,
        strength=strength,
        limitation=limitation,
    )


def _request(
    candidate_id: str,
    purpose: str,
    index: str = "0",
    strategy_id: str | None = None,
) -> EvidenceRequest:
    sid = strategy_id or f"authorization.{purpose}"
    strategy = STRATEGIES_BY_ID.get(sid)
    question = strategy.question_template if strategy else "test question"
    return EvidenceRequest(
        candidate_id=candidate_id,
        strategy_id=sid,
        purpose=purpose,
        target="src/Service.java",
        question=question,
        preferred_tools=list(strategy.allowed_tools) if strategy else [],
    )


def _dossier(
    candidate_id: str = "candidate-1",
    *,
    severity: Severity = Severity.WARNING,
    request_findings: list[tuple[str, EvidenceFinding]] | None = None,
    file: str = "src/Service.java",
    line: int = 10,
    issue_type: str = "authorization",
    claim: str = "missing guard",
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
        source_agent="threat_model",
        file=file,
        line=line,
        type=issue_type,
        severity_proposal=severity,
        claim=claim,
        confidence=0.99,
    )
    requests: list[EvidenceRequest] = []
    notes: list[EvidenceNote] = []
    for index, (purpose, finding) in enumerate(request_findings or []):
        request = _request(candidate_id, purpose, str(index))
        requests.append(request)
        notes.append(
            EvidenceNote(
                request_id=request.id,
                candidate_id=candidate_id,
                findings=[finding],
            )
        )
    return CandidateDossier(
        candidate=candidate,
        task=task,
        risk_profile=None,
        context_bundle=None,
        requests=tuple(requests),
        notes=tuple(notes),
    )


def _supported_dossier(
    *,
    tag: RiskTag = RiskTag.AUTHORIZATION,
    proposed: Severity = Severity.WARNING,
    factor_ids: tuple[str, ...] = (),
) -> CandidateDossier:
    strategy = strategies_for(tag, "support")[0]
    request = EvidenceRequest(
        candidate_id="candidate-1",
        strategy_id=strategy.id,
        purpose="support",
        target="src/Service.java",
        question=strategy.question_template,
        preferred_tools=list(strategy.allowed_tools),
    )
    findings = [
        _finding("supports", "direct", evidence_id="claim-support"),
        *[
            _finding("supports", "direct", evidence_id=f"factor-{i}")
            for i, _ in enumerate(factor_ids)
        ],
    ]
    note = EvidenceNote(
        request_id=request.id,
        candidate_id="candidate-1",
        findings=findings,
    )
    base = _dossier(severity=proposed)
    return replace(base, requests=(request,), notes=(note,))


# ── LLM test doubles ─────────────────────────────────────────────────────────


class _FailIfCalledLLM:
    def with_structured_output(self, schema, method):
        raise AssertionError("LLM must not run when gate decides")


class _ReturningNoneStructured:
    def invoke(self, messages):
        return None


class _ReturningNoneLLM:
    def with_structured_output(self, schema, method):
        return _ReturningNoneStructured()


class _AssessmentStructured:
    def __init__(self, owner):
        self.owner = owner

    def invoke(self, messages):
        self.owner.calls += 1
        self.owner.messages = messages
        return self.owner.assessment


class _AssessmentLLM:
    def __init__(self, assessment):
        self.assessment = assessment
        self.calls = 0
        self.messages = []

    def with_structured_output(self, schema, method):
        assert schema is CandidateEvidenceAssessment
        return _AssessmentStructured(self)


def _supported_assessment(**updates):
    values = {
        "candidate_id": "C001",
        "claim_status": "supported",
        "counter_effect": "none",
        "severity_factors": [],
        "conflicts": [],
        "reason": "support evidence establishes the candidate",
    }
    values.update(updates)
    return CandidateEvidenceAssessment(**values)


def _injection_critical_assessment():
    factors = policy_for(RiskTag.INJECTION).critical_requires
    return _supported_assessment(
        severity_factors=[
            SeverityFactorAssessment(
                factor_id=fid,
                status="proven",
                evidence_ids=[f"factor-{i}"],
                reason=f"{fid} is directly proven",
            )
            for i, fid in enumerate(factors)
        ]
    )


# ── gate: direct counter → drop ──────────────────────────────────────────────


def test_direct_counter_drops_before_llm_call():
    dossier = _dossier(
        request_findings=[("counter", _finding("contradicts", "direct"))]
    )
    batch = _judge([dossier], llm=_FailIfCalledLLM())
    assert batch.verdicts[0].action == "drop"
    assert batch.verdicts[0].reason_code == "direct_counter_evidence"


# ── gate: all insufficient → drop ────────────────────────────────────────────


def test_all_insufficient_drops_before_llm_call():
    dossier = _dossier(
        request_findings=[("support", _finding("insufficient", strength="contextual", observation="", limitation="no data"))]
    )
    batch = _judge([dossier], llm=_FailIfCalledLLM())
    assert batch.verdicts[0].action == "drop"
    assert batch.verdicts[0].reason_code == "evidence_insufficient"


# ── gate: no support → drop ──────────────────────────────────────────────────


def test_no_support_purpose_finding_drops_candidate():
    dossier = _dossier(
        request_findings=[("severity", _finding("supports", "direct"))]
    )
    batch = _judge([dossier], llm=_FailIfCalledLLM())
    assert batch.verdicts[0].action == "drop"
    assert batch.verdicts[0].reason_code == "no_supporting_evidence"


# ── gate: contextual support enters synthesis ────────────────────────────────


def test_contextual_support_enters_synthesis():
    dossier = _dossier(
        request_findings=[("support", _finding("supports", "contextual"))]
    )
    llm = _AssessmentLLM(_supported_assessment())
    batch = _judge([dossier], llm=llm)
    assert llm.calls == 1
    assert batch.verdicts[0].action == "keep"


def test_cross_candidate_note_is_ignored_before_gate():
    dossier = _dossier(
        request_findings=[("support", _finding("supports", "direct"))]
    )
    wrong_note = dossier.notes[0].model_copy(update={"candidate_id": "candidate-2"})
    batch = _judge(
        [replace(dossier, notes=(wrong_note,))],
        llm=_FailIfCalledLLM(),
    )
    assert batch.verdicts[0].action == "drop"
    assert batch.verdicts[0].reason_code == "evidence_insufficient"


def test_unregistered_strategy_note_is_ignored_before_gate():
    dossier = _dossier(
        request_findings=[("support", _finding("supports", "direct"))]
    )
    invalid_request = dossier.requests[0].model_copy(
        update={"strategy_id": "unknown.support"}
    )
    batch = _judge(
        [replace(dossier, requests=(invalid_request,))],
        llm=_FailIfCalledLLM(),
    )
    assert batch.verdicts[0].action == "drop"
    assert batch.verdicts[0].reason_code == "evidence_insufficient"


def test_synthesis_payload_includes_factor_descriptions():
    dossier = _supported_dossier(tag=RiskTag.INJECTION)
    llm = _AssessmentLLM(_supported_assessment())

    _judge([dossier], llm=llm)

    payload = json.loads(llm.messages[1][1])
    factors = {item["id"]: item["description"] for item in payload["allowed_factors"]}
    assert factors["untrusted_input"] == "攻击者可控输入能够到达受影响代码路径"
    assert "allowed_factor_ids" not in payload


def test_synthesis_payload_excludes_cross_candidate_findings():
    dossier = _supported_dossier(tag=RiskTag.INJECTION)
    poisoned_note = dossier.notes[0].model_copy(
        update={
            "candidate_id": "candidate-2",
            "findings": [_finding(evidence_id="poisoned-cross-candidate")],
        }
    )
    llm = _AssessmentLLM(_supported_assessment())

    _judge([replace(dossier, notes=(*dossier.notes, poisoned_note))], llm=llm)

    payload = json.loads(llm.messages[1][1])
    evidence_ids = {
        finding["evidence_id"]
        for request in payload["requests"]
        for finding in request["findings"]
    }
    assert "claim-support" in evidence_ids
    assert "poisoned-cross-candidate" not in evidence_ids


# ── synthesis: complete counter → drop ───────────────────────────────────────


def test_complete_counter_effect_drops_candidate():
    dossier = _supported_dossier()
    batch = _judge(
        [dossier],
        llm=_AssessmentLLM(_supported_assessment(counter_effect="complete")),
    )
    assert batch.verdicts[0].action == "drop"
    assert batch.verdicts[0].reason_code == "synthesized_counter_evidence"


# ── synthesis: unresolved → drop ─────────────────────────────────────────────


def test_unresolved_conflict_drops_candidate():
    dossier = _supported_dossier()
    batch = _judge(
        [dossier],
        llm=_AssessmentLLM(
            _supported_assessment(
                claim_status="unresolved",
                conflicts=["upstream guard coverage unclear"],
            )
        ),
    )
    assert batch.verdicts[0].action == "drop"
    assert batch.verdicts[0].reason_code == "evidence_conflict_unresolved"


# ── synthesis: refuted → drop ────────────────────────────────────────────────


def test_refuted_claim_drops_candidate():
    dossier = _supported_dossier()
    batch = _judge(
        [dossier],
        llm=_AssessmentLLM(_supported_assessment(claim_status="refuted")),
    )
    assert batch.verdicts[0].action == "drop"
    assert batch.verdicts[0].reason_code == "synthesized_counter_evidence"


# ── LLM failure → policy default ─────────────────────────────────────────────


def test_llm_failure_keeps_gate_passed_candidate_at_policy_default():
    dossier = _supported_dossier(tag=RiskTag.INJECTION, proposed=Severity.CRITICAL)
    batch = _judge([dossier], llm=_ReturningNoneLLM())
    assert batch.verdicts[0].action == "keep"
    assert batch.final_issues[0].severity is Severity.WARNING


# ── severity_proposal 不影响 resolved severity ────────────────────────────────


def test_proposed_severity_never_changes_resolved_severity():
    factors = policy_for(RiskTag.INJECTION).critical_requires
    low = _supported_dossier(tag=RiskTag.INJECTION, proposed=Severity.INFO, factor_ids=factors)
    high = _supported_dossier(tag=RiskTag.INJECTION, proposed=Severity.CRITICAL, factor_ids=factors)
    llm = _AssessmentLLM(_injection_critical_assessment())
    assert _judge([low], llm=llm).final_issues[0].severity is Severity.CRITICAL
    assert _judge([high], llm=llm).final_issues[0].severity is Severity.CRITICAL


# ── critical factor matching ─────────────────────────────────────────────────


def test_all_critical_factors_proven_resolves_critical():
    tag = RiskTag.DESERIALIZATION
    factors = policy_for(tag).critical_requires
    dossier = _supported_dossier(tag=tag, factor_ids=factors)
    llm = _AssessmentLLM(
        _supported_assessment(
            severity_factors=[
                SeverityFactorAssessment(
                    factor_id=fid, status="proven",
                    evidence_ids=[f"factor-{i}"], reason=f"{fid} proven",
                )
                for i, fid in enumerate(factors)
            ]
        )
    )
    assert _judge([dossier], llm=llm).final_issues[0].severity is Severity.CRITICAL


def test_one_missing_critical_factor_defaults_to_warning():
    tag = RiskTag.DESERIALIZATION
    factors = policy_for(tag).critical_requires
    dossier = _supported_dossier(tag=tag, factor_ids=factors[:-1])
    llm = _AssessmentLLM(
        _supported_assessment(
            severity_factors=[
                SeverityFactorAssessment(
                    factor_id=fid, status="proven",
                    evidence_ids=[f"factor-{i}"], reason=f"{fid} proven",
                )
                for i, fid in enumerate(factors[:-1])
            ]
        )
    )
    assert _judge([dossier], llm=llm).final_issues[0].severity is Severity.WARNING


def test_unknown_factor_evidence_citation_is_traced_and_ignored():
    factor_id = policy_for(RiskTag.INJECTION).critical_requires[0]
    dossier = _supported_dossier(tag=RiskTag.INJECTION)
    assessment = _supported_assessment(
        severity_factors=[
            SeverityFactorAssessment(
                factor_id=factor_id,
                status="proven",
                evidence_ids=["unknown-evidence"],
            )
        ]
    )

    batch = _judge([dossier], llm=_AssessmentLLM(assessment))

    assert batch.final_issues[0].severity is Severity.WARNING
    assert any(
        event == "unknown_evidence_citation_ignored"
        and "unknown-evidence" in detail
        for event, detail in batch.trace
    )


def test_unknown_evidence_citation_is_traced_even_when_claim_is_refuted():
    dossier = _supported_dossier(tag=RiskTag.INJECTION)
    assessment = _supported_assessment(
        claim_status="refuted",
        severity_factors=[
            SeverityFactorAssessment(
                factor_id=policy_for(RiskTag.INJECTION).critical_requires[0],
                status="proven",
                evidence_ids=["unknown-before-refutation"],
            )
        ],
    )

    batch = _judge([dossier], llm=_AssessmentLLM(assessment))

    assert batch.verdicts[0].action == "drop"
    assert any(
        event == "unknown_evidence_citation_ignored"
        and "unknown-before-refutation" in detail
        for event, detail in batch.trace
    )


def test_general_review_never_critical():
    dossier = _supported_dossier(tag=RiskTag.GENERAL_REVIEW)
    batch = _judge([dossier], llm=_AssessmentLLM(_supported_assessment()))
    assert batch.final_issues[0].severity is not Severity.CRITICAL


# ── invalid binding → drop ───────────────────────────────────────────────────


def test_invalid_binding_drops():
    from codeguard_agent.pipeline.evidence.planner import (
        CandidateBindingFailure,
        DossierAssembly,
    )
    candidate = CandidateIssue(
        id="orphan", task_id="no-match", source_agent="threat_model",
        file="src/X.java", line=1, type="test",
        severity_proposal=Severity.WARNING, claim="orphan",
    )
    failure = CandidateBindingFailure(candidate, "missing_task")
    assembly = DossierAssembly((), (failure,), ())
    batch = _judge_from_assembly(assembly, llm=_FailIfCalledLLM())
    assert batch.verdicts[0].action == "drop"
    assert batch.verdicts[0].reason_code == "invalid_candidate_binding"


def test_candidate_group_does_not_drop_supported_member_with_refuted_sibling():
    refuted = _dossier(
        "candidate-refuted",
        request_findings=[("counter", _finding("contradicts", "direct"))],
        claim="dead catch is misleading",
    )
    supported = _dossier(
        "candidate-supported",
        request_findings=[("support", _finding("supports", "direct"))],
        claim="runtime exception escapes the event loop",
    )
    group = CandidateGroup(
        id="candidate-group-error-handling",
        members=(refuted.candidate, supported.candidate),
        primary_risk_tag=RiskTag.AUTHORIZATION,
        severity_proposal=Severity.WARNING,
        confidence=0.99,
        shared_root_cause="same root cause",
        shared_behavior="same behavior",
        shared_fix="same fix",
    )

    batch = _judge(
        [refuted, supported],
        llm=_AssessmentLLM(_supported_assessment()),
        candidate_groups=[group],
    )

    assert batch.final_candidate_ids == [supported.candidate.id]
    assert [issue.message for issue in batch.final_issues] == [
        supported.candidate.claim
    ]


def test_candidate_group_combines_all_supported_member_information():
    first = _dossier(
        "candidate-first",
        request_findings=[("support", _finding("supports", "direct"))],
        claim="request without id is accepted",
    )
    second = _dossier(
        "candidate-second",
        request_findings=[("support", _finding("supports", "direct"))],
        issue_type="contract bypass",
        claim="blank id bypasses the contract",
    )
    group = CandidateGroup(
        id="candidate-group-input",
        members=(first.candidate, second.candidate),
        primary_risk_tag=RiskTag.AUTHORIZATION,
        severity_proposal=Severity.WARNING,
        confidence=0.99,
        shared_root_cause="same contract defect",
        shared_behavior="same invalid request behavior",
        shared_fix="same validation fix",
    )

    batch = _judge(
        [first, second],
        llm=_AssessmentLLM(_supported_assessment()),
        candidate_groups=[group],
    )

    assert batch.final_candidate_ids == [first.candidate.id]
    assert len(batch.final_issues) == 1
    assert first.candidate.claim in batch.final_issues[0].message
    assert second.candidate.claim in batch.final_issues[0].message
    assert first.candidate.type in batch.final_issues[0].type
    assert second.candidate.type in batch.final_issues[0].type


def test_candidate_group_splits_when_judged_impacts_have_different_severity():
    first = _dossier("candidate-first", claim="runtime interruption")
    second = _dossier("candidate-second", claim="dead catch")
    group = CandidateGroup(
        id="candidate-group-not-equivalent",
        members=(first.candidate, second.candidate),
        primary_risk_tag=RiskTag.ERROR_HANDLING,
        severity_proposal=Severity.WARNING,
        confidence=0.99,
        shared_root_cause="same upstream change",
        shared_behavior="claimed shared behavior",
        shared_fix="claimed shared fix",
    )
    batch = JudgeBatch()
    _emit_supported_issues(
        batch,
        [
            (
                first.candidate.id,
                Issue(
                    severity=Severity.WARNING,
                    file=first.candidate.file,
                    type=first.candidate.type,
                    message=first.candidate.claim,
                ),
            ),
            (
                second.candidate.id,
                Issue(
                    severity=Severity.INFO,
                    file=second.candidate.file,
                    type=second.candidate.type,
                    message=second.candidate.claim,
                ),
            ),
        ],
        [group],
    )

    assert batch.final_candidate_ids == [
        first.candidate.id,
        second.candidate.id,
    ]
    assert [issue.message for issue in batch.final_issues] == [
        first.candidate.claim,
        second.candidate.claim,
    ]
    assert any(event == "candidate_group_split" for event, _ in batch.trace)


def _judge(dossiers, *, llm=None, candidate_groups=()):
    from codeguard_agent.pipeline.evidence.planner import DossierAssembly
    assembly = DossierAssembly(tuple(dossiers), (), ())
    return _judge_impl(
        assembly,
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
        candidate_groups=candidate_groups,
    )


def _judge_from_assembly(assembly, *, llm=None):
    return _judge_impl(assembly, judge_llm=llm, structured_method="function_calling", max_retries=1)
