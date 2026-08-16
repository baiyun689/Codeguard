"""Phase 5B evidence model contracts."""

from codeguard_agent.models import council
from codeguard_agent.models.council import Verdict


def test_legacy_evidence_types_are_removed():
    assert not hasattr(council, "EvidenceNoteStatus")
    assert not hasattr(council, "EvidenceJudgment")
    assert not hasattr(council, "build_evidence_requests")
    assert not hasattr(council, "EvidenceRequest")
    assert not hasattr(council, "EvidenceNote")
    assert not hasattr(council, "EvidenceFinding")
    assert not hasattr(council, "CandidateEvidenceAssessment")


def test_verdict_action_is_keep_or_drop():
    keep = Verdict(candidate_id="c-1", action="keep", reason_code="ok")
    drop = Verdict(candidate_id="c-1", action="drop", reason_code="bad")
    assert keep.action == "keep"
    assert drop.action == "drop"
    assert keep.resolved_severity is None
