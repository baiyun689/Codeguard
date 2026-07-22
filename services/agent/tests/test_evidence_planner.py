"""EvidencePlanner 的纯函数契约测试 — one-pass complete planning."""

from __future__ import annotations

import inspect
import json
import threading
import time
from dataclasses import FrozenInstanceError

import pytest

from codeguard_agent.pipeline import evidence_planner
from codeguard_agent.models.council import (
    CandidateIssue,
    EvidenceNote,
    EvidenceRequest,
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
    candidate_task_id: str | None = None,
    candidate_file: str | None = None,
    candidate_id: str | None = None,
) -> CandidateDossier:
    task_id = f"task-{index}"
    file = f"src/Service{index}.java"
    cid = candidate_id or f"candidate-{index}"
    task = ReviewTask(
        id=task_id,
        file=file,
        patch="+ authorize(request);",
        changed_lines=[index + 1],
    )
    candidate = CandidateIssue(
        id=cid,
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


def _resolve_as(monkeypatch: pytest.MonkeyPatch, tag: RiskTag) -> None:
    _force_resolution(monkeypatch, _resolution(tag))


def _trace_details(plan, event: str) -> list[dict[str, object]]:
    return [json.loads(detail) for name, detail in plan.trace if name == event]


# -- mandatory support + severity + all registered counters --


def test_initial_plan_always_contains_support_and_severity(monkeypatch):
    _resolve_as(monkeypatch, RiskTag.GENERAL_REVIEW)
    dossier = _dossier(1)
    plan = plan_evidence(
        [dossier], classifier_llm=None, structured_method="function_calling"
    )
    assert [request.purpose for request in plan.requests] == [
        "counter", "support", "severity"
    ]


def test_authorization_plan_contains_local_and_upstream_counter(monkeypatch):
    _resolve_as(monkeypatch, RiskTag.AUTHORIZATION)
    dossier = _dossier(2)
    plan = plan_evidence(
        [dossier], classifier_llm=None, structured_method="function_calling"
    )
    assert [request.strategy_id for request in plan.requests] == [
        "authorization.counter",
        "authorization.counter_upstream",
        "authorization.support",
        "authorization.severity",
    ]


def test_plan_interface_has_no_evidence_round():
    assert "evidence_round" not in inspect.signature(plan_evidence).parameters


# -- candidate order preserved --


def test_candidate_tag_resolution_runs_concurrently_and_keeps_plan_order(monkeypatch):
    lock = threading.Lock()
    active = 0
    peak_active = 0

    def resolve(dossier, classifier_llm, *, structured_method):
        nonlocal active, peak_active
        with lock:
            active += 1
            peak_active = max(peak_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return _resolution(reason=dossier.candidate.id)

    monkeypatch.setattr(
        "codeguard_agent.pipeline.evidence_planner.resolve_candidate_evidence_tag",
        resolve,
    )
    dossiers = [_dossier(30), _dossier(31), _dossier(32)]

    plan = plan_evidence(
        dossiers,
        classifier_llm=object(),
        structured_method="function_calling",
    )

    assert peak_active > 1
    # 3 dossiers × 4 requests (AUTHORIZATION: counter + counter_upstream + support + severity)
    # Requests are grouped: all counters per dossier, then support per dossier, then severity per dossier
    purposes = [r.purpose for r in plan.requests]
    # dossier 30: counter, counter_upstream; 31: counter, counter_upstream; 32: counter, counter_upstream
    assert purposes[:6] == ["counter", "counter", "counter", "counter", "counter", "counter"]
    # then support: 30, 31, 32
    assert purposes[6:9] == ["support", "support", "support"]
    # then severity: 30, 31, 32
    assert purposes[9:] == ["severity", "severity", "severity"]
    assert len(plan.requests) == 12
    expected_ids = [d.candidate.id for d in dossiers]
    assert [
        detail["candidate_id"]
        for detail in _trace_details(plan, "candidate_evidence_tag_resolved")
    ] == expected_ids


# -- no global cap --


def test_initial_plan_has_no_global_twenty_request_cap(monkeypatch):
    _resolve_as(monkeypatch, RiskTag.GENERAL_REVIEW)
    dossiers = [_dossier(index) for index in range(30)]

    plan = plan_evidence(
        dossiers,
        classifier_llm=None,
        structured_method="function_calling",
    )

    # 30 dossiers × 3 requests (counter + support + severity for GENERAL_REVIEW)
    assert len(plan.requests) == 90
    purposes = [r.purpose for r in plan.requests]
    assert purposes[:30] == ["counter"] * 30
    assert purposes[30:60] == ["support"] * 30
    assert purposes[60:] == ["severity"] * 30


# -- strategy id stability --


def test_request_fields_come_from_strategy_and_id_is_stable(monkeypatch):
    _force_resolution(monkeypatch)
    dossier = _dossier(1)
    strategy = STRATEGIES_BY_ID["authorization.counter"]

    first = plan_evidence(
        [dossier],
        classifier_llm=None,
        structured_method="function_calling",
    ).requests[0]
    second = plan_evidence(
        [dossier],
        classifier_llm=None,
        structured_method="function_calling",
    ).requests[0]

    assert first.target == dossier.task.file
    assert first.question == strategy.question_template
    assert first.preferred_tools == ["get_file_content", "find_sensitive_apis"]
    assert first.strategy_id == strategy.id
    assert first.purpose == strategy.purpose
    assert first.id == second.id


# -- queued strategy exclusion --


def test_initial_plan_does_not_repeat_queued_strategy(monkeypatch):
    _resolve_as(monkeypatch, RiskTag.GENERAL_REVIEW)
    queued = EvidenceRequest(
        candidate_id="candidate-4",
        strategy_id="general_review.counter",
        purpose="counter",
        target="src/Service4.java",
        question="queued",
    )
    dossier = _dossier(4, requests=(queued,))

    plan = plan_evidence(
        [dossier],
        classifier_llm=None,
        structured_method="function_calling",
    )

    # counter already queued → silently skipped; support + severity added
    assert [r.strategy_id for r in plan.requests] == [
        "general_review.support",
        "general_review.severity",
    ]


def test_initial_plan_queued_counter_skips_all_counters_adds_support_severity(
    monkeypatch,
):
    _resolve_as(monkeypatch, RiskTag.AUTHORIZATION)
    queued_counter = EvidenceRequest(
        candidate_id="candidate-17",
        strategy_id="authorization.counter",
        purpose="counter",
        target="src/Service17.java",
        question="queued base counter",
    )
    dossier = _dossier(17, requests=(queued_counter,))

    plan = plan_evidence(
        [dossier],
        classifier_llm=None,
        structured_method="function_calling",
    )

    # authorization.counter excluded; authorization.counter_upstream still available
    assert [r.strategy_id for r in plan.requests] == [
        "authorization.counter_upstream",
        "authorization.support",
        "authorization.severity",
    ]


# -- explicit per-candidate cap --


def test_only_initial_round_exposes_an_explicit_per_candidate_cap():
    assert evidence_planner.MAX_INITIAL_REQUESTS_PER_CANDIDATE == 4
    assert not hasattr(
        evidence_planner,
        "MAX_FOLLOWUP_REQUESTS_PER_CANDIDATE",
    )
    assert "MAX_FOLLOWUP_REQUESTS_PER_CANDIDATE" not in evidence_planner.__all__


# -- invalid binding --


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


# -- classification trace --


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


# -- trace fields --


def test_planned_trace_contains_required_request_fields(monkeypatch):
    _resolve_as(monkeypatch, RiskTag.GENERAL_REVIEW)
    dossier = _dossier(15)

    plan = plan_evidence(
        [dossier],
        classifier_llm=None,
        structured_method="function_calling",
    )

    trace = _trace_details(plan, "evidence_planned")
    purposes = [t["purpose"] for t in trace]
    assert purposes == ["counter", "support", "severity"]
    for t in trace:
        assert "candidate_id" in t
        assert "strategy_id" in t
        assert "target" in t
        assert "task_id" in t
        assert "reason" in t


# -- frozen dossier --


def test_candidate_dossier_is_frozen():
    dossier = _dossier(16)

    with pytest.raises(FrozenInstanceError):
        dossier.risk_profile = None  # type: ignore[misc]


# -- assemble_dossiers --


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
    bundle = TaskContextBundle(task_id=first.task.id)

    assembly = evidence_planner.assemble_dossiers(
        [second.candidate, first.candidate],
        [first.task, second.task],
        {first.task.id: first.risk_profile},
        {first.task.id: bundle},
        [request],
        [note],
    )

    assert [d.candidate.id for d in assembly.dossiers] == [
        second.candidate.id,
        first.candidate.id,
    ]
    bound = assembly.dossiers[1]
    assert bound.requests == (request,)
    assert bound.notes == (note,)
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
    )

    assert assembly.dossiers == ()
    assert [(failure.candidate.id, failure.reason) for failure in assembly.failures] == [
        ("missing", "missing_task"),
        ("mismatch", "file_mismatch"),
    ]
    details = [json.loads(detail) for _, detail in assembly.trace]
    assert [detail["reason"] for detail in details] == ["missing_task", "file_mismatch"]
