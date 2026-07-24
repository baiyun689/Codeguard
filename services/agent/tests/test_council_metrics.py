"""Phase 5 证据链过程指标的确定性测试。"""

from __future__ import annotations

from codeguard_agent.models.council import (
    CandidateIssue,
    CouncilTrace,
    EvidenceFinding,
    EvidenceNote,
    EvidenceRequest,
    Verdict,
)
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import ReviewTask, RiskTag
from codeguard_agent.pipeline.council.metrics import compute_council_run_stats
from codeguard_agent.pipeline.evidence.planner import CandidateDossier, DossierAssembly
from codeguard_agent.pipeline.evidence.rules import strategies_for


def _dossier(candidate_id: str, *, file: str | None = None) -> CandidateDossier:
    path = file or f"src/{candidate_id}.java"
    task = ReviewTask(
        id=f"{path}#h0",
        file=path,
        patch="+service.update();",
        changed_lines=[10],
    )
    candidate = CandidateIssue(
        id=candidate_id,
        task_id=task.id,
        source_agent="threat_model",
        file=path,
        line=10,
        type="authorization",
        severity_proposal=Severity.WARNING,
        claim="敏感操作缺少授权保护",
    )
    return CandidateDossier(candidate, task, None, None, (), ())


def _with_finding(
    dossier: CandidateDossier,
    *,
    purpose: str,
    relation: str,
    strength: str = "contextual",
) -> CandidateDossier:
    strategy = strategies_for(RiskTag.AUTHORIZATION, purpose)[0]
    calls = strategy.build_tool_calls(dossier)
    request = EvidenceRequest(
        candidate_id=dossier.candidate.id,
        strategy_id=strategy.id,
        purpose=purpose,
        target=dossier.task.file,
        question=strategy.question_template,
        preferred_tools=list(dict.fromkeys(call.tool_name for call in calls)),
    )
    finding = EvidenceFinding(
        evidence_id=f"evidence-{dossier.candidate.id}",
        source="task_patch",
        observation="事实" if relation != "insufficient" else "",
        relation=relation,
        strength=strength,
        limitation="没有足够上下文" if relation == "insufficient" else "",
    )
    note = EvidenceNote(
        request_id=request.id,
        candidate_id=dossier.candidate.id,
        findings=[finding],
    )
    return CandidateDossier(
        dossier.candidate,
        dossier.task,
        dossier.risk_profile,
        dossier.context_bundle,
        (request,),
        (note,),
    )


def _stats(
    dossiers: list[CandidateDossier],
    *,
    final_ids: list[str],
    traces: list[CouncilTrace] | None = None,
):
    candidates = [dossier.candidate for dossier in dossiers]
    return compute_council_run_stats(
        candidates=candidates,
        assembly=DossierAssembly(tuple(dossiers), (), ()),
        verdicts=[Verdict(item.id, "keep", "test") for item in candidates],
        final_candidate_ids=final_ids,
        evidence_request_count=sum(len(dossier.requests) for dossier in dossiers),
        truncated_candidates=0,
        council_trace=traces or [],
    )


def test_direct_counter_retained_rate_uses_candidate_survivor_mapping():
    dropped = _with_finding(
        _dossier("dropped"),
        purpose="counter",
        relation="contradicts",
        strength="direct",
    )

    stats = _stats([dropped], final_ids=[])

    assert stats.direct_counter_candidate_count == 1
    assert stats.direct_counter_retained_count == 0
    assert stats.direct_counter_retained_rate == 0.0


def test_all_insufficient_retained_rate_counts_only_nonempty_associated_findings():
    retained = _with_finding(
        _dossier("retained"),
        purpose="counter",
        relation="insufficient",
    )
    no_findings = _dossier("no-findings")

    stats = _stats([retained, no_findings], final_ids=["retained", "no-findings"])

    assert stats.all_insufficient_candidate_count == 1
    assert stats.all_insufficient_retained_count == 1
    assert stats.all_insufficient_retained_rate == 1.0


def test_final_issue_strategy_and_fact_coverage_use_surviving_candidates():
    with_fact = _with_finding(
        _dossier("with-fact"),
        purpose="support",
        relation="supports",
        strength="direct",
    )
    insufficient = _with_finding(
        _dossier("insufficient"),
        purpose="counter",
        relation="insufficient",
    )
    no_request = _dossier("no-request")

    stats = _stats(
        [with_fact, insufficient, no_request],
        final_ids=["with-fact", "insufficient", "no-request"],
    )

    assert stats.final_issue_count == 3
    assert stats.final_issue_strategy_covered_count == 2
    assert stats.final_issue_strategy_coverage == 2 / 3
    assert stats.final_issue_fact_covered_count == 1
    assert stats.final_issue_fact_coverage == 1 / 3


def test_registry_coverage_is_computed_from_every_risk_tag():
    stats = _stats([], final_ids=[])

    assert stats.registry_risk_tag_total == len(RiskTag)
    assert stats.registry_risk_tag_covered_count == len(RiskTag)
    assert stats.registry_risk_tag_coverage == 1.0


def test_actual_tool_calls_come_from_evidence_agent_trace_not_global_context():
    dossiers = [_dossier("one"), _dossier("two")]
    traces = [
        CouncilTrace(node="context_provider", event="context_tool_called"),
        CouncilTrace(node="evidence_agent", event="evidence_tool_called"),
        CouncilTrace(node="evidence_agent", event="evidence_tool_reused"),
    ]

    stats = _stats(dossiers, final_ids=["one", "two"], traces=traces)

    assert stats.actual_evidence_tool_calls == 1
    assert stats.average_evidence_tool_calls == 0.5


def test_zero_denominators_are_none_except_average_tool_calls():
    stats = _stats([], final_ids=[])

    assert stats.direct_counter_retained_rate is None
    assert stats.all_insufficient_retained_rate is None
    assert stats.final_issue_strategy_coverage is None
    assert stats.final_issue_fact_coverage is None
    assert stats.average_evidence_tool_calls == 0.0


def test_gate_and_severity_metrics_are_derived_from_verdicts_and_trace():
    candidates = [
        _dossier("no-support"),
        _dossier("defaulted"),
        _dossier("normal-default"),
        _dossier("critical"),
    ]
    verdicts = [
        Verdict("no-support", "drop", "no_supporting_evidence"),
        Verdict(
            "defaulted",
            "keep",
            "severity_evidence_incomplete",
            resolved_severity=Severity.WARNING,
        ),
        Verdict(
            "normal-default",
            "keep",
            "severity_resolved",
            resolved_severity=Severity.WARNING,
        ),
        Verdict(
            "critical",
            "keep",
            "severity_resolved",
            resolved_severity=Severity.CRITICAL,
        ),
    ]
    traces = [
        CouncilTrace(
            node="council_judge",
            event="severity_resolved",
            detail=(
                '{"candidate_id":"normal-default","matched_rule":"authorization.default",'
                '"severity":"WARNING","missing_critical_factors":["impact"]}'
            ),
        ),
        CouncilTrace(
            node="council_judge",
            event="severity_resolved",
            detail=(
                '{"candidate_id":"defaulted","matched_rule":"authorization.default",'
                '"severity":"WARNING","missing_critical_factors":["impact"]}'
            ),
        ),
        CouncilTrace(
            node="council_judge",
            event="severity_resolved",
            detail=(
                '{"candidate_id":"critical","matched_rule":"authorization.critical",'
                '"severity":"CRITICAL","missing_critical_factors":[]}'
            ),
        ),
    ]

    stats = compute_council_run_stats(
        candidates=[item.candidate for item in candidates],
        assembly=DossierAssembly(tuple(candidates), (), ()),
        verdicts=verdicts,
        final_candidate_ids=["defaulted", "normal-default", "critical"],
        evidence_request_count=0,
        truncated_candidates=0,
        council_trace=traces,
    )

    assert stats.no_support_candidate_count == 1
    assert stats.no_support_retained_count == 0
    assert stats.severity_defaulted_count == 1
    assert stats.critical_candidate_count == 1
    assert stats.critical_policy_matched_count == 1
    assert stats.critical_missing_factor_count == 2
    assert stats.severity_transitions == {
        "WARNING->WARNING": 2,
        "WARNING->CRITICAL": 1,
    }
