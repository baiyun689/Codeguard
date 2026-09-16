"""验证 patch、上下文和工具证据的注册、短别名分配、复用解析及提示词传递。"""

from __future__ import annotations
import json
from codeguard_agent.models.evidence import (
    ArtifactAvailability,
    EvidenceCaptureMode,
    EvidenceSourceKind,
)
from codeguard_agent.models.schemas import EvidenceRole, ReviewResult
from codeguard_agent.models.tasks import (
    ResolvedSymbol,
    ReviewTask,
    SymbolResolutionStatus,
    TaskSymbolContext,
)
from codeguard_agent.pipeline.execution.engines import DirectEngine
from codeguard_agent.pipeline.evidence.ledger import (
    EvidenceCatalogBuilder,
    capture_tool_records,
    render_evidence_catalog,
)
from codeguard_agent.pipeline.execution.discovery import (
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


def _bundle(*symbols: ResolvedSymbol, truncated: bool = False) -> TaskSymbolContext:
    return TaskSymbolContext(
        task_id="task-1",
        symbols=tuple(symbols),
        status=SymbolResolutionStatus.RESOLVED,
        truncated=truncated,
    )


def _fact(source: str, content: str, truncated: bool = False) -> ResolvedSymbol:
    del source, truncated
    return ResolvedSymbol(
        file="src/A.java",
        symbol_id=content,
        kind="method",
        start_line=1,
        end_line=2,
        source_set="MAIN",
    )


def _record(
    *,
    tool: str = "read_symbol",
    arguments: dict | None = None,
    output: str = "REAL CONTENT",
    status: str = "complete",
    call_id: str = "call-1",
    resolved_output: str = "",
    reused_from_call_id: str = "",
) -> DiscoveryToolRecord:
    return DiscoveryToolRecord(
        call_id=call_id,
        tool=tool,
        arguments=arguments or {"symbol_id": "java:A#run()"},
        output=output,
        duration_ms=1.0,
        status=status,
        reuse_key="read_symbol:{}",
        reused_from_call_id=reused_from_call_id,
        resolved_output=resolved_output,
    )


class _Builder:
    def __init__(self):
        self._builder = EvidenceCatalogBuilder()

    def build(self, bundle=None):
        return self._builder.build_initial(
            task=_task(), symbol_context=bundle, reviewer="threat_model", revision=REV
        )

    def append(self, catalog, records):
        return self._builder.append_tool_records(catalog, records)


def test_初始目录_注册_patch_为_P01():
    catalog = _Builder().build()
    assert catalog.patch_alias() == "P01"
    patch_artifact = catalog.artifacts[catalog.alias_to_artifact_id["P01"]]
    assert patch_artifact.source_kind is EvidenceSourceKind.TASK_PATCH
    assert patch_artifact.payload == "+    exec(cmd);\n"
    assert patch_artifact.arguments == {"file_path": "src/A.java"}
    assert patch_artifact.capture_mode is EvidenceCaptureMode.GENERATED
    assert patch_artifact.availability is ArtifactAvailability.AVAILABLE


def test_初始目录_逐条注册上下文为_Cxx():
    bundle = _bundle(
        _fact("resolve_change_context", "symbol A"),
        _fact("resolve_change_context", "symbol B"),
    )
    catalog = _Builder().build(bundle)
    aliases = catalog.symbol_aliases()
    assert aliases == ["C01", "C02"]
    first = catalog.artifacts[catalog.alias_to_artifact_id["C01"]]
    assert first.source_kind is EvidenceSourceKind.SYMBOL_CONTEXT
    assert first.tool == "resolve_change_context"
    assert '"symbol_id":"symbol A"' in first.payload
    assert first.availability is ArtifactAvailability.AVAILABLE


def test_初始目录_空_bundle_仅_patch():
    catalog = _Builder().build(None)
    assert catalog.symbol_aliases() == []
    assert len(catalog.artifacts) == 1


def test_初始目录_截断事实保持可用并带限制声明():
    bundle = _bundle(
        _fact("resolve_change_context", "symbol A"), truncated=True
    ).model_copy(update={"limitations": ("symbol_context_truncated",)})
    catalog = _Builder().build(bundle)
    art = catalog.artifacts[catalog.alias_to_artifact_id["C01"]]
    assert art.availability is ArtifactAvailability.AVAILABLE
    assert art.limitations == ("symbol_context_truncated",)


def test_追加工具记录_按首次出现顺序生成_Txx():
    catalog = _Builder().build()
    records = [
        _record(
            tool="query_relations",
            arguments={"subject_symbol_id": "A", "relation": "callees"},
            output="edges A",
        ),
        _record(tool="read_symbol", output="code B"),
    ]
    catalog = _Builder().append(catalog, records)
    assert catalog.tool_aliases() == ["T01", "T02"]
    t1 = catalog.artifacts[catalog.alias_to_artifact_id["T01"]]
    assert t1.source_kind is EvidenceSourceKind.TOOL_CALL
    assert t1.tool == "query_relations"
    assert t1.payload == "edges A"
    assert t1.capture_mode is EvidenceCaptureMode.EXECUTED
    assert t1.call_id == "call-1"


def test_追加工具记录_短标记复用_与_complete_patch_不建新artifact():
    catalog = _Builder().build()
    records = [
        _record(call_id="call-1"),
        _record(
            call_id="call-2",
            output=REPEATED_TOOL_RESULT,
            status="reused",
            resolved_output="REAL CONTENT",
        ),
        _record(call_id="call-3", output=COMPLETE_PATCH_RESULT, status="reused"),
        _record(call_id="call-4", arguments={"file_path": "src/B.java"}),
    ]
    catalog = _Builder().append(catalog, records)
    assert catalog.tool_aliases() == ["T01", "T02"]
    assert [
        a.payload
        for a in catalog.artifacts.values()
        if a.source_kind is EvidenceSourceKind.TOOL_CALL
    ] == ["REAL CONTENT", "REAL CONTENT"]
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
    assert artifact.availability is ArtifactAvailability.AVAILABLE
    assert artifact.reused_from_artifact_id == ""


def test_追加工具记录_失败状态映射():
    catalog = _Builder().build()
    records = [
        _record(call_id="f1", output="boom", status="failed"),
        _record(call_id="f2", output="rejected", status="rejected"),
        _record(call_id="f3", output="not found", status="not_found"),
    ]
    catalog = _Builder().append(catalog, records)
    statuses = [
        catalog.artifacts[catalog.alias_to_artifact_id[f"T{i:02d}"]].availability
        for i in (1, 2, 3)
    ]
    assert statuses == [
        ArtifactAvailability.FAILED,
        ArtifactAvailability.REJECTED,
        ArtifactAvailability.MISSING,
    ]


def test_追加工具记录_无法识别状态映射为_invalid():
    catalog = _Builder().append(
        _Builder().build(), [_record(call_id="invalid", status="unexpected")]
    )
    artifact = catalog.artifacts[catalog.alias_to_artifact_id["T01"]]
    assert artifact.availability is ArtifactAvailability.INVALID


def test_capture_tool_records_只向_state_暴露引用不暴露原始输出():
    catalog = _Builder().build()
    records = [
        _record(call_id="call-1", output="SECRET RAW PAYLOAD"),
        _record(
            call_id="call-2",
            output=REPEATED_TOOL_RESULT,
            status="reused",
            resolved_output="SECRET RAW PAYLOAD",
            reused_from_call_id="call-1",
        ),
        _record(
            call_id="call-3",
            output=COMPLETE_PATCH_RESULT,
            status="reused",
            reused_from_call_id="task_patch",
        ),
    ]
    batch = capture_tool_records(catalog, records)
    assert len(batch.trace_refs) == 3
    assert batch.trace_refs[0].artifact_id
    assert batch.trace_refs[1].artifact_id == batch.trace_refs[0].artifact_id
    assert (
        batch.trace_refs[1].reused_from_artifact_id == batch.trace_refs[0].artifact_id
    )
    assert batch.trace_refs[2].artifact_id == catalog.alias_to_artifact_id["P01"]
    assert "output" not in batch.trace_refs[0].model_dump()
    artifact = batch.catalog.artifacts[batch.trace_refs[0].artifact_id]
    assert artifact.payload == "SECRET RAW PAYLOAD"


class _FakeDelegate:
    def __init__(self):
        self.calls = 0

    def read_symbol(self, _path, **kwargs):
        self.calls += 1
        return ToolResponse(success=True, result="REAL CONTENT")

    def query_relations(self, _symbol_id):
        self.calls += 1
        return ToolResponse(
            success=True,
            result=json.dumps(
                {
                    "schema_version": 2,
                    "outcome": "found",
                    "coverage": "complete",
                    "source_scope": "MAIN",
                    "subject_symbol_id": "java:A#m()",
                    "symbols": [
                        {"id": "java:A#m()", "kind": "method", "source_set": "MAIN"}
                    ],
                    "relationships": [
                        {
                            "sourceId": "java:B#call()",
                            "targetId": "java:A#m()",
                            "kind": "calls",
                            "file": "B.java",
                            "line": 1,
                            "source_set": "MAIN",
                            "resolution": "RESOLVED",
                            "verbose_diagnostic": "must stay out of reviewer context",
                        }
                    ],
                    "unresolved_relationships": [],
                    "unresolved_count": 0,
                    "limitations": [],
                    "snapshot_main_coverage": "partial",
                },
                ensure_ascii=False,
            ),
        )


def test_共享协调器_跨任务复用_返回真实内容且记录指向首次调用():
    coordinator = DiscoveryToolCoordinator()
    client_a = CoordinatedDiscoveryToolClient(_FakeDelegate(), coordinator)
    client_b = CoordinatedDiscoveryToolClient(_FakeDelegate(), coordinator)
    resp_a = client_a.read_symbol("java:A#run()")
    resp_b = client_b.read_symbol("java:A#run()")
    assert resp_a.result == "REAL CONTENT\n\n[证据编号 T01]"
    assert resp_b.result == "REAL CONTENT\n\n[证据编号 T01]"
    record_a = client_a.trace_records[-1]
    record_b = client_b.trace_records[-1]
    assert record_a.status == "complete"
    assert record_b.status == "reused"
    assert record_b.resolved_output == "REAL CONTENT"
    assert record_b.reused_from_call_id == record_a.call_id


def test_同一客户端二次调用_返回短标记_但_record_解析真实payload():
    coordinator = DiscoveryToolCoordinator()
    client = CoordinatedDiscoveryToolClient(_FakeDelegate(), coordinator)
    first = client.read_symbol("java:A#run()")
    second = client.read_symbol("java:A#run()")
    assert first.result == "REAL CONTENT\n\n[证据编号 T01]"
    assert second.result == REPEATED_TOOL_RESULT
    record = client.trace_records[-1]
    assert record.status == "reused"
    assert record.resolved_output == "REAL CONTENT"


def test_complete_patch_短标记记录_解析目标为_patch():
    client = CoordinatedDiscoveryToolClient(
        _FakeDelegate(),
        DiscoveryToolCoordinator(),
        complete_patch_symbol_ids={"java:New#m()"},
    )
    resp = client.read_symbol("java:New#m()")
    assert resp.result == COMPLETE_PATCH_RESULT
    assert "P01" not in (resp.result or "")
    record = client.trace_records[-1]
    assert record.reused_from_call_id == "task_patch"
    assert record.resolved_output == ""


def test_llm_catalog_hides_internal_patch_alias():
    catalog = _Builder().build(_bundle(_fact("resolve_change_context", "symbol A")))
    rendered = render_evidence_catalog(catalog)
    assert "P01" not in rendered
    assert 'id="C01"' in rendered


def test_绑定器_role为枚举成员时不抛():
    from codeguard_agent.models.schemas import DiscoveredIssue, EvidenceRefSelection
    from codeguard_agent.pipeline.evidence.ledger import bind_discovered_issue

    catalog = _Builder().build(_bundle(_fact("resolve_change_context", "symbol A")))
    issue = DiscoveredIssue(
        file="src/A.java",
        line=1,
        type="t",
        message="m",
        evidence_refs=[
            EvidenceRefSelection(alias="C01", role=EvidenceRole.REACHABILITY)
        ],
    )
    candidate = bind_discovered_issue(
        issue, task=_task(), reviewer="threat_model", catalog=catalog, candidate_index=1
    )
    assert candidate.evidence_ref_errors == []
    assert [ref.declared_role for ref in candidate.evidence_refs] == [
        EvidenceRole.MECHANISM,
        EvidenceRole.REACHABILITY,
    ]


def test_绑定器保留失败工具引用供_verifier_重放():
    from codeguard_agent.models.schemas import DiscoveredIssue, EvidenceRefSelection
    from codeguard_agent.pipeline.evidence.ledger import bind_discovered_issue

    catalog = _Builder().append(
        _Builder().build(),
        [_record(call_id="failed", output="HTTP 503", status="failed")],
    )
    issue = DiscoveredIssue(
        file="src/A.java",
        line=1,
        type="t",
        message="m",
        evidence_refs=[
            EvidenceRefSelection(alias="T01", role=EvidenceRole.REACHABILITY)
        ],
    )
    candidate = bind_discovered_issue(
        issue, task=_task(), reviewer="threat_model", catalog=catalog, candidate_index=1
    )
    assert candidate.evidence_ref_errors == []
    assert (
        candidate.evidence_refs[-1].artifact_id == catalog.alias_to_artifact_id["T01"]
    )


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
