"""Evidence Ledger 证据目录(P/C/T Artifact 注册)的工程正确性测试。

覆盖:patch/上下文/工具结果的目录注册、短别名分配、reused 解析到首次
真实 payload、目录经引擎与用户提示词贯通(源文档 §5.2-§5.6)。
"""

from __future__ import annotations

from codeguard_agent.models.council import ContextFact
from codeguard_agent.models.evidence import (
    EvidenceArtifactStatus,
    EvidenceCaptureMode,
    EvidenceSourceKind,
)
from codeguard_agent.models.schemas import EvidenceRole, ReviewResult, Severity
from codeguard_agent.models.tasks import ReviewTask, TaskContextBundle
from codeguard_agent.pipeline.engines import (
    DirectEngine,
    ToolAgentEngine,
    _gathered_context_from_records,
)
from codeguard_agent.pipeline.evidence.ledger import EvidenceCatalogBuilder
from codeguard_agent.pipeline.reviewers.reviewers import build_reviewer_user_prompt
from codeguard_agent.pipeline.risk.discovery import (
    COMPLETE_PATCH_RESULT,
    REPEATED_TOOL_RESULT,
    CoordinatedDiscoveryToolClient,
    DiscoveryToolCoordinator,
    DiscoveryToolRecord,
)
from codeguard_agent.tools.tool_client import ToolResponse

REV = "abc123:deadbeef"


def _task() -> ReviewTask:
    return ReviewTask(id="task-1", file="src/A.java", patch="+    exec(cmd);\n")


def _bundle(*facts: ContextFact, truncated: bool = False) -> TaskContextBundle:
    return TaskContextBundle(task_id="task-1", facts=list(facts), truncated=truncated)


def _fact(source: str, content: str, truncated: bool = False) -> ContextFact:
    return ContextFact(source=source, kind="symbol_context", content=content, truncated=truncated)


def _record(
    *,
    tool: str = "get_file_content",
    arguments: dict | None = None,
    output: str = "REAL CONTENT",
    status: str = "complete",
    call_id: str = "call-1",
    resolved_output: str = "",
) -> DiscoveryToolRecord:
    return DiscoveryToolRecord(
        call_id=call_id,
        tool=tool,
        arguments=arguments or {"file_path": "src/A.java"},
        output=output,
        duration_ms=1.0,
        status=status,
        reuse_key="get_file_content:{}",
        resolved_output=resolved_output,
    )


class _Builder:
    def __init__(self):
        self._builder = EvidenceCatalogBuilder()

    def build(self, bundle=None):
        return self._builder.build_initial(
            task=_task(), context_bundle=bundle, reviewer="threat_model", revision=REV
        )

    def append(self, catalog, records):
        return self._builder.append_tool_records(catalog, records)


# ── build_initial:P01/Cxx ──────────────────────────────────────────────


def test_初始目录_注册_patch_为_P01():
    catalog = _Builder().build()
    assert catalog.patch_alias() == "P01"
    patch_artifact = catalog.artifacts[catalog.alias_to_artifact_id["P01"]]
    assert patch_artifact.source_kind is EvidenceSourceKind.TASK_PATCH
    assert patch_artifact.payload == "+    exec(cmd);\n"
    assert patch_artifact.arguments == {"file_path": "src/A.java"}
    assert patch_artifact.capture_mode is EvidenceCaptureMode.GENERATED
    assert patch_artifact.status is EvidenceArtifactStatus.COMPLETE


def test_初始目录_逐条注册上下文为_Cxx():
    bundle = _bundle(
        _fact("resolve_change_context", "symbol A"),
        _fact("resolve_change_context", "symbol B"),
    )
    catalog = _Builder().build(bundle)
    aliases = catalog.context_aliases()
    assert aliases == ["C01", "C02"]
    first = catalog.artifacts[catalog.alias_to_artifact_id["C01"]]
    assert first.source_kind is EvidenceSourceKind.PREFETCHED_CONTEXT
    assert first.tool == "resolve_change_context"
    assert first.payload == "symbol A"
    assert first.status is EvidenceArtifactStatus.COMPLETE


def test_初始目录_空_bundle_仅_patch():
    catalog = _Builder().build(None)
    assert catalog.context_aliases() == []
    assert len(catalog.artifacts) == 1


def test_初始目录_截断事实标_partial_并带限制声明():
    bundle = _bundle(_fact("resolve_change_context", "symbol A", truncated=True))
    catalog = _Builder().build(bundle)
    art = catalog.artifacts[catalog.alias_to_artifact_id["C01"]]
    assert art.status is EvidenceArtifactStatus.PARTIAL
    assert art.limitations == ("context_truncated",)


# ── append_tool_records:Txx ────────────────────────────────────────────


def test_追加工具记录_按首次出现顺序生成_Txx():
    catalog = _Builder().build()
    records = [
        _record(tool="inspect_change_impact", arguments={"symbol_id": "A"}, output="edges A"),
        _record(tool="get_file_content", output="code B"),
    ]
    catalog = _Builder().append(catalog, records)
    assert catalog.tool_aliases() == ["T01", "T02"]
    t1 = catalog.artifacts[catalog.alias_to_artifact_id["T01"]]
    assert t1.source_kind is EvidenceSourceKind.TOOL_CALL
    assert t1.tool == "inspect_change_impact"
    assert t1.payload == "edges A"
    assert t1.capture_mode is EvidenceCaptureMode.EXECUTED
    assert t1.call_id == "call-1"


def test_追加工具记录_短标记复用_与_complete_patch_不建新artifact():
    catalog = _Builder().build()
    records = [
        _record(call_id="call-1"),
        _record(
            call_id="call-2", output=REPEATED_TOOL_RESULT, status="reused",
            resolved_output="REAL CONTENT",
        ),
        _record(call_id="call-3", output=COMPLETE_PATCH_RESULT, status="reused"),
        _record(call_id="call-4", arguments={"file_path": "src/B.java"}),
    ]
    catalog = _Builder().append(catalog, records)
    assert catalog.tool_aliases() == ["T01", "T02"]
    assert [a.payload for a in catalog.artifacts.values() if a.source_kind is EvidenceSourceKind.TOOL_CALL] == [
        "REAL CONTENT", "REAL CONTENT",
    ]
    assert "call-3" not in {a.call_id for a in catalog.artifacts.values()}


def test_追加工具记录_跨任务复用_注册为_REUSED_artifact():
    catalog = _Builder().build()
    record = _record(
        call_id="call-b",
        output="REAL CONTENT",
        status="reused",
        resolved_output="REAL CONTENT",
    )
    catalog = _Builder().append(catalog, [record])
    assert catalog.tool_aliases() == ["T01"]
    artifact = catalog.artifacts[catalog.alias_to_artifact_id["T01"]]
    assert artifact.capture_mode is EvidenceCaptureMode.REUSED
    assert artifact.status is EvidenceArtifactStatus.COMPLETE
    assert artifact.reused_from_artifact_id == ""


def test_追加工具记录_失败状态映射():
    catalog = _Builder().build()
    records = [
        _record(call_id="f1", output="boom", status="failed"),
        _record(call_id="f2", output="rejected", status="rejected"),
        _record(call_id="f3", output="not found", status="not_found"),
    ]
    catalog = _Builder().append(catalog, records)
    statuses = [catalog.artifacts[catalog.alias_to_artifact_id[f"T{i:02d}"]].status for i in (1, 2, 3)]
    assert statuses == [
        EvidenceArtifactStatus.FAILED,
        EvidenceArtifactStatus.REJECTED,
        EvidenceArtifactStatus.NOT_FOUND,
    ]


# ── reused 解析到首次真实 payload ──────────────────────────────────────


class _FakeDelegate:
    def __init__(self):
        self.calls = 0

    def get_file_content(self, _path):
        self.calls += 1
        return ToolResponse(success=True, result="REAL CONTENT")


def test_共享协调器_跨任务复用_返回真实内容且记录指向首次调用():
    coordinator = DiscoveryToolCoordinator()
    client_a = CoordinatedDiscoveryToolClient(_FakeDelegate(), coordinator)
    client_b = CoordinatedDiscoveryToolClient(_FakeDelegate(), coordinator)
    resp_a = client_a.get_file_content("src/A.java")
    resp_b = client_b.get_file_content("src/A.java")
    # 首发回显编号;跨任务复用:LLM 看到真实内容(协调器缓存命中),不是短标记。
    assert resp_a.result == "REAL CONTENT\n\n[证据编号 T01]"
    assert resp_b.result == "REAL CONTENT"
    record_a = client_a.trace_records[-1]
    record_b = client_b.trace_records[-1]
    assert record_a.status == "complete"
    assert record_b.status == "reused"
    assert record_b.resolved_output == "REAL CONTENT"
    assert record_b.reused_from_call_id == record_a.call_id


def test_同一客户端二次调用_返回短标记_但_record_解析真实payload():
    coordinator = DiscoveryToolCoordinator()
    client = CoordinatedDiscoveryToolClient(_FakeDelegate(), coordinator)
    first = client.get_file_content("src/A.java")
    second = client.get_file_content("src/A.java")
    assert first.result == "REAL CONTENT\n\n[证据编号 T01]"
    assert second.result == REPEATED_TOOL_RESULT
    record = client.trace_records[-1]
    assert record.status == "reused"
    assert record.resolved_output == "REAL CONTENT"


def test_complete_patch_短标记记录_解析目标为_patch():
    client = CoordinatedDiscoveryToolClient(
        _FakeDelegate(),
        DiscoveryToolCoordinator(),
        complete_patch_files={"src/New.java"},
    )
    resp = client.get_file_content("src/New.java")
    assert resp.result == COMPLETE_PATCH_RESULT + "\n\n[证据编号 P01]"
    record = client.trace_records[-1]
    assert record.reused_from_call_id == "task_patch"
    assert record.resolved_output == ""


def test_gathered_context_复用记录携带真实payload():
    record = _record(output=REPEATED_TOOL_RESULT, status="reused", resolved_output="REAL CONTENT")
    gathered = _gathered_context_from_records([record])
    assert [g.content for g in gathered] == ["REAL CONTENT"]


# ── 目录经提示词与引擎贯通 ─────────────────────────────────────────────


def test_用户提示词_带目录时渲染_evidence_id():
    bundle = _bundle(_fact("resolve_change_context", "symbol A"))
    catalog = _Builder().build(bundle)
    prompt = build_reviewer_user_prompt(task=_task(), context_bundle=bundle, catalog=catalog)
    assert 'evidence_id="P01"' in prompt
    assert 'evidence_id="C01"' in prompt


def test_绑定器_role为枚举成员时不抛():
    # str-Enum 成员在 3.11+ 下 str() 返回限定名,绑定器必须取 .value(回归)。
    from codeguard_agent.models.schemas import (
        DiscoveredIssue,
        EvidenceRefSelection,
    )
    from codeguard_agent.pipeline.evidence.ledger import bind_discovered_issue

    catalog = _Builder().build(_bundle(_fact("resolve_change_context", "symbol A")))
    issue = DiscoveredIssue(
        severity=Severity.WARNING,
        file="src/A.java",
        line=1,
        type="t",
        message="m",
        evidence_refs=[
            EvidenceRefSelection(alias="C01", role=EvidenceRole.REACHABILITY)
        ],
    )
    candidate = bind_discovered_issue(
        issue, task=_task(), reviewer="threat_model",
        catalog=catalog, candidate_index=1,
    )
    assert candidate.evidence_ref_errors == []
    assert [ref.declared_role for ref in candidate.evidence_refs] == [
        EvidenceRole.MECHANISM,  # 自动 patch
        EvidenceRole.REACHABILITY,
    ]


def test_用户提示词_无目录时不含_evidence_id():
    prompt = build_reviewer_user_prompt(task=_task())
    assert "evidence_id=" not in prompt


class _FakeStructured:
    def __init__(self, result):
        self._result = result

    def invoke(self, _messages):
        return self._result


class _FakeLLM:
    def __init__(self, result):
        self._result = result

    def with_structured_output(self, _schema, method=None):
        return _FakeStructured(self._result)


class _SuccessfulAgentEngine(ToolAgentEngine):
    def _run_agent(self, llm, system_prompt, user_prompt):  # noqa: ARG002
        return {"messages": []}


def test_直连引擎_透传目录():
    catalog = _Builder().build()
    outcome = DirectEngine().review(
        _FakeLLM(ReviewResult(summary="x")),
        system_prompt="s",
        user_prompt="u",
        reviewer_name="logic",
        max_retries=1,
        structured_method="function_calling",
        evidence_catalog=catalog,
    )
    assert outcome.evidence_catalog is catalog


def test_react_正常路径_目录追加工具记录():
    records = [_record(tool="inspect_change_impact", arguments={"symbol_id": "A"}, output="edges A")]
    client = type("Client", (), {"trace_records": records})()
    engine = _SuccessfulAgentEngine(tool_client=client)
    catalog = _Builder().build()
    outcome = engine.review(
        _FakeLLM(ReviewResult(summary="x")),
        system_prompt="s",
        user_prompt="u",
        reviewer_name="logic",
        max_retries=1,
        structured_method="function_calling",
        evidence_catalog=catalog,
    )
    assert outcome.evidence_catalog is not None
    assert outcome.evidence_catalog.tool_aliases() == ["T01"]
    t1 = outcome.evidence_catalog.artifacts[outcome.evidence_catalog.alias_to_artifact_id["T01"]]
    assert t1.payload == "edges A"
    assert outcome.evidence_catalog.patch_alias() == "P01"


def test_react_无目录输入时_结果目录为空():
    records = [_record()]
    client = type("Client", (), {"trace_records": records})()
    engine = _SuccessfulAgentEngine(tool_client=client)
    outcome = engine.review(
        _FakeLLM(ReviewResult(summary="x")),
        system_prompt="s",
        user_prompt="u",
        reviewer_name="logic",
        max_retries=1,
        structured_method="function_calling",
    )
    assert outcome.evidence_catalog is None
