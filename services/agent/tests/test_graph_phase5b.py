"""Phase 5B graph wiring and single-writer contracts(ADR-046 两节点证据链版)。"""

from __future__ import annotations
import codeguard_agent.pipeline.orchestration.graph as G
from codeguard_agent.models.tasks import TaskSelection


def test_graph_wires_verifier_and_judge():
    graph = G.build_review_graph(llm=None)
    drawable = graph.get_graph()
    pairs = {(edge.source, edge.target) for edge in drawable.edges}
    assert ("council_coordinator", "evidence_verifier") in pairs
    assert ("evidence_verifier", "council_judge") in pairs
    assert "evidence_verifier" in drawable.nodes
    assert "concern_analyzer" not in drawable.nodes
    assert "evidence_strategist" not in drawable.nodes
    assert "evidence_researcher" not in drawable.nodes
    assert "impact_assessor" not in drawable.nodes


def test_graph_defaults_to_controlled_discovery():
    graph = G.build_review_graph(llm=None)
    nodes = set(graph.get_graph().nodes)
    assert "controlled_review" in nodes
    assert "review_plan" not in nodes
    assert not any((node.startswith("discover_") for node in nodes))


def test_main_llm_is_effective_fallback_for_verifier_and_judges(monkeypatch):
    main_llm = object()
    captured = {}

    def verifier_factory(tool_client=None, judge_llm=None):
        captured["verifier"] = judge_llm
        return lambda state: {}

    def judge_factory(judge_llm=None):
        captured["judge"] = judge_llm
        return lambda state: {}

    def direct_factory(judge_llm=None):
        captured["direct"] = judge_llm
        return lambda state: {}

    monkeypatch.setattr(G, "_evidence_verifier_node", verifier_factory)
    monkeypatch.setattr(G, "_council_judge_node", judge_factory)
    monkeypatch.setattr(G, "_direct_judge_node", direct_factory)
    G.build_review_graph(llm=main_llm, fp_verify_llm=None)
    assert captured == {"verifier": None, "judge": main_llm}
