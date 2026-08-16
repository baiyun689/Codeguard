"""Phase 5B graph wiring and single-writer contracts(ADR-046 两节点证据链版)。"""

from __future__ import annotations

import codeguard_agent.pipeline.graph as G
from codeguard_agent.models.tasks import TaskSelection
from codeguard_agent.pipeline.reviewers.reviewers import DEFAULT_REVIEWERS


def test_reviewer_branch_never_writes_evidence_fields():
    node = G.make_reviewer_node(DEFAULT_REVIEWERS[0], llm=None, tool_client=None)

    out = node(
        {
            "review_tasks": [],
            "risk_priors": {},
            "task_selection": TaskSelection(selected_task_ids=[]),
        }
    )

    assert "evidence_requests" not in out
    assert "candidate_facts" not in out
    assert "candidate_relations" not in out


def test_graph_wires_verifier_and_judge():
    graph = G.build_review_graph(enable_summary=False, llm=None)
    drawable = graph.get_graph()
    pairs = {(edge.source, edge.target) for edge in drawable.edges}

    assert ("council_coordinator", "evidence_verifier") in pairs
    assert ("evidence_verifier", "council_judge") in pairs
    assert "evidence_verifier" in drawable.nodes
    assert "concern_analyzer" not in drawable.nodes
    assert "evidence_strategist" not in drawable.nodes
    assert "evidence_researcher" not in drawable.nodes
    assert "impact_assessor" not in drawable.nodes


def test_main_llm_is_effective_fallback_for_verifier_and_judges(monkeypatch):
    main_llm = object()
    captured = {}

    def verifier_factory(tool_client=None, judge_llm=None):
        captured["verifier"] = judge_llm
        return lambda state: {}

    def judge_factory(llm, judge_llm=None):
        captured["judge"] = judge_llm
        return lambda state: {}

    def direct_factory(judge_llm=None):
        captured["direct"] = judge_llm
        return lambda state: {}

    monkeypatch.setattr(G, "_evidence_verifier_node", verifier_factory)
    monkeypatch.setattr(G, "_council_judge_node", judge_factory)
    monkeypatch.setattr(G, "_direct_judge_node", direct_factory)

    G.build_review_graph(enable_summary=False, llm=main_llm, fp_verify_llm=None)

    assert captured == {
        "verifier": main_llm,
        "judge": main_llm,
        "direct": main_llm,
    }
