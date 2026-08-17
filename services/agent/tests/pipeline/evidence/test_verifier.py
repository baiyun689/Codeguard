"""Evidence Ledger 验证器测试:Artifact 健康检查 / 图护栏 / guard 扫描 / 异常重放。

正常路径零 LLM、零重放;只证明 Artifact 真实可用、属于候选范围。
"""

from __future__ import annotations

import json

from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.evidence import (
    EvidenceArtifact,
    EvidenceArtifactStatus,
    EvidenceCaptureMode,
    EvidenceRef,
    EvidenceSourceKind,
    EvidenceValidationStatus,
)
from codeguard_agent.models.schemas import EvidenceRole, Severity
from codeguard_agent.models.tasks import ContextFact, ReviewTask, RiskTag, TaskContextBundle
from codeguard_agent.pipeline.evidence.planner import CandidateDossier
from codeguard_agent.pipeline.evidence.verifier import verify_evidence
from codeguard_agent.tools.tool_client import ToolResponse

REV = "abc123:deadbeef"
TASK_ID = "task-1"


def _task() -> ReviewTask:
    return ReviewTask(
        id=TASK_ID, file="src/A.java", patch="+    exec(cmd);\n", changed_lines=[1]
    )


def _candidate(*artifact_ids: str) -> CandidateIssue:
    return CandidateIssue(
        id="c1",
        task_id=TASK_ID,
        source_agent="threat_model",
        file="src/A.java",
        line=1,
        type="command-injection",
        severity_proposal=Severity.WARNING,
        claim="未转义参数进入命令构造",
        confidence=0.8,
        evidence_refs=[
            EvidenceRef(artifact_id=artifact_id, declared_role=EvidenceRole.MECHANISM)
            for artifact_id in artifact_ids
        ],
    )


def _dossier(candidate: CandidateIssue, bundle: TaskContextBundle | None = None) -> CandidateDossier:
    return CandidateDossier(candidate=candidate, task=_task(), context_bundle=bundle)


def _patch_artifact(payload: str = "+    exec(cmd);\n") -> EvidenceArtifact:
    return EvidenceArtifact.build(
        task_id=TASK_ID, reviewer="threat_model", revision=REV,
        source_kind=EvidenceSourceKind.TASK_PATCH, payload=payload,
        status=EvidenceArtifactStatus.COMPLETE,
        capture_mode=EvidenceCaptureMode.GENERATED,
        arguments={"file_path": "src/A.java"},
    )


def _file_artifact(payload: str = "class A { void m() { exec(cmd); } }") -> EvidenceArtifact:
    return EvidenceArtifact.build(
        task_id=TASK_ID, reviewer="threat_model", revision=REV,
        source_kind=EvidenceSourceKind.TOOL_CALL, tool="get_file_content",
        arguments={"file_path": "src/A.java"}, payload=payload,
        status=EvidenceArtifactStatus.COMPLETE,
        capture_mode=EvidenceCaptureMode.EXECUTED,
    )


def _graph_payload(
    *,
    subject: str = "java:A#m()",
    status: str = "confirmed",
    coverage: str = "full",
    source_scope: str = "MAIN",
    relationships: list | None = None,
) -> str:
    return json.dumps({
        "status": status,
        "coverage": coverage,
        "source_scope": source_scope,
        "subject_symbol_id": subject,
        "symbols": [{"id": subject, "kind": "method"}],
        "relationships": relationships if relationships is not None else [
            {"sourceId": "java:A#m()", "targetId": "java:B#exec()", "kind": "calls",
             "file": "A.java", "line": 1, "source_set": "MAIN"},
        ],
        "limitations": [],
    }, ensure_ascii=False)


def _graph_artifact(
    payload: str,
    *,
    revision: str = REV,
    status: EvidenceArtifactStatus = EvidenceArtifactStatus.COMPLETE,
) -> EvidenceArtifact:
    return EvidenceArtifact.build(
        task_id=TASK_ID, reviewer="threat_model", revision=revision,
        source_kind=EvidenceSourceKind.TOOL_CALL, tool="inspect_change_impact",
        arguments={"symbol_id": "java:A#m()"}, payload=payload,
        status=status, capture_mode=EvidenceCaptureMode.EXECUTED,
    )


class _FakeToolClient:
    def __init__(self, result: str, success: bool = True):
        self._result = result
        self._success = success
        self.calls = 0

    def inspect_change_impact(self, symbol_id: str):
        self.calls += 1
        return ToolResponse(success=self._success, result=self._result)


def _verify(candidate: CandidateIssue, artifacts: dict, *, tool_client=None, revision: str = REV, enabled_replay_tools=None):
    dossier = _dossier(candidate)
    return verify_evidence(
        [dossier],
        artifacts=artifacts,
        tool_client=tool_client,
        revision=revision,
        enabled_replay_tools=enabled_replay_tools,
        tag_by_candidate={candidate.id: RiskTag.INJECTION},
    )


# ── patch/context/工具健康 ─────────────────────────────────────────────


def test_patch_hash_有效_grounded():
    patch = _patch_artifact()
    batch = _verify(_candidate(patch.id), {patch.id: patch})
    verification = batch.candidates["c1"]
    assert verification.grounding_status == "grounded"
    assert verification.eligible_for_judge is True
    assert any(
        item.artifact_id == patch.id
        and item.validation_status is EvidenceValidationStatus.VALID
        for item in verification.valid_evidence
    )


def test_patch_hash_篡改_ungrounded_不可裁决():
    patch = _patch_artifact()
    corrupted = patch.model_copy(update={"payload_hash": "0" * 64})
    batch = _verify(_candidate(corrupted.id), {corrupted.id: corrupted})
    verification = batch.candidates["c1"]
    assert verification.grounding_status == "ungrounded"
    assert verification.eligible_for_judge is False
    assert verification.rejection_reason == "patch_artifact_missing_or_corrupt"


def test_context_fact_partial_标_limited():
    patch = _patch_artifact()
    context = EvidenceArtifact.build(
        task_id=TASK_ID, reviewer="threat_model", revision=REV,
        source_kind=EvidenceSourceKind.PREFETCHED_CONTEXT,
        tool="resolve_change_context", payload="symbol A",
        status=EvidenceArtifactStatus.PARTIAL,
        capture_mode=EvidenceCaptureMode.GENERATED,
        limitations=("context_truncated",),
    )
    batch = _verify(_candidate(patch.id, context.id), {patch.id: patch, context.id: context})
    verification = batch.candidates["c1"]
    assert verification.grounding_status == "partially_grounded"
    context_items = [
        item for item in verification.valid_evidence
        if item.artifact_id == context.id
    ]
    assert context_items[0].validation_status is EvidenceValidationStatus.LIMITED
    assert "context_truncated" in context_items[0].limitations


def test_文件工具_complete_valid():
    patch = _patch_artifact()
    file_artifact = _file_artifact()
    batch = _verify(
        _candidate(patch.id, file_artifact.id),
        {patch.id: patch, file_artifact.id: file_artifact},
    )
    verification = batch.candidates["c1"]
    assert verification.grounding_status == "grounded"
    file_items = [
        item for item in verification.valid_evidence
        if item.artifact_id == file_artifact.id
    ]
    assert file_items[0].validation_status is EvidenceValidationStatus.VALID


# ── 图护栏 ─────────────────────────────────────────────────────────────


def test_图响应_valid_护栏通过():
    patch = _patch_artifact()
    graph = _graph_artifact(_graph_payload())
    batch = _verify(_candidate(patch.id, graph.id), {patch.id: patch, graph.id: graph})
    verification = batch.candidates["c1"]
    assert verification.grounding_status == "grounded"
    graph_items = [
        item for item in verification.valid_evidence if item.artifact_id == graph.id
    ]
    assert graph_items[0].validation_status is EvidenceValidationStatus.VALID


def test_图响应_subject_mismatch_invalid():
    patch = _patch_artifact()
    graph = _graph_artifact(_graph_payload(subject="java:Other#x()"))
    batch = _verify(_candidate(patch.id, graph.id), {patch.id: patch, graph.id: graph})
    verification = batch.candidates["c1"]
    assert verification.grounding_status == "partially_grounded"
    assert verification.invalid_references
    assert "graph_subject_mismatch" in verification.invalid_references[0].detail


def test_图响应_test_only_confirmation_invalid():
    patch = _patch_artifact()
    payload = json.loads(_graph_payload(relationships=[]))
    payload["test_relationships"] = [{
        "sourceId": "java:A#m()", "targetId": "java:T#t()", "kind": "calls",
        "file": "A.java", "line": 1, "source_set": "TEST",
    }]
    graph = _graph_artifact(json.dumps(payload, ensure_ascii=False))
    batch = _verify(_candidate(patch.id, graph.id), {patch.id: patch, graph.id: graph})
    verification = batch.candidates["c1"]
    assert verification.grounding_status == "partially_grounded"
    assert verification.invalid_references


def test_图响应_coverage_partial_limited_保留正事实():
    patch = _patch_artifact()
    graph = _graph_artifact(_graph_payload(coverage="partial"))
    batch = _verify(_candidate(patch.id, graph.id), {patch.id: patch, graph.id: graph})
    verification = batch.candidates["c1"]
    assert verification.grounding_status == "partially_grounded"
    graph_items = [
        item for item in verification.valid_evidence if item.artifact_id == graph.id
    ]
    assert graph_items[0].validation_status is EvidenceValidationStatus.LIMITED
    assert "graph_coverage_partial" in graph_items[0].limitations


# ── 异常重放 ───────────────────────────────────────────────────────────


def test_图响应_status_unknown_触发重放_确认后_replay_confirmed():
    patch = _patch_artifact()
    graph = _graph_artifact(_graph_payload(status="unknown"))
    client = _FakeToolClient(_graph_payload())
    batch = _verify(
        _candidate(patch.id, graph.id),
        {patch.id: patch, graph.id: graph},
        tool_client=client,
    )
    verification = batch.candidates["c1"]
    assert graph.id in batch.replayed_artifact_ids
    graph_items = [
        item for item in verification.valid_evidence if item.artifact_id == graph.id
    ]
    assert graph_items[0].validation_status is EvidenceValidationStatus.REPLAY_CONFIRMED
    assert client.calls == 1


def test_失败artifact_白名单空_禁止重放_limited():
    patch = _patch_artifact()
    graph = _graph_artifact(_graph_payload(), status=EvidenceArtifactStatus.FAILED)
    batch = _verify(
        _candidate(patch.id, graph.id),
        {patch.id: patch, graph.id: graph},
        tool_client=_FakeToolClient(_graph_payload()),
        enabled_replay_tools=[],
    )
    verification = batch.candidates["c1"]
    graph_items = [
        item for item in verification.valid_evidence if item.artifact_id == graph.id
    ]
    assert graph_items[0].validation_status is EvidenceValidationStatus.LIMITED
    assert "replay_not_enabled" in graph_items[0].limitations


def test_重放失败_只产生限制_不作为反证():
    patch = _patch_artifact()
    graph = _graph_artifact(_graph_payload(), status=EvidenceArtifactStatus.FAILED)
    batch = _verify(
        _candidate(patch.id, graph.id),
        {patch.id: patch, graph.id: graph},
        tool_client=_FakeToolClient("", success=False),
    )
    verification = batch.candidates["c1"]
    graph_items = [
        item for item in verification.valid_evidence if item.artifact_id == graph.id
    ]
    assert graph_items[0].validation_status is EvidenceValidationStatus.LIMITED
    assert any("replay" in lim for lim in graph_items[0].limitations)


def test_revision_mismatch_触发重放():
    patch = _patch_artifact()
    graph = _graph_artifact(_graph_payload(), revision="other:rev")
    client = _FakeToolClient(_graph_payload())
    batch = _verify(
        _candidate(patch.id, graph.id),
        {patch.id: patch, graph.id: graph},
        tool_client=client,
    )
    assert graph.id in batch.replayed_artifact_ids


def test_重放_相同调用全局只执行一次():
    patch = _patch_artifact()
    graph = _graph_artifact(_graph_payload(status="unknown"))
    candidate_b = _candidate(patch.id, graph.id).model_copy(update={"id": "c2"})
    dossier_b = _dossier(candidate_b)
    client = _FakeToolClient(_graph_payload())
    batch = verify_evidence(
        [_dossier(_candidate(patch.id, graph.id)), dossier_b],
        artifacts={patch.id: patch, graph.id: graph},
        tool_client=client,
        revision=REV,
        enabled_replay_tools=None,
        tag_by_candidate={"c1": RiskTag.INJECTION, "c2": RiskTag.INJECTION},
    )
    assert batch.replayed_artifact_ids == [graph.id]
    assert client.calls == 1


# ── guard 扫描与引用范围 ───────────────────────────────────────────────


def _guard_bundle() -> TaskContextBundle:
    return TaskContextBundle(
        task_id=TASK_ID,
        facts=[
            ContextFact(
                source="tool:resolve_change_context",
                kind="symbol_context",
                content=json.dumps(
                    {
                        "file": "src/A.java",
                        "symbol_id": "java:A#m()",
                        "kind": "method",
                        "start_line": 1,
                        "end_line": 2,
                        "signature": "public void m()",
                        "annotations": ["PreAuthorize"],
                        "resolution": "resolved",
                    },
                    sort_keys=True,
                ),
            )
        ],
    )


def test_guard_注解命中_直接反证_不可裁决():
    patch = _patch_artifact()
    batch = verify_evidence(
        [_dossier(_candidate(patch.id), bundle=_guard_bundle())],
        artifacts={patch.id: patch},
        tool_client=None,
        revision=REV,
        enabled_replay_tools=None,
        tag_by_candidate={"c1": RiskTag.AUTHORIZATION},
    )
    verification = batch.candidates["c1"]
    assert verification.eligible_for_judge is False
    assert verification.rejection_reason == "direct_counter_guard"


def test_引用指向缺失artifact_无效引用_partially_grounded():
    patch = _patch_artifact()
    batch = _verify(_candidate(patch.id, "ev-missing"), {patch.id: patch})
    verification = batch.candidates["c1"]
    assert verification.grounding_status == "partially_grounded"
    assert verification.invalid_references


def test_跨任务artifact_无效引用():
    patch = _patch_artifact()
    foreign = _file_artifact().model_copy(update={"task_id": "task-other"})
    batch = _verify(_candidate(patch.id, foreign.id), {patch.id: patch, foreign.id: foreign})
    verification = batch.candidates["c1"]
    assert verification.grounding_status == "partially_grounded"
    assert verification.invalid_references


def test_候选无patch引用_ungrounded():
    # 自动 patch 引用缺失(如绑定异常)时,候选不可裁决。
    batch = _verify(_candidate(), {})
    verification = batch.candidates["c1"]
    assert verification.grounding_status == "ungrounded"
    assert verification.eligible_for_judge is False


# ── 指标事件 ───────────────────────────────────────────────────────────


def test_验证指标事件_存在():
    patch = _patch_artifact()
    batch = _verify(_candidate(patch.id), {patch.id: patch})
    events = [event for event, _detail in batch.trace]
    assert "evidence_verification_metrics" in events
    detail = json.loads(batch.trace[-1][1])
    assert detail["candidates"] == 1
    assert detail["artifacts_patch"] == 1
    assert detail["judge_eligible"] == 1
