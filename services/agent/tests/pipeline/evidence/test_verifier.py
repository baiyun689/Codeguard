"""Evidence Ledger 验证器测试:Artifact 健康检查 / 图护栏 / guard 扫描 / 异常重放。

正常路径零 LLM、零重放;只证明 Artifact 真实可用、属于候选范围。
"""

from __future__ import annotations

import json

from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.evidence import (
    ArtifactAvailability,
    EvidenceArtifact,
    EvidenceCaptureMode,
    EvidenceRef,
    EvidenceSourceKind,
    EvidenceValidationStatus,
)
from codeguard_agent.models.schemas import EvidenceRole, Severity
from codeguard_agent.models.tasks import (
    ResolvedSymbol,
    ReviewTask,
    SymbolResolutionStatus,
    TaskSymbolContext,
)
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


def _dossier(
    candidate: CandidateIssue, context: TaskSymbolContext | None = None
) -> CandidateDossier:
    return CandidateDossier(candidate=candidate, task=_task(), symbol_context=context)


def _patch_artifact(payload: str = "+    exec(cmd);\n") -> EvidenceArtifact:
    return EvidenceArtifact.build(
        task_id=TASK_ID, reviewer="threat_model", revision=REV,
        source_kind=EvidenceSourceKind.TASK_PATCH, payload=payload,
        availability=ArtifactAvailability.AVAILABLE,
        capture_mode=EvidenceCaptureMode.GENERATED,
        arguments={"file_path": "src/A.java"},
    )


def _file_artifact(payload: str = "class A { void m() { exec(cmd); } }") -> EvidenceArtifact:
    return EvidenceArtifact.build(
        task_id=TASK_ID, reviewer="threat_model", revision=REV,
        source_kind=EvidenceSourceKind.TOOL_CALL, tool="get_file_content",
        arguments={"file_path": "src/A.java"}, payload=payload,
        availability=ArtifactAvailability.AVAILABLE,
        capture_mode=EvidenceCaptureMode.EXECUTED,
    )


def _graph_payload(
    *,
    subject: str = "java:A#m()",
    outcome: str = "found",
    coverage: str = "complete",
    source_scope: str = "MAIN",
    relationships: list | None = None,
) -> str:
    return json.dumps({
        "schema_version": 2,
        "outcome": outcome,
        "coverage": coverage,
        "source_scope": source_scope,
        "subject_symbol_id": subject,
        "symbols": [{"id": subject, "kind": "method", "source_set": source_scope}],
        "relationships": relationships if relationships is not None else [
            {"sourceId": "java:A#m()", "targetId": "java:B#exec()", "kind": "calls",
             "file": "A.java", "line": 1, "source_set": "MAIN",
             "resolution": "RESOLVED"},
        ],
        "unresolved_relationships": [],
        "unresolved_count": 0,
        "limitations": [],
    }, ensure_ascii=False)


def _graph_artifact(
    payload: str,
    *,
    revision: str = REV,
    availability: ArtifactAvailability = ArtifactAvailability.AVAILABLE,
) -> EvidenceArtifact:
    return EvidenceArtifact.build(
        task_id=TASK_ID, reviewer="threat_model", revision=revision,
        source_kind=EvidenceSourceKind.TOOL_CALL, tool="inspect_change_impact",
        arguments={"symbol_id": "java:A#m()"}, payload=payload,
        availability=availability, capture_mode=EvidenceCaptureMode.EXECUTED,
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
    symbol = ResolvedSymbol(
        file="src/A.java",
        symbol_id="java:A#m()",
        kind="method",
        start_line=1,
        end_line=2,
        source_set="MAIN",
    )
    context = EvidenceArtifact.build(
        task_id=TASK_ID, reviewer="threat_model", revision=REV,
        source_kind=EvidenceSourceKind.SYMBOL_CONTEXT,
        tool="resolve_change_context", payload=symbol.model_dump_json(),
        arguments={"symbol_id": symbol.symbol_id},
        availability=ArtifactAvailability.AVAILABLE,
        capture_mode=EvidenceCaptureMode.GENERATED,
        limitations=("symbol_context_truncated",),
    )
    batch = _verify(_candidate(patch.id, context.id), {patch.id: patch, context.id: context})
    verification = batch.candidates["c1"]
    assert verification.grounding_status == "partially_grounded"
    context_items = [
        item for item in verification.valid_evidence
        if item.artifact_id == context.id
    ]
    assert context_items[0].validation_status is EvidenceValidationStatus.LIMITED
    assert "symbol_context_truncated" in context_items[0].limitations


def test_symbol_context_scope_mismatch_is_invalid():
    patch = _patch_artifact()
    symbol = ResolvedSymbol(
        file="src/Other.java",
        symbol_id="java:Other#m()",
        kind="method",
        start_line=1,
        end_line=2,
        source_set="MAIN",
    )
    context = EvidenceArtifact.build(
        task_id=TASK_ID,
        reviewer="threat_model",
        revision=REV,
        source_kind=EvidenceSourceKind.SYMBOL_CONTEXT,
        tool="resolve_change_context",
        arguments={"symbol_id": symbol.symbol_id},
        payload=symbol.model_dump_json(),
        availability=ArtifactAvailability.AVAILABLE,
        capture_mode=EvidenceCaptureMode.GENERATED,
    )

    batch = _verify(
        _candidate(patch.id, context.id),
        {patch.id: patch, context.id: context},
    )

    verification = batch.candidates["c1"]
    assert any(
        item.detail.startswith("invalid_symbol_context:symbol_scope_mismatch")
        for item in verification.invalid_references
    )


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


def test_图响应_legacy_scope_fields_protocol_invalid():
    patch = _patch_artifact()
    legacy_fields = (
        "main_symbols", "test_symbols", "generated_symbols",
        "main_relationships", "test_relationships", "generated_relationships",
    )
    for field in legacy_fields:
        payload = json.loads(_graph_payload())
        payload[field] = []
        graph = _graph_artifact(json.dumps(payload, ensure_ascii=False))
        batch = _verify(
            _candidate(patch.id, graph.id),
            {patch.id: patch, graph.id: graph},
        )
        verification = batch.candidates["c1"]
        assert verification.grounding_status == "partially_grounded"
        assert verification.invalid_references
        assert "graph_legacy_scope_fields" in verification.invalid_references[0].detail


def test_图响应_symbol_scope_mismatch_invalid():
    patch = _patch_artifact()
    payload = json.loads(_graph_payload())
    payload["symbols"][0]["source_set"] = "TEST"
    graph = _graph_artifact(json.dumps(payload, ensure_ascii=False))
    batch = _verify(_candidate(patch.id, graph.id), {patch.id: patch, graph.id: graph})
    verification = batch.candidates["c1"]
    assert verification.grounding_status == "partially_grounded"
    assert verification.invalid_references
    assert "graph_symbol_scope_mismatch" in verification.invalid_references[0].detail


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


def test_图响应_not_found_complete_作为有效范围事实():
    patch = _patch_artifact()
    graph = _graph_artifact(_graph_payload(
        outcome="not_found", coverage="complete", relationships=[]
    ))
    batch = _verify(_candidate(patch.id, graph.id), {patch.id: patch, graph.id: graph})
    graph_items = [
        item for item in batch.candidates["c1"].valid_evidence
        if item.artifact_id == graph.id
    ]
    assert graph_items[0].validation_status is EvidenceValidationStatus.VALID


def test_图响应_illegal_outcome_coverage_combination_invalid():
    patch = _patch_artifact()
    graph = _graph_artifact(_graph_payload(
        outcome="not_found", coverage="partial", relationships=[]
    ))
    batch = _verify(_candidate(patch.id, graph.id), {patch.id: patch, graph.id: graph})
    verification = batch.candidates["c1"]
    assert verification.invalid_references
    assert "invalid_graph_outcome_coverage" in verification.invalid_references[0].detail


# ── 异常重放 ───────────────────────────────────────────────────────────


def test_旧图响应协议直接_invalid_且不重放():
    patch = _patch_artifact()
    graph = _graph_artifact(json.dumps({
        "coverage": "partial",
        "subject_symbol_id": "java:A#m()",
        "source_scope": "MAIN",
        "relationships": [],
    }))
    client = _FakeToolClient(_graph_payload())
    batch = _verify(
        _candidate(patch.id, graph.id),
        {patch.id: patch, graph.id: graph},
        tool_client=client,
    )
    verification = batch.candidates["c1"]
    assert graph.id not in batch.replayed_artifact_ids
    assert verification.invalid_references
    assert "graph_protocol_mismatch" in verification.invalid_references[0].detail
    assert client.calls == 0


def test_indeterminate_不重放_形成_evidence_gap():
    patch = _patch_artifact()
    graph = _graph_artifact(_graph_payload(
        outcome="indeterminate", coverage="partial", relationships=[]
    ))
    client = _FakeToolClient(_graph_payload())
    batch = _verify(
        _candidate(patch.id, graph.id),
        {patch.id: patch, graph.id: graph},
        tool_client=client,
    )
    verification = batch.candidates["c1"]
    assert not any(item.artifact_id == graph.id for item in verification.valid_evidence)
    assert verification.evidence_gaps[0].reason == "graph_indeterminate"
    assert graph.id not in batch.replayed_artifact_ids
    assert client.calls == 0


def test_失败artifact_重放后重新校验为_valid():
    patch = _patch_artifact()
    graph = _graph_artifact(
        _graph_payload(), availability=ArtifactAvailability.FAILED
    )
    batch = _verify(
        _candidate(patch.id, graph.id),
        {patch.id: patch, graph.id: graph},
        tool_client=_FakeToolClient(_graph_payload()),
    )
    verification = batch.candidates["c1"]
    assert len(batch.replayed_artifacts) == 1
    replayed = next(iter(batch.replayed_artifacts.values()))
    graph_items = [
        item
        for item in verification.valid_evidence
        if item.artifact_id == replayed.id
    ]
    assert graph_items[0].validation_status is EvidenceValidationStatus.VALID
    assert "evidence_replay_valid" in [event for event, _ in batch.trace]
    assert replayed.payload == _graph_payload()
    assert replayed.replayed_from_artifact_id == graph.id
    assert replayed.call_id.startswith("evidence-replay-")
    replay_trace = next(
        json.loads(detail)
        for event, detail in batch.trace
        if event == "evidence_replay_valid"
    )
    assert replay_trace["artifact_id"] == replayed.id
    assert replay_trace["replayed_from_artifact_id"] == graph.id


def test_失败artifact_白名单空_形成_gap():
    patch = _patch_artifact()
    graph = _graph_artifact(
        _graph_payload(), availability=ArtifactAvailability.FAILED
    )
    batch = _verify(
        _candidate(patch.id, graph.id),
        {patch.id: patch, graph.id: graph},
        tool_client=_FakeToolClient(_graph_payload()),
        enabled_replay_tools=[],
    )
    gap = batch.candidates["c1"].evidence_gaps[0]
    assert "replay_not_enabled" in gap.limitations


def test_重放失败_形成_gap_不作为反证():
    patch = _patch_artifact()
    graph = _graph_artifact(
        _graph_payload(), availability=ArtifactAvailability.FAILED
    )
    batch = _verify(
        _candidate(patch.id, graph.id),
        {patch.id: patch, graph.id: graph},
        tool_client=_FakeToolClient("", success=False),
    )
    verification = batch.candidates["c1"]
    assert verification.evidence_gaps
    assert any("replay" in lim for lim in verification.evidence_gaps[0].limitations)
    assert len(batch.replayed_artifacts) == 1
    replayed = next(iter(batch.replayed_artifacts.values()))
    assert replayed.availability is ArtifactAvailability.FAILED
    assert verification.evidence_gaps[0].artifact_id == replayed.id


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
    graph = _graph_artifact(
        _graph_payload(), availability=ArtifactAvailability.FAILED
    )
    candidate_b = _candidate(patch.id, graph.id).model_copy(update={"id": "c2"})
    dossier_b = _dossier(candidate_b)
    client = _FakeToolClient(_graph_payload())
    batch = verify_evidence(
        [_dossier(_candidate(patch.id, graph.id)), dossier_b],
        artifacts={patch.id: patch, graph.id: graph},
        tool_client=client,
        revision=REV,
        enabled_replay_tools=None,
    )
    assert batch.replayed_artifact_ids == [graph.id]
    assert client.calls == 1


def test_重放后仍_indeterminate_形成_gap_不升级():
    patch = _patch_artifact()
    graph = _graph_artifact(
        _graph_payload(), availability=ArtifactAvailability.FAILED
    )
    replay_payload = _graph_payload(
        outcome="indeterminate", coverage="partial", relationships=[]
    )
    client = _FakeToolClient(replay_payload)
    batch = _verify(
        _candidate(patch.id, graph.id),
        {patch.id: patch, graph.id: graph},
        tool_client=client,
    )
    verification = batch.candidates["c1"]
    assert verification.evidence_gaps[0].reason == "graph_indeterminate"
    assert not any(item.artifact_id == graph.id for item in verification.valid_evidence)
    assert "evidence_replay_unavailable" in [event for event, _ in batch.trace]


# ── guard 扫描与引用范围 ───────────────────────────────────────────────


def _guard_context(annotation: str = "PreAuthorize") -> TaskSymbolContext:
    return TaskSymbolContext(
        task_id=TASK_ID,
        status=SymbolResolutionStatus.RESOLVED,
        symbols=(
            ResolvedSymbol(
                file="src/A.java",
                symbol_id="java:A#m()",
                kind="method",
                start_line=1,
                end_line=2,
                signature="public void m()",
                annotations=(annotation,),
                source_set="MAIN",
            ),
        ),
    )


def _verify_with_bundle(candidate: CandidateIssue, annotation: str):
    patch = _patch_artifact()
    batch = verify_evidence(
        [_dossier(candidate, context=_guard_context(annotation))],
        artifacts={patch.id: patch},
        tool_client=None,
        revision=REV,
        enabled_replay_tools=None,
    )
    return batch.candidates[candidate.id]


def test_guard_threat候选_授权注解命中_直接反证_不可裁决():
    patch = _patch_artifact()
    verification = _verify_with_bundle(_candidate(patch.id), "PreAuthorize")
    assert verification.eligible_for_judge is False
    assert verification.rejection_reason == "direct_counter_guard"


def test_guard_behavior候选_事务注解命中_直接反证():
    candidate = _candidate(_patch_artifact().id).model_copy(
        update={"source_agent": "behavior"}
    )
    verification = _verify_with_bundle(candidate, "Transactional")
    assert verification.eligible_for_judge is False
    assert verification.rejection_reason == "direct_counter_guard"


def test_guard_behavior候选_授权注解不扫描():
    # guard 过滤按发现者分工:授权注解只对 threat_model 候选反证。
    candidate = _candidate(_patch_artifact().id).model_copy(
        update={"source_agent": "behavior"}
    )
    verification = _verify_with_bundle(candidate, "PreAuthorize")
    assert verification.eligible_for_judge is True


def test_guard_maintainability候选_不扫描():
    candidate = _candidate(_patch_artifact().id).model_copy(
        update={"source_agent": "maintainability"}
    )
    verification = _verify_with_bundle(candidate, "PreAuthorize")
    assert verification.eligible_for_judge is True


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
