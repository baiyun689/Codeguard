"""Purpose-aware CouncilJudge matrix tests."""

from __future__ import annotations

import importlib
import json

import pytest

from codeguard_agent.models.council import (
    CandidateIssue,
    EvidenceFinding,
    EvidenceNote,
    EvidenceRequest,
    JudgeDecision,
    JudgeDecisions,
)
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import ReviewTask
from codeguard_agent.pipeline.evidence_planner import (
    CandidateBindingFailure,
    CandidateDossier,
    DossierAssembly,
    assemble_dossiers,
    plan_evidence,
)


def _request(candidate_id: str, purpose: str, suffix: str) -> EvidenceRequest:
    return EvidenceRequest(
        candidate_id=candidate_id,
        strategy_id=f"test.{purpose}.{suffix}",
        purpose=purpose,
        target="src/Service.java",
        question=f"question {purpose} {suffix}",
    )


def _finding(
    relation: str,
    strength: str = "contextual",
    *,
    evidence_id: str = "evidence-1",
) -> EvidenceFinding:
    return EvidenceFinding(
        evidence_id=evidence_id,
        source="task_patch",
        observation="guard exists" if relation != "insufficient" else "",
        relation=relation,
        strength=strength,
        limitation="not enough context" if relation == "insufficient" else "",
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
    requests = []
    notes = []
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
        latest_verdict=None,
    )


def _judge(
    dossiers=(),
    *,
    failures=(),
    llm=None,
    evidence_round=1,
    max_evidence_rounds=2,
):
    module = importlib.import_module("codeguard_agent.pipeline.council_judge")
    return module.judge_candidates(
        DossierAssembly(tuple(dossiers), tuple(failures), ()),
        judge_llm=llm,
        structured_method="function_calling",
        evidence_round=evidence_round,
        max_evidence_rounds=max_evidence_rounds,
        max_retries=1,
    )


def test_direct_counter_contradiction_always_drops_candidate():
    dossier = _dossier(
        request_findings=[("counter", _finding("contradicts", "direct"))]
    )

    batch = _judge([dossier])

    assert batch.verdicts[0].action == "drop"
    assert batch.verdicts[0].reason_code == "direct_counter_evidence"
    assert batch.final_issues == []
    assert batch.final_candidate_ids == []


def test_direct_severity_contradiction_downgrades_without_dropping():
    dossier = _dossier(
        severity=Severity.CRITICAL,
        request_findings=[("severity", _finding("contradicts", "direct"))],
    )

    batch = _judge([dossier])

    assert batch.verdicts[0].action == "downgrade"
    assert batch.final_issues[0].severity is Severity.WARNING
    assert dossier.candidate.severity_proposal is Severity.CRITICAL


class _JudgeLLM:
    def __init__(self, decision: JudgeDecision | None) -> None:
        self.decision = decision
        self.messages = []

    def with_structured_output(self, schema, method):
        self.schema = schema
        self.method = method
        return self

    def invoke(self, messages):
        self.messages.append(messages)
        return JudgeDecisions(decisions=[self.decision] if self.decision else [])


def test_direct_support_does_not_fast_keep_when_llm_is_available():
    dossier = _dossier(
        request_findings=[
            ("support", _finding("supports", "direct", evidence_id="support")),
            ("counter", _finding("insufficient", evidence_id="counter")),
        ]
    )
    llm = _JudgeLLM(JudgeDecision(candidate_id="C001", action="drop", reason="false positive"))

    batch = _judge([dossier], llm=llm)

    assert llm.messages
    assert batch.verdicts[0].action == "drop"
    assert batch.verdicts[0].reason_code == "llm_judge"


def test_all_insufficient_without_llm_downgrades_only_critical():
    critical = _dossier(
        "critical",
        severity=Severity.CRITICAL,
        request_findings=[("counter", _finding("insufficient"))],
    )
    warning = _dossier(
        "warning",
        severity=Severity.WARNING,
        request_findings=[("counter", _finding("insufficient"))],
        file="src/Other.java",
    )

    batch = _judge([critical, warning])

    by_id = {verdict.candidate_id: verdict for verdict in batch.verdicts}
    assert by_id["critical"].action == "downgrade"
    assert by_id["warning"].action == "keep"


def test_orphan_note_is_ignored_and_traced_instead_of_treated_as_counter():
    dossier = _dossier()
    orphan = EvidenceNote(
        request_id="orphan",
        candidate_id=dossier.candidate.id,
        findings=[_finding("contradicts", "direct")],
    )
    dossier = CandidateDossier(
        candidate=dossier.candidate,
        task=dossier.task,
        risk_profile=None,
        context_bundle=None,
        requests=(),
        notes=(orphan,),
        latest_verdict=None,
    )

    batch = _judge([dossier])

    assert batch.verdicts[0].action == "keep"
    assert any(event == "orphan_evidence_ignored" for event, _ in batch.trace)


def test_last_round_needs_more_is_normalized_and_nonfinal_preserves_purpose():
    critical = _dossier("critical", severity=Severity.CRITICAL)
    final_llm = _JudgeLLM(
        JudgeDecision(
            candidate_id="C001",
            action="needs_more_evidence",
            requested_purpose="counter",
        )
    )
    more_llm = _JudgeLLM(
        JudgeDecision(
            candidate_id="C001",
            action="needs_more_evidence",
            requested_purpose="severity",
        )
    )

    final_batch = _judge(
        [critical], llm=final_llm, evidence_round=2, max_evidence_rounds=2
    )
    more_batch = _judge(
        [critical], llm=more_llm, evidence_round=1, max_evidence_rounds=2
    )

    assert final_batch.verdicts[0].action == "downgrade"
    assert final_batch.verdicts[0].severity_override is Severity.WARNING
    assert more_batch.verdicts[0].action == "needs_more_evidence"
    assert more_batch.verdicts[0].requested_purpose == "severity"
    detail = next(
        json.loads(detail)
        for event, detail in more_batch.trace
        if event == "judge_requested_more_evidence"
    )
    assert detail["requested_purpose"] == "severity"


def test_invalid_binding_is_dropped_before_llm():
    dossier = _dossier()
    failure = CandidateBindingFailure(dossier.candidate, "file_mismatch")
    llm = _JudgeLLM(JudgeDecision(candidate_id="C001", action="keep"))

    batch = _judge([], failures=[failure], llm=llm)

    assert llm.messages == []
    assert batch.verdicts[0].action == "drop"
    assert batch.verdicts[0].reason_code == "invalid_candidate_binding"


def test_dedup_never_uses_stale_file_from_another_candidate():
    first = _dossier("first", file="src/A.java", line=10, issue_type="same", claim="same root")
    duplicate = _dossier("duplicate", file="src/A.java", line=10, issue_type="same", claim="same root")
    other = _dossier("other", file="src/B.java", line=10, issue_type="same", claim="same root")

    batch = _judge([first, duplicate, other])

    assert {issue.file for issue in batch.final_issues} == {"src/A.java", "src/B.java"}


def test_judge_trace_contains_candidate_verdict_fields():
    dossier = _dossier()
    batch = _judge([dossier])

    detail = next(
        json.loads(value)
        for event, value in batch.trace
        if event == "judge_verdict"
    )
    assert set(detail) == {
        "candidate_id",
        "task_id",
        "action",
        "reason_code",
        "reason",
        "requested_purpose",
        "severity_override",
    }


def test_judge_prompt_is_candidate_dossier_and_last_round_hides_needs_more():
    dossier = _dossier(
        request_findings=[("counter", _finding("insufficient"))]
    )
    llm = _JudgeLLM(JudgeDecision(candidate_id="C001", action="keep"))

    _judge(
        [dossier],
        llm=llm,
        evidence_round=2,
        max_evidence_rounds=2,
    )

    rendered = "\n".join(content for call in llm.messages for _, content in call)
    assert dossier.candidate.claim in rendered
    assert dossier.task.patch in rendered
    assert dossier.requests[0].strategy_id in rendered
    assert dossier.requests[0].purpose in rendered
    assert dossier.requests[0].question in rendered
    assert "evidence_id" in rendered
    assert "limitation" in rendered
    assert "needs_more_evidence" not in rendered
    assert "suggested_tools" not in rendered


def test_unknown_candidate_alias_is_ignored_and_falls_back_conservatively():
    dossier = _dossier()
    llm = _JudgeLLM(JudgeDecision(candidate_id=dossier.candidate.id, action="drop"))

    batch = _judge([dossier], llm=llm)

    assert batch.verdicts[0].action == "keep"
    assert batch.verdicts[0].reason_code == "conservative_keep"


def test_severity_direct_contradiction_restricts_llm_and_steps_warning_to_info():
    critical = _dossier(
        "critical",
        severity=Severity.CRITICAL,
        request_findings=[("severity", _finding("contradicts", "direct"))],
    )
    warning = _dossier(
        "warning",
        severity=Severity.WARNING,
        request_findings=[("severity", _finding("contradicts", "direct"))],
        file="src/Other.java",
    )
    invalid_llm = _JudgeLLM(JudgeDecision(candidate_id="C001", action="drop"))

    critical_batch = _judge([critical], llm=invalid_llm)
    warning_batch = _judge([warning])

    assert critical_batch.verdicts[0].action == "downgrade"
    assert critical_batch.verdicts[0].severity_override is Severity.WARNING
    assert warning_batch.verdicts[0].action == "downgrade"
    assert warning_batch.verdicts[0].severity_override is Severity.INFO


class _JudgeAndMergeLLM:
    def __init__(self) -> None:
        self.schema = None

    def with_structured_output(self, schema, method):
        clone = _JudgeAndMergeLLM()
        clone.schema = schema
        return clone

    def invoke(self, messages):
        if self.schema is JudgeDecisions:
            return JudgeDecisions(
                decisions=[JudgeDecision(candidate_id="C001", action="keep")]
            )
        return self.schema.model_validate({"groups": [{"members": [1, 2]}]})


def test_global_semantic_aggregation_runs_after_candidate_verdicts():
    first = _dossier(
        "first",
        file="src/Service.java",
        line=10,
        issue_type="missing-check",
        claim="update path lacks tenant guard",
    )
    second = _dossier(
        "second",
        file="src/Service.java",
        line=40,
        issue_type="authorization",
        claim="tenant ownership is not checked in update",
    )

    batch = _judge([first, second], llm=_JudgeAndMergeLLM())

    assert len(batch.final_issues) == 1
    assert batch.final_candidate_ids == ["first"]
    assert any(verdict.reason_code == "aggregation_merge" for verdict in batch.verdicts)


class _SequenceJudgeAndMergeLLM:
    def __init__(self) -> None:
        self.schema = None
        self.judge_calls = 0

    def with_structured_output(self, schema, method):
        self.schema = schema
        return self

    def invoke(self, messages):
        if self.schema is JudgeDecisions:
            self.judge_calls += 1
            if self.judge_calls == 1:
                return JudgeDecisions(
                    decisions=[JudgeDecision(candidate_id="C001", action="keep")]
                )
            return JudgeDecisions(
                decisions=[
                    JudgeDecision(
                        candidate_id="C001",
                        action="needs_more_evidence",
                        requested_purpose="counter",
                    )
                ]
            )
        return self.schema.model_validate({"groups": [{"members": [1, 2]}]})


def test_needs_more_round_skips_all_aggregation_and_planner_sees_latest_verdict():
    first = _dossier("first")
    second = _dossier("second")
    llm = _SequenceJudgeAndMergeLLM()

    batch = _judge([first, second], llm=llm, evidence_round=1, max_evidence_rounds=2)

    assert any(
        verdict.candidate_id == "second"
        and verdict.action == "needs_more_evidence"
        for verdict in batch.verdicts
    )
    assert not any(verdict.action == "merge" for verdict in batch.verdicts)
    assert batch.final_issues == []

    assembly = assemble_dossiers(
        [first.candidate, second.candidate],
        [first.task],
        {},
        {},
        [],
        [],
        batch.verdicts,
    )
    latest = {dossier.candidate.id: dossier.latest_verdict for dossier in assembly.dossiers}
    assert latest["second"] is not None
    assert latest["second"].action == "needs_more_evidence"
    plan = plan_evidence(
        assembly.dossiers,
        evidence_round=1,
        classifier_llm=None,
        structured_method="function_calling",
    )
    assert [request.candidate_id for request in plan.requests] == ["second"]


@pytest.mark.parametrize(
    ("current", "adjusted", "expected_action", "expected_severity"),
    [
        (Severity.CRITICAL, Severity.CRITICAL, "downgrade", Severity.WARNING),
        (Severity.WARNING, Severity.CRITICAL, "downgrade", Severity.INFO),
        (Severity.INFO, Severity.WARNING, "keep", None),
    ],
)
def test_llm_downgrade_rejects_same_or_higher_adjusted_severity(
    current,
    adjusted,
    expected_action,
    expected_severity,
):
    dossier = _dossier(severity=current)
    llm = _JudgeLLM(
        JudgeDecision(
            candidate_id="C001",
            action="downgrade",
            adjusted_severity=adjusted,
        )
    )

    batch = _judge([dossier], llm=llm)

    assert batch.verdicts[0].action == expected_action
    assert batch.verdicts[0].severity_override is expected_severity
    assert batch.final_issues[0].severity is (expected_severity or current)


def test_single_candidate_llm_merge_is_rejected_without_dropping_candidate():
    dossier = _dossier()
    llm = _JudgeLLM(
        JudgeDecision(
            candidate_id="C001",
            action="merge",
            merge_target_id="C999",
        )
    )

    batch = _judge([dossier], llm=llm)

    assert batch.verdicts[0].action == "keep"
    assert len(batch.final_issues) == 1


def test_rule_dedup_maps_basename_fingerprint_without_readding_candidate():
    nested = _dossier(
        "nested",
        file="src/a/User.java",
        line=10,
        issue_type="same",
        claim="same root",
    )
    basename = _dossier(
        "basename",
        file="User.java",
        line=10,
        issue_type="same",
        claim="same root",
    )

    batch = _judge([nested, basename])

    assert len(batch.final_issues) == 1
    assert any(verdict.action == "merge" for verdict in batch.verdicts)
