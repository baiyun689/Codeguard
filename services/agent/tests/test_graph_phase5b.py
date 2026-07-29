"""Phase 5B graph wiring and single-writer contracts."""

from __future__ import annotations

import codeguard_agent.pipeline.graph as G
from codeguard_agent.models.council import EvidenceRequest
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


def test_planner_node_is_the_request_writer(monkeypatch):
    planned = _request(99)
    monkeypatch.setattr(
        G,
        "plan_claim_evidence",
        lambda _concern: type(
            "Plan",
            (),
            {
                "requests": (planned,),
                "goals": (),
                "uncovered_goals": (),
            },
        )(),
    )
    concern = type("Concern", (), {"concern_id": "concern-1"})()
    analysis = type(
        "Analysis",
        (),
        {
            "concerns": (concern,),
        },
    )()

    out = G._evidence_planner_node(None)(
        {
            "concern_analysis": analysis,
            "evidence_requests": [],
        }
    )

    assert out["evidence_requests"] == [planned]


def test_planner_without_structured_concerns_does_not_restore_tag_fallback():
    analysis = type(
        "Analysis",
        (),
        {
            "concerns": (),
        },
    )()
    out = G._evidence_planner_node(None)(
        {
            "concern_analysis": analysis,
            "evidence_requests": [],
        }
    )
    assert out["evidence_requests"] == []
    assert out["council_trace"][0].detail == "no structured concerns available"


def test_graph_wires_one_pass_planner_before_agent():
    graph = G.build_review_graph(enable_summary=False, llm=None)
    drawable = graph.get_graph()
    pairs = {(edge.source, edge.target) for edge in drawable.edges}

    assert ("council_coordinator", "concern_analyzer") in pairs
    assert ("concern_analyzer", "evidence_planner") in pairs
    assert ("evidence_planner", "evidence_agent") in pairs
    assert ("evidence_agent", "council_judge") in pairs
    assert "evidence_planner" in drawable.nodes


def test_main_llm_is_effective_fallback_for_planner_agent_and_judge(monkeypatch):
    main_llm = object()
    captured = {}

    def planner_factory(llm):
        captured["planner"] = llm
        return lambda state: {}

    def agent_factory(tool_client=None, judge_llm=None):
        captured["agent"] = judge_llm
        return lambda state: {}

    def judge_factory(llm, judge_llm=None):
        captured["judge"] = judge_llm
        return lambda state: {}

    monkeypatch.setattr(G, "_evidence_planner_node", planner_factory)
    monkeypatch.setattr(G, "_evidence_agent_node", agent_factory)
    monkeypatch.setattr(G, "_council_judge_node", judge_factory)

    G.build_review_graph(enable_summary=False, llm=main_llm, fp_verify_llm=None)

    assert captured == {"planner": main_llm, "agent": main_llm, "judge": main_llm}
