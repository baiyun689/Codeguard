"""guard_scan 确定性反证扫描测试(ADR-046)。"""
from codeguard_agent.models.council import CandidateFact
from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence.guard_scan import scan_guard_fact


def test_scan_guard_detects_preauthorize_as_direct_contradicts():
    fact = CandidateFact(
        fact_id="f1", source="tool:get_file_content",
        raw='@PreAuthorize("hasRole(\'ADMIN\')")\npublic void update() {}',
        replay_status="verified",
    )
    relation = scan_guard_fact(fact, RiskTag.AUTHORIZATION)
    assert relation is not None
    assert relation.relation == "contradicts"
    assert relation.strength == "direct"
    assert relation.observation.strip()


def test_scan_guard_detects_transactional_for_transaction_tag():
    fact = CandidateFact(
        fact_id="f1", source="tool:get_file_content",
        raw="@Transactional\npublic void placeOrder() {}",
        replay_status="verified",
    )
    relation = scan_guard_fact(fact, RiskTag.TRANSACTION_ATOMICITY)
    assert relation is not None
    assert relation.relation == "contradicts"


def test_scan_guard_silent_for_non_security_tags():
    fact = CandidateFact(fact_id="f1", source="tool:get_file_content",
                         raw="@PreAuthorize(...)\nvoid f() {}", replay_status="verified")
    assert scan_guard_fact(fact, RiskTag.PERFORMANCE) is None


def test_scan_guard_silent_without_annotation():
    fact = CandidateFact(fact_id="f1", source="tool:get_file_content",
                         raw="public void f() {}", replay_status="verified")
    assert scan_guard_fact(fact, RiskTag.AUTHORIZATION) is None


def test_scan_guard_silent_for_empty_raw():
    fact = CandidateFact(fact_id="f1", source="tool:get_file_content", raw="")
    assert scan_guard_fact(fact, RiskTag.AUTHORIZATION) is None
