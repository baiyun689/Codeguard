"""证据链过程指标的确定性测试(ADR-046 过渡签名:facts/relations 输入)。"""

from __future__ import annotations

import json

from codeguard_agent.models.council import (
    CandidateFact,
    CandidateIssue,
    CouncilTrace,
    FactRelation,
    Verdict,
)
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import ReviewTask
from codeguard_agent.pipeline.council.metrics import compute_council_run_stats
from codeguard_agent.pipeline.evidence.planner import CandidateDossier, DossierAssembly


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
    return CandidateDossier(candidate, task, None)


def _fact_and_relation(
    dossier: CandidateDossier,
    *,
    relation: str,
    strength: str = "contextual",
    replay_status: str = "recipe",
) -> tuple[CandidateFact, FactRelation]:
    fact = CandidateFact(
        fact_id=f"fact-{dossier.candidate.id}",
        source="diff",
        raw="+service.update();",
        replay_status=replay_status,
    )
    rel = FactRelation(
        fact_id=fact.fact_id,
        relation=relation,
        strength=strength,
        observation="事实" if relation != "insufficient" else "",
        limitation="" if relation != "insufficient" else "没有足够上下文",
    )
    return fact, rel


def _stats(
    dossiers: list[CandidateDossier],
    *,
    final_ids: list[str],
    relations: dict[str, list[FactRelation]] | None = None,
    facts: dict[str, list[CandidateFact]] | None = None,
    traces: list[CouncilTrace] | None = None,
):
    candidates = [dossier.candidate for dossier in dossiers]
    return compute_council_run_stats(
        candidates=candidates,
        assembly=DossierAssembly(tuple(dossiers), (), ()),
        verdicts=[Verdict(item.id, "keep", "test") for item in candidates],
        final_candidate_ids=final_ids,
        facts_by_candidate=facts or {},
        relations_by_candidate=relations or {},
        truncated_candidates=0,
        council_trace=traces or [],
    )


def _with_relation(
    dossier: CandidateDossier,
    *,
    relation: str,
    strength: str = "contextual",
) -> tuple[CandidateDossier, CandidateFact, FactRelation]:
    fact, rel = _fact_and_relation(
        dossier, relation=relation, strength=strength
    )
    return dossier, fact, rel


def test_direct_counter_retained_rate_uses_candidate_survivor_mapping():
    dropped, fact, rel = _with_relation(
        _dossier("dropped"),
        relation="contradicts",
        strength="direct",
    )

    stats = _stats(
        [dropped],
        final_ids=[],
        relations={"dropped": [rel]},
        facts={"dropped": [fact]},
    )

    assert stats.direct_counter_candidate_count == 1
    assert stats.direct_counter_retained_count == 0
    assert stats.direct_counter_retained_rate == 0.0


def test_all_insufficient_retained_rate_counts_only_nonempty_relations():
    retained, fact, rel = _with_relation(
        _dossier("retained"),
        relation="insufficient",
    )
    no_relations = _dossier("no-relations")

    stats = _stats(
        [retained, no_relations],
        final_ids=["retained", "no-relations"],
        relations={"retained": [rel]},
        facts={"retained": [fact]},
    )

    assert stats.all_insufficient_candidate_count == 1
    assert stats.all_insufficient_retained_count == 1
    assert stats.all_insufficient_retained_rate == 1.0


def test_final_issue_fact_coverage_uses_surviving_candidates():
    with_fact, fact_a, rel_a = _with_relation(
        _dossier("with-fact"),
        relation="supports",
        strength="direct",
    )
    insufficient, fact_b, rel_b = _with_relation(
        _dossier("insufficient"),
        relation="insufficient",
    )
    no_relations = _dossier("no-relations")

    stats = _stats(
        [with_fact, insufficient, no_relations],
        final_ids=["with-fact", "insufficient", "no-relations"],
        relations={
            "with-fact": [rel_a],
            "insufficient": [rel_b],
        },
        facts={
            "with-fact": [fact_a],
            "insufficient": [fact_b],
        },
    )

    assert stats.final_issue_count == 3
    # fact covered = 幸存候选存在非 insufficient 关系
    assert stats.final_issue_fact_covered_count == 1
    assert stats.final_issue_fact_coverage == 1 / 3


def test_actual_tool_calls_come_from_evidence_nodes_trace_not_global_context():
    dossiers = [_dossier("one"), _dossier("two")]
    traces = [
        CouncilTrace(node="context_provider", event="context_tool_called"),
        CouncilTrace(node="evidence_verifier", event="evidence_tool_called"),
        CouncilTrace(node="evidence_verifier", event="evidence_tool_reused"),
    ]

    stats = _stats(dossiers, final_ids=["one", "two"], traces=traces)

    assert stats.actual_evidence_tool_calls == 1
    assert stats.average_evidence_tool_calls == 0.5


def test_zero_denominators_are_none_except_average_tool_calls():
    stats = _stats([], final_ids=[])

    assert stats.direct_counter_retained_rate is None
    assert stats.all_insufficient_retained_rate is None
    assert stats.final_issue_fact_coverage is None
    assert stats.average_evidence_tool_calls == 0.0


def test_replay_stats_are_derived_from_facts():
    dossiers = [_dossier("verified"), _dossier("failed")]
    facts = {
        "verified": [
            CandidateFact(
                fact_id="f1", source="diff", raw="+x", replay_status="verified"
            ),
            CandidateFact(
                fact_id="f2", source="diff", raw="+y", replay_status="unverified"
            ),
        ],
        "failed": [
            CandidateFact(
                fact_id="f3", source="diff", raw="", replay_status="failed",
                limitation="tool_error",
            )
        ],
    }

    stats = _stats(dossiers, final_ids=[], facts=facts)

    assert stats.fact_count == 3
    assert stats.replay_verified_count == 1
    assert stats.replay_unverified_count == 1
    assert stats.replay_failed_count == 1


def test_chain_used_and_recipe_fallback_are_derived_from_path_trace_events():
    dossiers = [_dossier("chain"), _dossier("recipe"), _dossier("no-event")]
    traces = [
        CouncilTrace(
            node="evidence_verifier",
            event="candidate_evidence_path",
            detail=json.dumps({"candidate_id": "chain", "path": "chain"}),
        ),
        CouncilTrace(
            node="evidence_verifier",
            event="candidate_evidence_path",
            detail=json.dumps({"candidate_id": "recipe", "path": "recipe"}),
        ),
        CouncilTrace(node="council_judge", event="judge_done", detail=""),
    ]

    stats = _stats(dossiers, final_ids=[], traces=traces)

    # 按 verifier 的路径决策事件统计,与事实状态无关;其他事件不干扰
    assert stats.chain_used_count == 1
    assert stats.recipe_fallback_count == 1


def test_gate_and_severity_metrics_are_derived_from_verdicts():
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

    stats = compute_council_run_stats(
        candidates=[item.candidate for item in candidates],
        assembly=DossierAssembly(tuple(candidates), (), ()),
        verdicts=verdicts,
        final_candidate_ids=["defaulted", "normal-default", "critical"],
        facts_by_candidate={},
        relations_by_candidate={},
        truncated_candidates=0,
        council_trace=[],
    )

    assert stats.no_support_candidate_count == 1
    assert stats.no_support_retained_count == 0
    assert stats.judge_synthesis_failed_count == 1
    assert stats.critical_candidate_count == 1
    assert stats.severity_transitions == {
        "WARNING->WARNING": 2,
        "WARNING->CRITICAL": 1,
    }
