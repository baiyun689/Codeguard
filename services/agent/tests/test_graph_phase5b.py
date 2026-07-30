"""Phase 5B graph wiring and single-writer contracts."""

from __future__ import annotations

import codeguard_agent.pipeline.graph as G
from codeguard_agent.models.council import (
    EvidenceFinding,
    EvidenceNote,
    EvidenceRequest,
)
from codeguard_agent.models.tasks import TaskSelection
from codeguard_agent.pipeline.reviewers.reviewers import DEFAULT_REVIEWERS


def _request(index: int) -> EvidenceRequest:
    return EvidenceRequest(
        candidate_id=f"candidate-{index}",
        strategy_id="general_review.counter",
        purpose="counter",
        target=f"src/Service{index}.java",
        question="检查候选主张的反证",
    )


def test_evidence_request_reducer_only_deduplicates_without_cap():
    requests = [_request(index) for index in range(30)]

    reduced = G.dedup_evidence_request_reducer([], [*requests, requests[0]])

    assert reduced == requests


def test_reviewer_branch_never_writes_evidence_requests():
    node = G.make_reviewer_node(DEFAULT_REVIEWERS[0], llm=None, tool_client=None)

    out = node(
        {
            "review_tasks": [],
            "risk_priors": {},
            "task_selection": TaskSelection(selected_task_ids=[]),
        }
    )

    assert "evidence_requests" not in out


def test_strategist_node_is_the_initial_request_writer(monkeypatch):
    planned = _request(99)
    monkeypatch.setattr(
        G,
        "build_investigation_plans",
        lambda *_args, **_kwargs: type(
            "Batch",
            (),
            {
                "plans": (type("Plan", (), {"actionable": True})(),),
                "fallback_candidate_ids": (),
                "diagnostics": (),
                "llm_call_count": 1,
            },
        )(),
    )
    monkeypatch.setattr(
        G,
        "investigation_plans_to_requests",
        lambda _plans, _concerns: [planned],
    )
    concern = type("Concern", (), {"concern_id": "concern-1"})()
    analysis = type(
        "Analysis",
        (),
        {
            "concerns": (concern,),
        },
    )()

    out = G._evidence_strategist_node(None)(
        {
            "concern_analysis": analysis,
            "evidence_requests": [],
        }
    )

    assert out["evidence_requests"] == [planned]


def test_strategist_without_structured_concerns_does_not_create_requests():
    analysis = type(
        "Analysis",
        (),
        {
            "concerns": (),
        },
    )()
    out = G._evidence_strategist_node(None)(
        {
            "concern_analysis": analysis,
            "evidence_requests": [],
        }
    )
    assert out["evidence_requests"] == []
    assert out["council_trace"][0].detail == "no structured concerns available"


def test_graph_wires_four_stage_investigation_and_judgement():
    graph = G.build_review_graph(enable_summary=False, llm=None)
    drawable = graph.get_graph()
    pairs = {(edge.source, edge.target) for edge in drawable.edges}

    assert ("council_coordinator", "concern_analyzer") in pairs
    assert ("concern_analyzer", "evidence_strategist") in pairs
    assert ("evidence_strategist", "evidence_researcher") in pairs
    assert ("evidence_researcher", "impact_assessor") in pairs
    assert ("impact_assessor", "council_judge") in pairs
    assert "evidence_strategist" in drawable.nodes


def test_main_llm_is_effective_fallback_for_all_evidence_nodes_and_judge(monkeypatch):
    main_llm = object()
    captured = {}

    def strategist_factory(llm):
        captured["strategist"] = llm
        return lambda state: {}

    def researcher_factory(tool_client=None, judge_llm=None):
        captured["researcher"] = judge_llm
        return lambda state: {}

    def impact_factory(llm=None):
        captured["impact"] = llm
        return lambda state: {}

    def judge_factory(llm, judge_llm=None):
        captured["judge"] = judge_llm
        return lambda state: {}

    monkeypatch.setattr(G, "_evidence_strategist_node", strategist_factory)
    monkeypatch.setattr(G, "_evidence_researcher_node", researcher_factory)
    monkeypatch.setattr(G, "_impact_assessor_node", impact_factory)
    monkeypatch.setattr(G, "_council_judge_node", judge_factory)

    G.build_review_graph(enable_summary=False, llm=main_llm, fp_verify_llm=None)

    assert captured == {
        "strategist": main_llm,
        "researcher": main_llm,
        "impact": main_llm,
        "judge": main_llm,
    }


def test_impact_assessor_is_bounded_parallel_and_falls_back_per_concern(monkeypatch):
    concern = type(
        "Concern",
        (),
        {
            "concern_id": "concern-1",
            "tags": type(
                "Tags",
                (),
                {"primary_tag": None, "secondary_tags": ()},
            )(),
        },
    )()
    captured = {}

    def bounded(items, fn, *, max_workers):
        captured["max_workers"] = max_workers
        return [fn(item) for item in items]

    monkeypatch.setattr(G, "run_bounded_parallel", bounded)
    monkeypatch.setattr(
        G,
        "assess_impact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    out = G._impact_assessor_node(None)(
        {
            "concern_analysis": type(
                "Analysis", (), {"concerns": (concern,)}
            )(),
            "evidence_requests": [],
            "evidence_notes": [],
        }
    )

    assert captured["max_workers"] == 6
    assert out["impact_assessments"]["concern-1"].concern_id == "concern-1"
    assert out["council_trace"][0].event == "impact_assessment_degraded"


def test_impact_assessor_reuses_impact_relevant_support_findings(monkeypatch):
    concern = type(
        "Concern",
        (),
        {
            "concern_id": "concern-1",
            "tags": type(
                "Tags",
                (),
                {"primary_tag": None, "secondary_tags": ()},
            )(),
        },
    )()
    impact_request = EvidenceRequest(
        candidate_id="candidate-1",
        strategy_id="claim.observable_consequence.support",
        purpose="support",
        target="src/Service.java",
        question="该错误会产生什么运行时后果？",
        concern_id="concern-1",
        fact_type="observable_consequence",
    )
    root_cause_request = EvidenceRequest(
        candidate_id="candidate-1",
        strategy_id="claim.changed_condition.support",
        purpose="support",
        target="src/Service.java",
        question="变更是否存在？",
        concern_id="concern-1",
        fact_type="changed_condition",
    )
    notes = [
        EvidenceNote(
            request_id=impact_request.id,
            candidate_id="candidate-1",
            findings=[
                EvidenceFinding(
                    evidence_id="impact-fact",
                    source="task_patch",
                    observation="运行时调用路径可达并执行外部命令",
                    relation="supports",
                    strength="direct",
                )
            ],
        ),
        EvidenceNote(
            request_id=root_cause_request.id,
            candidate_id="candidate-1",
            findings=[
                EvidenceFinding(
                    evidence_id="root-cause-fact",
                    source="task_patch",
                    observation="代码条件发生变化",
                    relation="supports",
                    strength="direct",
                )
            ],
        ),
    ]
    captured = {}
    original = G.assess_impact

    def capture(concern_id, findings, rubric, *, llm=None):
        captured["evidence_ids"] = [finding.evidence_id for finding in findings]
        return original(concern_id, findings, rubric, llm=llm)

    monkeypatch.setattr(G, "assess_impact", capture)

    G._impact_assessor_node(None)(
        {
            "concern_analysis": type(
                "Analysis", (), {"concerns": (concern,)}
            )(),
            "evidence_requests": [impact_request, root_cause_request],
            "evidence_notes": notes,
        }
    )

    assert captured["evidence_ids"] == ["impact-fact"]
