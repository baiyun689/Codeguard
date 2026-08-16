"""verdict 门控测试(ADR-046)。"""
from codeguard_agent.models.council import FactRelation
from codeguard_agent.pipeline.council.verdict import gate_candidate


def _relation(relation, strength="contextual"):
    return FactRelation(
        fact_id="f1", relation=relation, strength=strength,
        observation="obs" if relation in {"supports", "contradicts"} else "",
        limitation="" if relation in {"supports", "contradicts"} else "lim",
    )


def test_gate_direct_counter_drops():
    code, _ = gate_candidate([
        _relation("supports"), _relation("contradicts", strength="direct"),
    ])
    assert code == "direct_counter_evidence"


def test_gate_no_facts_drops():
    code, _ = gate_candidate([])
    assert code == "evidence_insufficient"


def test_gate_all_insufficient_drops():
    code, _ = gate_candidate([_relation("insufficient")])
    assert code == "evidence_insufficient"


def test_gate_no_support_drops():
    code, _ = gate_candidate([_relation("contradicts")])  # contextual 反证不构成直接反证
    assert code == "no_supporting_evidence"


def test_gate_supported_passes():
    assert gate_candidate([_relation("supports")]) is None
