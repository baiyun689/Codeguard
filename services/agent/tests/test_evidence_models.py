"""Phase 5B evidence model contracts."""

from hashlib import sha256

import pytest
from pydantic import ValidationError

from codeguard_agent.models import council
from codeguard_agent.models.council import (
    CandidateEvidenceAssessment,
    EvidenceRequest,
    SeverityFactorAssessment,
    Verdict,
)


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


@pytest.mark.parametrize(
    "missing",
    ["candidate_id", "strategy_id", "purpose", "target", "question"],
)
def test_evidence_request_requires_all_strategy_fields(missing):
    values = {
        "candidate_id": "candidate-1",
        "strategy_id": "authorization.counter",
        "purpose": "counter",
        "target": "src/Service.java",
        "question": "当前作用域是否已有鉴权保护？",
    }
    values.pop(missing)

    with pytest.raises(ValidationError):
        EvidenceRequest(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_id", " "),
        ("strategy_id", ""),
        ("target", "\t"),
        ("question", "\n"),
    ],
)
def test_evidence_request_rejects_blank_strategy_fields(field, value):
    values = {
        "candidate_id": "candidate-1",
        "strategy_id": "authorization.counter",
        "purpose": "counter",
        "target": "src/Service.java",
        "question": "当前作用域是否已有鉴权保护？",
    }
    values[field] = value

    with pytest.raises(ValidationError):
        EvidenceRequest(**values)


def test_evidence_note_requires_at_least_one_finding():
    with pytest.raises(ValidationError):
        council.EvidenceNote(request_id="request-1", candidate_id="candidate-1", findings=[])


@pytest.mark.parametrize("relation", ["supports", "contradicts"])
def test_relational_finding_requires_observation(relation):
    with pytest.raises(ValidationError):
        council.EvidenceFinding(
            evidence_id="evidence-1",
            source="task_patch",
            observation=" ",
            relation=relation,
            strength="direct",
        )


def test_insufficient_finding_is_contextual_and_requires_limitation():
    with pytest.raises(ValidationError):
        council.EvidenceFinding(
            evidence_id="evidence-1",
            source="task_patch",
            observation="",
            relation="insufficient",
            strength="direct",
            limitation="not enough context",
        )
    with pytest.raises(ValidationError):
        council.EvidenceFinding(
            evidence_id="evidence-1",
            source="task_patch",
            observation="",
            relation="insufficient",
            strength="contextual",
            limitation=" ",
        )


def test_legacy_evidence_types_are_removed():
    assert not hasattr(council, "EvidenceNoteStatus")
    assert not hasattr(council, "EvidenceJudgment")
    assert not hasattr(council, "build_evidence_requests")


def test_verdict_action_is_keep_or_drop():
    keep = Verdict(candidate_id="c-1", action="keep", reason_code="ok")
    drop = Verdict(candidate_id="c-1", action="drop", reason_code="bad")
    assert keep.action == "keep"
    assert drop.action == "drop"
    assert keep.resolved_severity is None


# ── Evidence synthesis models (Task 1) ──


def test_candidate_evidence_assessment_accepts_only_bounded_factor_states():
    assessment = CandidateEvidenceAssessment(
        candidate_id="C001",
        claim_status="supported",
        counter_effect="partial",
        severity_factors=[
            SeverityFactorAssessment(
                factor_id="untrusted_input",
                status="proven",
                evidence_ids=["E1"],
                reason="request parameter reaches the query builder",
            )
        ],
        conflicts=[],
        reason="candidate remains supported after partial mitigation",
    )
    assert assessment.severity_factors[0].status == "proven"


def test_synthesis_model_rejects_unknown_status():
    with pytest.raises(ValidationError):
        SeverityFactorAssessment(
            factor_id="untrusted_input",
            status="likely",
            evidence_ids=["E1"],
            reason="invalid unbounded state",
        )


def test_candidate_evidence_assessment_requires_candidate_id():
    with pytest.raises(ValidationError):
        CandidateEvidenceAssessment(
            candidate_id="  ",
            claim_status="supported",
            counter_effect="none",
        )


def test_severity_factor_assessment_requires_factor_id():
    with pytest.raises(ValidationError):
        SeverityFactorAssessment(
            factor_id="",
            status="proven",
        )
