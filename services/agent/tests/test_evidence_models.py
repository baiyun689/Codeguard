"""EvidenceRequest 模型的 Phase 5A 兼容性测试。"""

from hashlib import sha256

import pytest

from codeguard_agent.models.council import EvidenceRequest, JudgeDecision, Verdict
from codeguard_agent.models.schemas import Severity


def test_evidence_request_id_distinguishes_strategy_and_purpose():
    common = {
        "candidate_id": "candidate-1",
        "target": "src/Service.java",
        "question": "调用方是否完成权限校验？",
        "preferred_tools": ["find_callers", "get_file_content"],
    }

    baseline = EvidenceRequest(
        **common,
        strategy_id="auth-callers",
        purpose="counter",
    )
    different_strategy = EvidenceRequest(
        **common,
        strategy_id="auth-guards",
        purpose="counter",
    )
    different_purpose = EvidenceRequest(
        **common,
        strategy_id="auth-callers",
        purpose="support",
    )

    assert len({baseline.id, different_strategy.id, different_purpose.id}) == 3


def test_evidence_request_id_is_stable_for_identical_semantics():
    semantics = {
        "candidate_id": "candidate-1",
        "strategy_id": "auth-callers",
        "purpose": "severity",
        "target": "src/Service.java",
        "question": "影响范围是否跨越信任边界？",
        "preferred_tools": ["find_callers", "get_file_content"],
    }

    first = EvidenceRequest(**semantics)
    second = EvidenceRequest(**semantics)
    payload = "\0".join(
        [
            "candidate-1",
            "auth-callers",
            "severity",
            "src/Service.java",
            "影响范围是否跨越信任边界？",
            "find_callers",
            "get_file_content",
        ]
    )
    expected_id = f"evidence-{sha256(payload.encode('utf-8')).hexdigest()[:16]}"

    assert first.id == second.id == expected_id


def test_evidence_request_preserves_legacy_constructor_defaults():
    request = EvidenceRequest(
        candidate_id="candidate-1",
        target="src/Service.java",
        question="原有证据请求仍可构造",
        preferred_tools=["get_file_content"],
    )

    assert request.strategy_id == ""
    assert request.purpose == "counter"


@pytest.mark.parametrize("purpose", ["support", "counter", "severity"])
def test_verdict_requested_purpose_defaults_to_none_and_accepts_all_values(purpose):
    baseline = Verdict(
        candidate_id="candidate-1",
        action="keep",
        reason_code="done",
    )
    requested = Verdict(
        candidate_id="candidate-1",
        action="needs_more_evidence",
        reason_code="more",
        requested_purpose=purpose,
    )

    assert baseline.requested_purpose is None
    assert requested.requested_purpose == purpose


@pytest.mark.parametrize("purpose", ["support", "counter", "severity"])
def test_judge_decision_requested_purpose_defaults_to_none_and_accepts_all_values(
    purpose,
):
    baseline = JudgeDecision(candidate_id="candidate-1", action="keep")
    requested = JudgeDecision(
        candidate_id="candidate-1",
        action="needs_more_evidence",
        adjusted_severity=Severity.WARNING,
        requested_purpose=purpose,
    )

    assert baseline.requested_purpose is None
    assert requested.requested_purpose == purpose
