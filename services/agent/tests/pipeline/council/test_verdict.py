"""批量 EvidenceJudge 与消融档裁决测试(Evidence Ledger)。"""

from __future__ import annotations
import json
from types import SimpleNamespace
import pytest
from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.evidence import (
    ArtifactAvailability,
    CandidateVerification,
    EvidenceArtifact,
    EvidenceCaptureMode,
    EvidenceGap,
    EvidenceJudgeAssessment,
    EvidenceJudgeBatch,
    EvidenceRef,
    EvidenceSourceKind,
    EvidenceValidationStatus,
    VerifiedEvidence,
)
from codeguard_agent.models.schemas import EvidenceRole, Severity
from codeguard_agent.models.tasks import ReviewTask
from codeguard_agent.pipeline.council.verdict import (
    _bounded_graph_path_facts,
    judge_direct,
    judge_with_evidence,
)
from codeguard_agent.pipeline.evidence.planner import CandidateDossier, DossierAssembly

REV = "abc123:deadbeef"
TASK_ID = "task-1"


def _task() -> ReviewTask:
    return ReviewTask(
        id=TASK_ID, file="src/A.java", patch="+    exec(cmd);\n", changed_lines=[1]
    )


def _candidate(
    cid: str,
    source_agent: str = "threat_model",
    role: EvidenceRole = EvidenceRole.MECHANISM,
) -> CandidateIssue:
    return CandidateIssue(
        id=cid,
        task_id=TASK_ID,
        source_agent=source_agent,
        file="src/A.java",
        line=1,
        type="command-injection",
        claim="未转义参数进入命令构造",
        confidence=0.8,
        evidence_refs=[
            EvidenceRef(
                artifact_id="ev-patch",
                declared_role=EvidenceRole.MECHANISM,
                auto_bound=True,
            ),
            EvidenceRef(artifact_id="ev-tool", declared_role=role),
        ],
    )


def _patch_artifact() -> EvidenceArtifact:
    return EvidenceArtifact.build(
        task_id=TASK_ID,
        reviewer="threat_model",
        revision=REV,
        source_kind=EvidenceSourceKind.TASK_PATCH,
        payload="+    exec(cmd);\n",
        availability=ArtifactAvailability.AVAILABLE,
        capture_mode=EvidenceCaptureMode.GENERATED,
        arguments={"symbol_id": "java:A#run()"},
    )


def _tool_artifact() -> EvidenceArtifact:
    return EvidenceArtifact.build(
        task_id=TASK_ID,
        reviewer="threat_model",
        revision=REV,
        source_kind=EvidenceSourceKind.TOOL_CALL,
        tool="read_symbol",
        arguments={"symbol_id": "java:A#run()"},
        payload="class A { void m() { exec(cmd); } }",
        availability=ArtifactAvailability.AVAILABLE,
        capture_mode=EvidenceCaptureMode.EXECUTED,
    )


def _verification(cid: str, eligible: bool = True) -> CandidateVerification:
    return CandidateVerification(
        candidate_id=cid,
        source_kinds={EvidenceSourceKind.TASK_PATCH, EvidenceSourceKind.TOOL_CALL},
        valid_evidence=[
            VerifiedEvidence(
                artifact_id="ev-patch",
                source_kind=EvidenceSourceKind.TASK_PATCH,
                content="+    exec(cmd);\n",
                validation_status=EvidenceValidationStatus.VALID,
            ),
            VerifiedEvidence(
                artifact_id="ev-tool",
                source_kind=EvidenceSourceKind.TOOL_CALL,
                tool="read_symbol",
                arguments={"symbol_id": "java:A#run()"},
                content="class A { void m() { exec(cmd); } }",
                validation_status=EvidenceValidationStatus.VALID,
            ),
        ],
        grounding_status="grounded",
        eligible_for_judge=eligible,
    )


def _assembly(candidates: list[CandidateIssue]) -> DossierAssembly:
    dossiers = [
        CandidateDossier(candidate=candidate, task=_task(), symbol_context=None)
        for candidate in candidates
    ]
    return DossierAssembly(tuple(dossiers), (), ())


def _artifacts() -> dict[str, EvidenceArtifact]:
    patch = _patch_artifact()
    tool_artifact = _tool_artifact()
    return {"ev-patch": patch, "ev-tool": tool_artifact}


def test_judge_path_facts_are_only_a_bounded_rendering_of_projected_edges():
    payload = {
        "schema_version": 2,
        "subject_symbol_id": "s1",
        "relationships": [
            {"sourceId": "s1", "targetId": "s2", "kind": "CALLS"},
            {"sourceId": "s2", "targetId": "s3", "kind": "CALLS"},
            {"sourceId": "s3", "targetId": "s4", "kind": "WRITES"},
        ],
    }
    paths = _bounded_graph_path_facts(
        json.dumps(payload), arguments={"symbol_id": "s1"}
    )
    assert paths == [
        {"symbols": ["s1", "s2", "s3"], "relationships": ["CALLS", "CALLS"]}
    ]


class _FakeJudgeLLM:
    """按输入候选数分派的伪 Judge LLM:批>1 返回 None(触发二分),单候选返回裁决。"""

    def __init__(self, result):
        self._result = result
        self.calls = 0
        self.payloads = []

    def with_structured_output(self, _schema, method=None, include_raw=False):
        return self

    def invoke(self, messages):
        self.calls += 1
        user = messages[1][1]
        payload = json.loads(user)
        self.payloads.append(payload)
        candidates = payload.get("candidates") if isinstance(payload, dict) else None
        count = len(candidates) if isinstance(candidates, list) else 1
        if count > 1:
            return None
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _ContractRetryJudgeLLM:
    """First response omits one candidate; the bounded retry returns it."""

    def __init__(self, first_id: str, second_id: str):
        self.first_id = first_id
        self.second_id = second_id
        self.calls = 0

    def with_structured_output(self, _schema, method=None, include_raw=False):
        return self

    def invoke(self, messages):
        self.calls += 1
        payload = json.loads(messages[1][1])
        ids = [item["candidate_id"] for item in payload["candidates"]]
        if ids == [self.first_id, self.second_id]:
            return EvidenceJudgeBatch(assessments=[_assessment(self.first_id)])
        return EvidenceJudgeBatch(assessments=[_assessment(self.second_id)])


def _assessment(
    cid: str,
    action: str = "keep",
    severity: Severity | None = Severity.WARNING,
    evidence: list[str] | None = None,
) -> EvidenceJudgeAssessment:
    return EvidenceJudgeAssessment(
        candidate_id=cid,
        action=action,
        severity=severity,
        evidence_ids=evidence if evidence is not None else ["F001", "F002"],
        reason="patch 与文件事实均支持",
    )


def _raw_judge(arguments, *, name="EvidenceJudgeBatch", extra_calls=()):
    return {
        "parsed": None,
        "parsing_error": ValueError("Extra data"),
        "raw": SimpleNamespace(
            tool_calls=list(extra_calls),
            invalid_tool_calls=[{"name": name, "args": arguments}],
        ),
    }


def _run_raw_judge(raw):
    llm = _FakeJudgeLLM(raw)
    batch = judge_with_evidence(
        _assembly([_candidate("c1")]), {"c1": _verification("c1")},
        _artifacts(), judge_llm=llm, structured_method="function_calling", max_retries=1,
    )
    return batch, llm


def test_judge_recovers_single_trailing_bracket_without_another_model_call():
    arguments = EvidenceJudgeBatch(assessments=[_assessment("c1")]).model_dump_json()
    batch, llm = _run_raw_judge(_raw_judge(arguments + "]"))
    assert len(batch.final_issues) == 1
    assert llm.calls == 1
    assert any(event == "evidence_judge_output_recovered" for event, _ in batch.trace)


def test_judge_recovers_through_real_langchain_parser_without_network():
    import httpx
    from langchain_openai import ChatOpenAI

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "test",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_test", "type": "function", "function": {
                        "name": "EvidenceJudgeBatch",
                        "arguments": EvidenceJudgeBatch(
                            assessments=[_assessment("c1")]
                        ).model_dump_json() + "]",
                    },
                }],
            }}],
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        llm = ChatOpenAI(model="test", api_key="test", http_client=client, max_retries=0)
        batch = judge_with_evidence(
            _assembly([_candidate("c1")]), {"c1": _verification("c1")},
            _artifacts(), judge_llm=llm, structured_method="function_calling", max_retries=1,
        )
    assert len(batch.final_issues) == 1
    assert len(requests) == 1


@pytest.mark.parametrize("suffix", [" {}", " explanatory text", "]]", ',"action":"drop"'])
def test_judge_does_not_discard_arbitrary_trailing_content(suffix):
    arguments = EvidenceJudgeBatch(assessments=[_assessment("c1")]).model_dump_json()
    batch, _ = _run_raw_judge(_raw_judge(arguments + suffix))
    assert not batch.final_issues
    assert batch.verdicts[0].reason_code == "verification_failed"


def test_recovered_judge_still_checks_evidence_references():
    arguments = EvidenceJudgeBatch(
        assessments=[_assessment("c1", evidence=["F999"])]
    ).model_dump_json()
    batch, _ = _run_raw_judge(_raw_judge(arguments + "]"))
    assert not batch.final_issues


@pytest.mark.parametrize("raw", [
    _raw_judge('{"assessments": []}]', name="AnotherTool"),
    _raw_judge('{"assessments": []}]', extra_calls=[{"name": "AnotherTool"}]),
    _raw_judge('{"assessments": [], "assessments": []}]'),
    _raw_judge('{"assessments": [{"action": "keep"}]}]'),
])
def test_judge_recovery_rejects_ambiguous_or_incomplete_payload(raw):
    batch, _ = _run_raw_judge(raw)
    assert not batch.final_issues


def test_file_level_candidate_exposes_unresolved_location_limitation():
    candidate = _candidate("c-location").model_copy(update={"line": 0})
    llm = _FakeJudgeLLM(EvidenceJudgeBatch(assessments=[_assessment("c-location")]))
    judge_with_evidence(
        _assembly([candidate]),
        {"c-location": _verification("c-location")},
        _artifacts(),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    limitations = llm.payloads[0]["candidates"][0]["evidence"][0]["limitations"]
    assert "candidate_location_unresolved" in limitations
    assert (
        llm.payloads[0]["candidates"][0]["verified_evidence"]
        == llm.payloads[0]["candidates"][0]["evidence"]
    )


def test_evidence_gap_is_visible_but_has_no_evidence_id():
    candidate = _candidate("c-gap")
    verification = _verification("c-gap").model_copy(
        update={
            "evidence_gaps": [
                EvidenceGap(
                    artifact_id="ev-gap",
                    tool="query_relations",
                    arguments={
                        "subject_symbol_id": "java:A#m()",
                        "relation": "callees",
                    },
                    declared_role=EvidenceRole.REACHABILITY,
                    reason="graph_indeterminate",
                    limitations=("graph_coverage_partial",),
                )
            ]
        }
    )
    llm = _FakeJudgeLLM(EvidenceJudgeBatch(assessments=[_assessment("c-gap")]))
    judge_with_evidence(
        _assembly([candidate]),
        {"c-gap": verification},
        _artifacts(),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    gap = llm.payloads[0]["candidates"][0]["evidence_gaps"][0]
    assert gap["reason"] == "graph_indeterminate"
    assert "evidence_id" not in gap


def test_mock模式_确定性keep_提案严重度():
    candidate = _candidate("c1")
    batch = judge_with_evidence(
        _assembly([candidate]),
        {"c1": _verification("c1")},
        _artifacts(),
        judge_llm=None,
        structured_method="function_calling",
        max_retries=1,
    )
    assert len(batch.verdicts) == 1
    assert batch.verdicts[0].action == "keep"
    assert batch.verdicts[0].reason_code == "mock_deterministic_keep"
    assert len(batch.final_issues) == 1
    assert batch.final_issues[0].severity is Severity.WARNING


def test_keep裁决_产出issue():
    candidate = _candidate("c1")
    llm = _FakeJudgeLLM(
        EvidenceJudgeBatch(assessments=[_assessment("c1", severity=Severity.CRITICAL)])
    )
    batch = judge_with_evidence(
        _assembly([candidate]),
        {"c1": _verification("c1")},
        _artifacts(),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].action == "keep"
    assert batch.final_issues[0].severity is Severity.CRITICAL


def test_judge_contract_retry_recovers_missing_candidate_assessment():
    candidates = [_candidate("c1"), _candidate("c2")]
    llm = _ContractRetryJudgeLLM("c1", "c2")
    batch = judge_with_evidence(
        _assembly(candidates),
        {"c1": _verification("c1"), "c2": _verification("c2")},
        _artifacts(),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert llm.calls == 2
    assert {verdict.candidate_id for verdict in batch.verdicts} == {"c1", "c2"}
    assert all((verdict.action == "keep" for verdict in batch.verdicts))
    assert len(batch.final_issues) == 2


def test_drop裁决_不产出issue():
    candidate = _candidate("c1")
    llm = _FakeJudgeLLM(
        EvidenceJudgeBatch(
            assessments=[_assessment("c1", action="drop", severity=None, evidence=[])]
        )
    )
    batch = judge_with_evidence(
        _assembly([candidate]),
        {"c1": _verification("c1")},
        _artifacts(),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].action == "drop"
    assert batch.final_issues == []


def test_judge_drop_does_not_discard_verified_return_state_observation():
    """Model wording must not erase a ledger-closed state propagation fact."""
    patch = "-\treturn context;\n+\treturn doOpenInternal(retryPolicy, state);\n"
    source = "\n        if (this.retryContextCache.containsKey(key)) {\n            RetryContext context = this.retryContextCache.get(key);\n            context.removeAttribute(RetryContext.CLOSED);\n            return doOpenInternal(retryPolicy, state);\n        }\n    "
    graph = json.dumps(
        {
            "schema_version": 2,
            "subject_symbol_id": "java:A#open()",
            "relationships": [
                {
                    "sourceId": "java:A#open()",
                    "targetId": "java:A#doOpenInternal()",
                    "kind": "CALLS",
                }
            ],
        }
    )
    task = ReviewTask(id=TASK_ID, file="src/A.java", patch=patch, changed_lines=[500])
    candidate = CandidateIssue(
        id="c-return-state",
        task_id=TASK_ID,
        source_agent="behavior",
        file="src/A.java",
        line=500,
        type="state-propagation",
        claim="open 返回对象可能丢失缓存中的重试状态",
        mechanism="局部 context 清理后 return 改为调用内部 opener",
        impact="调用方可能观察到缓存状态或重试计数不一致",
        evidence_observation="源码显示缓存 context 被读取并清理后未作为返回值传出",
        evidence_refs=[
            EvidenceRef(artifact_id="ev-patch", declared_role=EvidenceRole.MECHANISM),
            EvidenceRef(artifact_id="ev-source", declared_role=EvidenceRole.MECHANISM),
            EvidenceRef(
                artifact_id="ev-graph", declared_role=EvidenceRole.REACHABILITY
            ),
        ],
    )
    patch_artifact = EvidenceArtifact.build(
        task_id=TASK_ID,
        reviewer="behavior",
        revision=REV,
        source_kind=EvidenceSourceKind.TASK_PATCH,
        payload=patch,
        availability=ArtifactAvailability.AVAILABLE,
        capture_mode=EvidenceCaptureMode.GENERATED,
    )
    source_artifact = EvidenceArtifact.build(
        task_id=TASK_ID,
        reviewer="behavior",
        revision=REV,
        source_kind=EvidenceSourceKind.TOOL_CALL,
        tool="read_symbol",
        arguments={"symbol_id": "java:A#open()"},
        payload=source,
        availability=ArtifactAvailability.AVAILABLE,
        capture_mode=EvidenceCaptureMode.EXECUTED,
    )
    graph_artifact = EvidenceArtifact.build(
        task_id=TASK_ID,
        reviewer="behavior",
        revision=REV,
        source_kind=EvidenceSourceKind.TOOL_CALL,
        tool="query_relations",
        arguments={
            "subject_symbol_id": "java:A#open()",
            "path_kind": "behavior",
            "depth": "3",
            "relation": "callees",
        },
        payload=graph,
        availability=ArtifactAvailability.AVAILABLE,
        capture_mode=EvidenceCaptureMode.EXECUTED,
    )
    verification = CandidateVerification(
        candidate_id=candidate.id,
        source_kinds={EvidenceSourceKind.TASK_PATCH, EvidenceSourceKind.TOOL_CALL},
        valid_evidence=[
            VerifiedEvidence(
                artifact_id="ev-patch",
                source_kind=EvidenceSourceKind.TASK_PATCH,
                content=patch,
                validation_status=EvidenceValidationStatus.VALID,
            ),
            VerifiedEvidence(
                artifact_id="ev-source",
                source_kind=EvidenceSourceKind.TOOL_CALL,
                tool="read_symbol",
                arguments={"symbol_id": "java:A#open()"},
                content=source,
                validation_status=EvidenceValidationStatus.VALID,
            ),
            VerifiedEvidence(
                artifact_id="ev-graph",
                source_kind=EvidenceSourceKind.TOOL_CALL,
                tool="query_relations",
                arguments={
                    "subject_symbol_id": "java:A#open()",
                    "path_kind": "behavior",
                    "depth": "3",
                    "relation": "callees",
                },
                content=graph,
                validation_status=EvidenceValidationStatus.VALID,
            ),
        ],
        grounding_status="grounded",
        eligible_for_judge=True,
    )
    llm = _FakeJudgeLLM(
        EvidenceJudgeBatch(
            assessments=[
                _assessment(
                    candidate.id,
                    action="drop",
                    severity=None,
                    evidence=["F001", "F002"],
                )
            ]
        )
    )
    assembly = DossierAssembly(
        (CandidateDossier(candidate=candidate, task=task, symbol_context=None),), (), ()
    )
    batch = judge_with_evidence(
        assembly,
        {candidate.id: verification},
        {
            "ev-patch": patch_artifact,
            "ev-source": source_artifact,
            "ev-graph": graph_artifact,
        },
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].action == "keep"
    assert batch.verdicts[0].reason_code == "deterministic_evidence_keep"
    assert batch.final_issues[0].severity is Severity.WARNING


def test_不可裁决候选_按验证淘汰原因drop():
    candidate = _candidate("c1")
    batch = judge_with_evidence(
        _assembly([candidate]),
        {
            "c1": _verification("c1", eligible=False).model_copy(
                update={"rejection_reason": "patch_artifact_missing_or_corrupt"}
            )
        },
        _artifacts(),
        judge_llm=None,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].reason_code == "patch_artifact_missing_or_corrupt"
    assert batch.final_issues == []


def test_keep无evidence_合同违约_fail_closed():
    candidate = _candidate("c1")
    llm = _FakeJudgeLLM(
        EvidenceJudgeBatch(assessments=[_assessment("c1", evidence=[])])
    )
    batch = judge_with_evidence(
        _assembly([candidate]),
        {"c1": _verification("c1")},
        _artifacts(),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].reason_code == "verification_failed"
    assert batch.final_issues == []


def test_keep缺severity_合同违约():
    candidate = _candidate("c1")
    llm = _FakeJudgeLLM(
        EvidenceJudgeBatch(assessments=[_assessment("c1", severity=None)])
    )
    batch = judge_with_evidence(
        _assembly([candidate]),
        {"c1": _verification("c1")},
        _artifacts(),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].reason_code == "verification_failed"


def test_maintainability_候选由_judge决定_CRITICAL():
    candidate = _candidate("c1", source_agent="maintainability")
    llm = _FakeJudgeLLM(
        EvidenceJudgeBatch(assessments=[_assessment("c1", severity=Severity.CRITICAL)])
    )
    batch = judge_with_evidence(
        _assembly([candidate]),
        {"c1": _verification("c1")},
        _artifacts(),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].reason_code == "ok"
    assert batch.final_issues[0].severity is Severity.CRITICAL


def test_evidence全为LOCATION_违约():
    candidate = _candidate("c1", role=EvidenceRole.LOCATION)
    llm = _FakeJudgeLLM(
        EvidenceJudgeBatch(assessments=[_assessment("c1", evidence=["F002"])])
    )
    batch = judge_with_evidence(
        _assembly([candidate]),
        {"c1": _verification("c1")},
        _artifacts(),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].reason_code == "verification_failed"


def test_evidence引用未知ID_违约():
    candidate = _candidate("c1")
    llm = _FakeJudgeLLM(
        EvidenceJudgeBatch(assessments=[_assessment("c1", evidence=["F999"])])
    )
    batch = judge_with_evidence(
        _assembly([candidate]),
        {"c1": _verification("c1")},
        _artifacts(),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].reason_code == "verification_failed"


def test_drop带severity_违约():
    candidate = _candidate("c1")
    llm = _FakeJudgeLLM(
        EvidenceJudgeBatch(
            assessments=[_assessment("c1", action="drop", severity=Severity.WARNING)]
        )
    )
    batch = judge_with_evidence(
        _assembly([candidate]),
        {"c1": _verification("c1")},
        _artifacts(),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].reason_code == "verification_failed"


def test_drop可以引用已知证据():
    candidate = _candidate("c1")
    llm = _FakeJudgeLLM(
        EvidenceJudgeBatch(
            assessments=[
                _assessment("c1", action="drop", severity=None, evidence=["F001"])
            ]
        )
    )
    batch = judge_with_evidence(
        _assembly([candidate]),
        {"c1": _verification("c1")},
        _artifacts(),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].reason_code == "judge_drop"


def test_批失败_二分为单候选_仍能裁决():
    candidates = [_candidate("c1"), _candidate("c2")]
    llm = _FakeJudgeLLM(None)
    batch = judge_with_evidence(
        _assembly(candidates),
        {"c1": _verification("c1"), "c2": _verification("c2")},
        _artifacts(),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert all((v.reason_code == "verification_failed" for v in batch.verdicts))
    assert llm.calls >= 3


def test_单候选批失败_fail_closed_留痕():
    candidate = _candidate("c1")
    llm = _FakeJudgeLLM(RuntimeError("boom"))
    batch = judge_with_evidence(
        _assembly([candidate]),
        {"c1": _verification("c1")},
        _artifacts(),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].reason_code == "verification_failed"
    assert batch.final_issues == []
    assert any((event == "evidence_judge_batch_failed" for event, _ in batch.trace))


def test_输出未知候选ID_该候选fail_closed():
    candidates = [_candidate("c1"), _candidate("c2")]
    llm = _FakeJudgeLLM(
        EvidenceJudgeBatch(assessments=[_assessment("c1"), _assessment("c-unknown")])
    )
    batch = judge_with_evidence(
        _assembly(candidates),
        {"c1": _verification("c1"), "c2": _verification("c2")},
        _artifacts(),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    by_id = {v.candidate_id: v for v in batch.verdicts}
    assert by_id["c1"].action == "keep"
    assert by_id["c2"].reason_code == "verification_failed"


def test_direct_mock模式_keep提案严重度():
    candidate = _candidate("c1")
    batch = judge_direct(
        _assembly([candidate]),
        judge_llm=None,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].action == "keep"
    assert batch.final_issues[0].severity is Severity.WARNING


def test_direct_LLM不可用_fail_closed():
    candidate = _candidate("c1")
    llm = _FakeJudgeLLM(RuntimeError("boom"))
    batch = judge_direct(
        _assembly([candidate]),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].reason_code == "verification_failed"
    assert batch.final_issues == []


def test_direct_drop裁决_不产出():
    candidate = _candidate("c1")
    llm = _FakeJudgeLLM(
        EvidenceJudgeAssessment(
            candidate_id="C001", action="drop", severity=None, reason="patch 不足以成立"
        )
    )
    batch = judge_direct(
        _assembly([candidate]),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].action == "drop"
    assert batch.final_issues == []
