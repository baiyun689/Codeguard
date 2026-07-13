"""EvidencePlanner 的纯函数契约测试。"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from codeguard_agent.pipeline import evidence_planner
from codeguard_agent.models.council import (
    CandidateIssue,
    EvidenceNote,
    EvidenceRequest,
    Verdict,
)
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import (
    ReviewTask,
    RiskProfile,
    RiskTag,
    TaskContextBundle,
)
from codeguard_agent.pipeline.evidence_planner import (
    CandidateDossier,
    plan_evidence,
)
from codeguard_agent.pipeline.evidence_rules import (
    STRATEGIES_BY_ID,
    CandidateTagResolution,
)


def _dossier(
    index: int,
    *,
    severity: Severity = Severity.WARNING,
    confidence: float = 0.95,
    tag_scores: dict[RiskTag, int] | None = None,
    requests: tuple[EvidenceRequest, ...] = (),
    notes: tuple[EvidenceNote, ...] = (),
    verdict: Verdict | None = None,
    candidate_task_id: str | None = None,
    candidate_file: str | None = None,
) -> CandidateDossier:
    task_id = f"task-{index}"
    file = f"src/Service{index}.java"
    task = ReviewTask(
        id=task_id,
        file=file,
        patch="+ authorize(request);",
        changed_lines=[index + 1],
    )
    candidate = CandidateIssue(
        id=f"candidate-{index}",
        task_id=candidate_task_id or task_id,
        source_agent="threat_model",
        file=candidate_file or file,
        line=index + 1,
        type="authorization",
        severity_proposal=severity,
        claim="missing authorization check",
        confidence=confidence,
    )
    profile = None
    if tag_scores is not None:
        profile = RiskProfile(task_id=task_id, tag_scores=tag_scores)
    return CandidateDossier(
        candidate=candidate,
        task=task,
        risk_profile=profile,
        context_bundle=None,
        requests=requests,
        notes=notes,
        latest_verdict=verdict,
    )


def _resolution(
    tag: RiskTag = RiskTag.AUTHORIZATION,
    *,
    confidence: float = 0.95,
    source: str = "rule",
    reason: str = "test resolution",
) -> CandidateTagResolution:
    return CandidateTagResolution(
        tag=tag,
        confidence=confidence,
        source=source,
        reason=reason,
    )


def _force_resolution(monkeypatch: pytest.MonkeyPatch, resolution=None) -> None:
    chosen = resolution or _resolution()
    monkeypatch.setattr(
        "codeguard_agent.pipeline.evidence_planner.resolve_candidate_evidence_tag",
        lambda dossier, classifier_llm, *, structured_method: (
            chosen(dossier) if callable(chosen) else chosen
        ),
    )


def _trace_details(plan, event: str) -> list[dict[str, object]]:
    return [json.loads(detail) for name, detail in plan.trace if name == event]


def test_initial_plan_orders_all_counters_before_gated_supports(monkeypatch):
    _force_resolution(monkeypatch)
    dossiers = [
        _dossier(0, severity=Severity.CRITICAL),
        _dossier(1, tag_scores={RiskTag.AUTHORIZATION: 2}),
        _dossier(2, confidence=0.89),
        _dossier(3, tag_scores={RiskTag.AUTHORIZATION: 1}),
    ]

    plan = plan_evidence(
        dossiers,
        evidence_round=0,
        classifier_llm=object(),
        structured_method="json_schema",
    )

    assert [request.purpose for request in plan.requests] == [
        "counter",
        "counter",
        "counter",
        "counter",
        "support",
        "support",
        "support",
    ]
    assert [request.candidate_id for request in plan.requests[:4]] == [
        dossier.candidate.id for dossier in dossiers
    ]
    assert {request.candidate_id for request in plan.requests[4:]} == {
        "candidate-0",
        "candidate-1",
        "candidate-2",
    }


def test_initial_plan_has_no_global_twenty_request_cap(monkeypatch):
    _force_resolution(monkeypatch)
    dossiers = [_dossier(index) for index in range(30)]

    plan = plan_evidence(
        dossiers,
        evidence_round=0,
        classifier_llm=None,
        structured_method="function_calling",
    )

    assert len(plan.requests) == 30
    assert all(request.purpose == "counter" for request in plan.requests)
    assert [request.candidate_id for request in plan.requests] == [
        dossier.candidate.id for dossier in dossiers
    ]


def test_request_fields_come_from_strategy_and_id_is_stable(monkeypatch):
    _force_resolution(monkeypatch)
    dossier = _dossier(1)
    strategy = STRATEGIES_BY_ID["authorization.counter"]

    first = plan_evidence(
        [dossier],
        evidence_round=0,
        classifier_llm=None,
        structured_method="function_calling",
    ).requests[0]
    second = plan_evidence(
        [dossier],
        evidence_round=0,
        classifier_llm=None,
        structured_method="function_calling",
    ).requests[0]

    assert first.target == dossier.task.file
    assert first.question == strategy.question_template
    assert first.preferred_tools == ["get_file_content", "find_sensitive_apis"]
    assert first.strategy_id == strategy.id
    assert first.purpose == strategy.purpose
    assert first.id == second.id


def test_initial_plan_does_not_repeat_queued_strategy(monkeypatch):
    _force_resolution(monkeypatch, _resolution(RiskTag.GENERAL_REVIEW))
    queued = EvidenceRequest(
        candidate_id="candidate-4",
        strategy_id="general_review.counter",
        purpose="counter",
        target="src/Service4.java",
        question="queued",
    )
    dossier = _dossier(4, confidence=0.5, requests=(queued,))

    plan = plan_evidence(
        [dossier],
        evidence_round=0,
        classifier_llm=None,
        structured_method="function_calling",
    )

    assert [request.strategy_id for request in plan.requests] == [
        "general_review.support"
    ]
    skipped = _trace_details(plan, "evidence_plan_skipped")
    assert skipped == [
        {
            "candidate_id": "candidate-4",
            "purpose": "counter",
            "reason": "no_available_strategy",
            "tag": "GENERAL_REVIEW",
        }
    ]


def test_initial_plan_does_not_replace_queued_base_counter_with_upstream(
    monkeypatch,
):
    _force_resolution(monkeypatch, _resolution(RiskTag.AUTHORIZATION))
    queued = EvidenceRequest(
        candidate_id="candidate-17",
        strategy_id="authorization.counter",
        purpose="counter",
        target="src/Service17.java",
        question="queued base counter",
    )
    dossier = _dossier(17, requests=(queued,))

    plan = plan_evidence(
        [dossier],
        evidence_round=0,
        classifier_llm=None,
        structured_method="function_calling",
    )

    assert plan.requests == []
    assert _trace_details(plan, "evidence_plan_skipped") == [
        {
            "candidate_id": "candidate-17",
            "purpose": "counter",
            "reason": "no_available_strategy",
            "tag": "AUTHORIZATION",
        }
    ]


def test_only_initial_round_exposes_an_explicit_per_candidate_cap():
    assert evidence_planner.MAX_INITIAL_REQUESTS_PER_CANDIDATE == 2
    assert not hasattr(
        evidence_planner,
        "MAX_FOLLOWUP_REQUESTS_PER_CANDIDATE",
    )
    assert "MAX_FOLLOWUP_REQUESTS_PER_CANDIDATE" not in evidence_planner.__all__


@pytest.mark.parametrize(
    ("dossier", "expected_task_id"),
    [
        (_dossier(5, candidate_task_id="wrong-task"), "task-5"),
        (_dossier(6, candidate_file="src/Other.java"), "task-6"),
    ],
)
def test_invalid_candidate_binding_is_skipped(dossier, expected_task_id):
    plan = plan_evidence(
        [dossier],
        evidence_round=0,
        classifier_llm=None,
        structured_method="function_calling",
    )

    assert plan.requests == []
    assert _trace_details(plan, "evidence_plan_skipped") == [
        {
            "candidate_id": dossier.candidate.id,
            "reason": "invalid_candidate_binding",
            "task_id": expected_task_id,
        }
    ]


def test_classification_trace_has_stable_required_fields_and_prior_match(monkeypatch):
    def resolve(dossier):
        tag = (
            RiskTag.AUTHORIZATION
            if dossier.candidate.id == "candidate-7"
            else RiskTag.INJECTION
        )
        return _resolution(tag, confidence=0.85, reason=f"resolved {tag.value}")

    _force_resolution(monkeypatch, resolve)
    dossiers = [
        _dossier(
            7,
            tag_scores={RiskTag.AUTHORIZATION: 2, RiskTag.INJECTION: 0},
        ),
        _dossier(8, tag_scores={RiskTag.AUTHORIZATION: 1}),
    ]

    plan = plan_evidence(
        dossiers,
        evidence_round=0,
        classifier_llm=None,
        structured_method="function_calling",
    )

    details = _trace_details(plan, "candidate_evidence_tag_resolved")
    assert details == [
        {
            "candidate_id": "candidate-7",
            "confidence": 0.85,
            "matches_task_prior": True,
            "reason": "resolved AUTHORIZATION",
            "source": "rule",
            "tag": "AUTHORIZATION",
            "task_id": "task-7",
            "task_tags": ["AUTHORIZATION"],
        },
        {
            "candidate_id": "candidate-8",
            "confidence": 0.85,
            "matches_task_prior": False,
            "reason": "resolved INJECTION",
            "source": "rule",
            "tag": "INJECTION",
            "task_id": "task-8",
            "task_tags": ["AUTHORIZATION"],
        },
    ]


def test_followup_only_handles_needs_more_and_reports_invalid_or_exhausted(monkeypatch):
    _force_resolution(monkeypatch, _resolution(RiskTag.GENERAL_REVIEW))
    existing = EvidenceRequest(
        candidate_id="candidate-9",
        strategy_id="general_review.support",
        purpose="support",
        target="src/Service9.java",
        question="already queued",
    )
    orphan = EvidenceNote(
        request_id="orphan",
        candidate_id="candidate-9",
        findings=[
            {
                "evidence_id": "orphan-evidence",
                "source": "test",
                "observation": "",
                "relation": "insufficient",
                "strength": "contextual",
                "limitation": "orphan",
            }
        ],
    )
    dossiers = [
        _dossier(
            9,
            requests=(existing,),
            notes=(orphan,),
            verdict=Verdict(
                candidate_id="candidate-9",
                action="needs_more_evidence",
                reason_code="need-support",
                requested_purpose="support",
            ),
        ),
        _dossier(
            10,
            verdict=Verdict(
                candidate_id="candidate-10",
                action="needs_more_evidence",
                reason_code="missing-purpose",
            ),
        ),
        _dossier(
            11,
            verdict=Verdict(
                candidate_id="candidate-11",
                action="keep",
                reason_code="done",
                requested_purpose="counter",
            ),
        ),
    ]

    plan = plan_evidence(
        dossiers,
        evidence_round=2,
        classifier_llm=None,
        structured_method="function_calling",
    )

    assert plan.requests == []
    assert _trace_details(plan, "evidence_plan_invalid_verdict") == [
        {
            "candidate_id": "candidate-10",
            "evidence_round": 2,
            "reason": "requested_purpose_missing",
            "task_id": "task-10",
        }
    ]
    assert _trace_details(plan, "evidence_plan_exhausted") == [
        {
            "candidate_id": "candidate-9",
            "evidence_round": 2,
            "purpose": "support",
            "reason": "no_remaining_strategy",
            "tag": "GENERAL_REVIEW",
        }
    ]
    assert {
        detail["candidate_id"]
        for detail in _trace_details(plan, "candidate_evidence_tag_resolved")
    } == {"candidate-9"}


def test_followup_selects_next_counter_strategy_and_severity_is_reachable(monkeypatch):
    _force_resolution(monkeypatch)
    queued_counter = EvidenceRequest(
        candidate_id="candidate-12",
        strategy_id="authorization.counter",
        purpose="counter",
        target="src/Service12.java",
        question="already queued",
    )
    dossiers = [
        _dossier(
            12,
            requests=(queued_counter,),
            verdict=Verdict(
                candidate_id="candidate-12",
                action="needs_more_evidence",
                reason_code="try-next-counter",
                requested_purpose="counter",
            ),
        ),
        _dossier(
            13,
            verdict=Verdict(
                candidate_id="candidate-13",
                action="needs_more_evidence",
                reason_code="calibrate-severity",
                requested_purpose="severity",
            ),
        ),
    ]

    plan = plan_evidence(
        dossiers,
        evidence_round=1,
        classifier_llm=None,
        structured_method="function_calling",
    )

    assert [request.strategy_id for request in plan.requests] == [
        "authorization.counter_upstream",
        "authorization.severity",
    ]
    assert [request.purpose for request in plan.requests] == ["counter", "severity"]


def test_followup_forwards_classifier_llm_and_structured_method(monkeypatch):
    classifier_llm = object()
    calls = []

    def resolve(dossier, received_llm, *, structured_method):
        calls.append((dossier.candidate.id, received_llm, structured_method))
        return _resolution()

    monkeypatch.setattr(
        "codeguard_agent.pipeline.evidence_planner.resolve_candidate_evidence_tag",
        resolve,
    )
    dossier = _dossier(
        14,
        verdict=Verdict(
            candidate_id="candidate-14",
            action="needs_more_evidence",
            reason_code="follow-up",
            requested_purpose="counter",
        ),
    )

    plan_evidence(
        [dossier],
        evidence_round=1,
        classifier_llm=classifier_llm,
        structured_method="json_schema",
    )

    assert calls == [("candidate-14", classifier_llm, "json_schema")]


def test_planned_trace_contains_required_request_fields(monkeypatch):
    _force_resolution(monkeypatch)
    dossier = _dossier(15)

    plan = plan_evidence(
        [dossier],
        evidence_round=0,
        classifier_llm=None,
        structured_method="function_calling",
    )

    assert _trace_details(plan, "evidence_planned") == [
        {
            "candidate_id": "candidate-15",
            "evidence_round": 0,
            "preferred_tools": ["get_file_content", "find_sensitive_apis"],
            "purpose": "counter",
            "reason": "initial_counter",
            "strategy_id": "authorization.counter",
            "target": "src/Service15.java",
            "task_id": "task-15",
        }
    ]


def test_candidate_dossier_is_frozen():
    dossier = _dossier(16)

    with pytest.raises(FrozenInstanceError):
        dossier.latest_verdict = None


def test_assemble_dossiers_preserves_candidate_order_and_groups_state():
    first = _dossier(21)
    second = _dossier(22)
    request = EvidenceRequest(
        candidate_id=first.candidate.id,
        strategy_id="authorization.counter",
        purpose="counter",
        target=first.task.file,
        question=STRATEGIES_BY_ID["authorization.counter"].question_template,
        preferred_tools=["get_file_content", "find_sensitive_apis"],
    )
    note = EvidenceNote(
        request_id=request.id,
        candidate_id=first.candidate.id,
        findings=[
            {
                "evidence_id": "evidence-1",
                "source": "task_patch",
                "observation": "",
                "relation": "insufficient",
                "strength": "contextual",
                "limitation": "not_analyzed",
            }
        ],
    )
    older = Verdict(first.candidate.id, "keep", "older")
    latest = Verdict(first.candidate.id, "needs_more_evidence", "latest", requested_purpose="support")
    bundle = TaskContextBundle(task_id=first.task.id)

    assembly = evidence_planner.assemble_dossiers(
        [second.candidate, first.candidate],
        [first.task, second.task],
        {first.task.id: first.risk_profile},
        {first.task.id: bundle},
        [request],
        [note],
        [older, latest],
    )

    assert [d.candidate.id for d in assembly.dossiers] == [
        second.candidate.id,
        first.candidate.id,
    ]
    bound = assembly.dossiers[1]
    assert bound.requests == (request,)
    assert bound.notes == (note,)
    assert bound.latest_verdict is latest
    assert bound.context_bundle is bundle


def test_assemble_dossiers_reports_missing_task_and_file_mismatch():
    valid = _dossier(23)
    missing = valid.candidate.model_copy(update={"id": "missing", "task_id": "absent"})
    mismatch = valid.candidate.model_copy(update={"id": "mismatch", "file": "src/Other.java"})

    assembly = evidence_planner.assemble_dossiers(
        [missing, mismatch],
        [valid.task],
        {},
        {},
        [],
        [],
        [],
    )

    assert assembly.dossiers == ()
    assert [(failure.candidate.id, failure.reason) for failure in assembly.failures] == [
        ("missing", "missing_task"),
        ("mismatch", "file_mismatch"),
    ]
    details = [json.loads(detail) for _, detail in assembly.trace]
    assert [detail["reason"] for detail in details] == ["missing_task", "file_mismatch"]
