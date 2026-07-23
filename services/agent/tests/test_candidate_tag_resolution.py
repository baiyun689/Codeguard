"""批量候选 RiskTag 解析的契约测试。"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.evidence_rules.classify import (
    CandidateTagResolution,
    resolve_candidate_tags,
)


def _dossier(candidate_type: str = "authorization", candidate_id: str = "candidate-a"):
    """构建轻量 dossier，满足 resolve_candidate_evidence_tag 的最小契约。"""
    candidate = SimpleNamespace(
        id=candidate_id,
        type=candidate_type,
        claim="missing authorization check",
        suggestion="add @PreAuthorize",
    )
    task = SimpleNamespace(
        id="task-0",
        file="src/Service.java",
        patch="+ authorize(request);",
    )
    return SimpleNamespace(candidate=candidate, task=task, risk_profile=None)


def test_batch_candidate_tag_resolution_keeps_input_mapping_and_falls_back(monkeypatch):
    dossiers = [
        _dossier(candidate_type="authorization", candidate_id="candidate-a"),
        _dossier(candidate_type="resource leak", candidate_id="candidate-b"),
    ]

    calls = []

    def fake_resolve(dossier, classifier_llm, *, structured_method):
        calls.append(dossier.candidate.id)
        if dossier.candidate.id == "candidate-b":
            raise RuntimeError("classifier failed")
        return CandidateTagResolution(
            tag=RiskTag.AUTHORIZATION,
            confidence=0.95,
            source="rule",
            reason="test",
        )

    monkeypatch.setattr(
        "codeguard_agent.pipeline.evidence_rules.classify.resolve_candidate_evidence_tag",
        fake_resolve,
    )

    result = resolve_candidate_tags(
        dossiers,
        classifier_llm=object(),
        structured_method="function_calling",
        max_workers=2,
    )

    assert set(calls) == {"candidate-a", "candidate-b"}
    assert result["candidate-a"].tag is RiskTag.AUTHORIZATION
    assert result["candidate-b"].tag is RiskTag.GENERAL_REVIEW
    assert result["candidate-b"].source == "general"


def test_batch_resolution_runs_concurrently_and_keeps_input_order(monkeypatch):
    lock = threading.Lock()
    active = 0
    peak_active = 0

    def fake_resolve(dossier, classifier_llm, *, structured_method):
        nonlocal active, peak_active
        with lock:
            active += 1
            peak_active = max(peak_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return CandidateTagResolution(
            tag=RiskTag.AUTHORIZATION,
            confidence=0.95,
            source="rule",
            reason=dossier.candidate.id,
        )

    monkeypatch.setattr(
        "codeguard_agent.pipeline.evidence_rules.classify.resolve_candidate_evidence_tag",
        fake_resolve,
    )

    dossiers = [_dossier(candidate_id=f"candidate-{i}") for i in range(4)]

    result = resolve_candidate_tags(
        dossiers,
        classifier_llm=object(),
        structured_method="function_calling",
        max_workers=4,
    )

    assert peak_active > 1
    assert list(result.keys()) == [d.candidate.id for d in dossiers]


def test_batch_resolution_caps_public_worker_limit_at_eight(monkeypatch):
    observed: list[int] = []

    def run(items, fn, *, max_workers):
        observed.append(max_workers)
        return [fn(item) for item in items]

    monkeypatch.setattr(
        "codeguard_agent.pipeline.concurrency.run_bounded_parallel",
        run,
    )

    resolve_candidate_tags(
        [_dossier()],
        classifier_llm=None,
        structured_method="function_calling",
        max_workers=99,
    )

    assert observed == [8]
