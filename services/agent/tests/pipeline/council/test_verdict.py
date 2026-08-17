"""批量 EvidenceJudge 与消融档裁决测试(Evidence Ledger)。"""
from __future__ import annotations

import json

from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.evidence import (
    CandidateVerification,
    EvidenceArtifact,
    EvidenceArtifactStatus,
    EvidenceCaptureMode,
    EvidenceJudgeAssessment,
    EvidenceJudgeBatch,
    EvidenceRef,
    EvidenceSourceKind,
    EvidenceValidationStatus,
    VerifiedEvidence,
)
from codeguard_agent.models.schemas import EvidenceRole, Severity
from codeguard_agent.models.tasks import ReviewTask
from codeguard_agent.pipeline.council.dedup import CandidateGroup
from codeguard_agent.pipeline.council.verdict import judge_direct, judge_with_evidence
from codeguard_agent.pipeline.evidence.planner import (
    CandidateDossier,
    DossierAssembly,
)

REV = "abc123:deadbeef"
TASK_ID = "task-1"


def _task() -> ReviewTask:
    return ReviewTask(
        id=TASK_ID, file="src/A.java", patch="+    exec(cmd);\n", changed_lines=[1]
    )


def _candidate(cid: str, source_agent: str = "threat_model", role: EvidenceRole = EvidenceRole.MECHANISM) -> CandidateIssue:
    return CandidateIssue(
        id=cid,
        task_id=TASK_ID,
        source_agent=source_agent,
        file="src/A.java",
        line=1,
        type="command-injection",
        severity_proposal=Severity.WARNING,
        claim="未转义参数进入命令构造",
        confidence=0.8,
        evidence_refs=[
            EvidenceRef(artifact_id="ev-patch", declared_role=EvidenceRole.MECHANISM, auto_bound=True),
            EvidenceRef(artifact_id="ev-tool", declared_role=role),
        ],
    )


def _patch_artifact() -> EvidenceArtifact:
    return EvidenceArtifact.build(
        task_id=TASK_ID, reviewer="threat_model", revision=REV,
        source_kind=EvidenceSourceKind.TASK_PATCH, payload="+    exec(cmd);\n",
        status=EvidenceArtifactStatus.COMPLETE,
        capture_mode=EvidenceCaptureMode.GENERATED,
        arguments={"file_path": "src/A.java"},
    )


def _tool_artifact() -> EvidenceArtifact:
    return EvidenceArtifact.build(
        task_id=TASK_ID, reviewer="threat_model", revision=REV,
        source_kind=EvidenceSourceKind.TOOL_CALL, tool="get_file_content",
        arguments={"file_path": "src/A.java"},
        payload="class A { void m() { exec(cmd); } }",
        status=EvidenceArtifactStatus.COMPLETE,
        capture_mode=EvidenceCaptureMode.EXECUTED,
    )


def _verification(cid: str, eligible: bool = True, role: EvidenceRole = EvidenceRole.MECHANISM) -> CandidateVerification:
    candidate = _candidate(cid, role=role)
    return CandidateVerification(
        candidate_id=cid,
        source_kinds={EvidenceSourceKind.TASK_PATCH, EvidenceSourceKind.TOOL_CALL},
        valid_evidence=[
            VerifiedEvidence(
                artifact_id="ev-patch", source_kind=EvidenceSourceKind.TASK_PATCH,
                content="+    exec(cmd);\n",
                validation_status=EvidenceValidationStatus.VALID,
            ),
            VerifiedEvidence(
                artifact_id="ev-tool", source_kind=EvidenceSourceKind.TOOL_CALL,
                tool="get_file_content", arguments={"file_path": "src/A.java"},
                content="class A { void m() { exec(cmd); } }",
                validation_status=EvidenceValidationStatus.VALID,
            ),
        ],
        grounding_status="grounded",
        eligible_for_judge=eligible,
    )


def _assembly(candidates: list[CandidateIssue]) -> DossierAssembly:
    dossiers = [
        CandidateDossier(candidate=candidate, task=_task(), context_bundle=None)
        for candidate in candidates
    ]
    return DossierAssembly(tuple(dossiers), (), ())


def _artifacts() -> dict[str, EvidenceArtifact]:
    patch = _patch_artifact()
    tool_artifact = _tool_artifact()
    return {"ev-patch": patch, "ev-tool": tool_artifact}


class _FakeJudgeLLM:
    """按输入候选数分派的伪 Judge LLM:批>1 返回 None(触发二分),单候选返回裁决。"""

    def __init__(self, result):
        self._result = result
        self.calls = 0

    def with_structured_output(self, _schema, method=None):
        return self

    def invoke(self, messages):
        self.calls += 1
        user = messages[1][1]
        payload = json.loads(user)
        candidates = payload.get("candidates") if isinstance(payload, dict) else None
        count = len(candidates) if isinstance(candidates, list) else 1
        if count > 1:
            return None
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _assessment(cid: str, action: str = "keep", severity: Severity | None = Severity.WARNING,
                supporting: list[str] | None = None, counter: list[str] | None = None) -> EvidenceJudgeAssessment:
    return EvidenceJudgeAssessment(
        candidate_id=cid,
        action=action,  # type: ignore[arg-type]
        severity=severity,
        supporting_evidence_ids=supporting if supporting is not None else ["F001", "F002"],
        counter_evidence_ids=counter or [],
        reason="patch 与文件事实均支持",
    )


# ── 批量裁决基本路径 ───────────────────────────────────────────────────


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
    llm = _FakeJudgeLLM(EvidenceJudgeBatch(
        assessments=[_assessment("c1", severity=Severity.CRITICAL)]
    ))
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


def test_drop裁决_不产出issue():
    candidate = _candidate("c1")
    llm = _FakeJudgeLLM(EvidenceJudgeBatch(
        assessments=[_assessment("c1", action="drop", severity=None, supporting=[])]
    ))
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


def test_不可裁决候选_按验证淘汰原因drop():
    candidate = _candidate("c1")
    batch = judge_with_evidence(
        _assembly([candidate]),
        {"c1": _verification("c1", eligible=False).model_copy(
            update={"rejection_reason": "direct_counter_guard"}
        )},
        _artifacts(),
        judge_llm=None,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].reason_code == "direct_counter_guard"
    assert batch.final_issues == []


# ── 输出合同校验 ───────────────────────────────────────────────────────


def test_keep无supporting_合同违约_fail_closed():
    candidate = _candidate("c1")
    llm = _FakeJudgeLLM(EvidenceJudgeBatch(
        assessments=[_assessment("c1", supporting=[])]
    ))
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
    llm = _FakeJudgeLLM(EvidenceJudgeBatch(
        assessments=[_assessment("c1", severity=None)]
    ))
    batch = judge_with_evidence(
        _assembly([candidate]),
        {"c1": _verification("c1")},
        _artifacts(),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].reason_code == "verification_failed"


def test_maintainability_候选_CRITICAL_违约():
    candidate = _candidate("c1", source_agent="maintainability")
    llm = _FakeJudgeLLM(EvidenceJudgeBatch(
        assessments=[_assessment("c1", severity=Severity.CRITICAL)]
    ))
    batch = judge_with_evidence(
        _assembly([candidate]),
        {"c1": _verification("c1")},
        _artifacts(),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].reason_code == "verification_failed"


def test_supporting全为LOCATION_违约():
    candidate = _candidate("c1", role=EvidenceRole.LOCATION)
    # 只引用 LOCATION 角色的工具事实(不含自动 patch)时违约。
    llm = _FakeJudgeLLM(EvidenceJudgeBatch(
        assessments=[_assessment("c1", supporting=["F002"])]
    ))
    batch = judge_with_evidence(
        _assembly([candidate]),
        {"c1": _verification("c1", role=EvidenceRole.LOCATION)},
        _artifacts(),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].reason_code == "verification_failed"


def test_supporting引用未知ID_违约():
    candidate = _candidate("c1")
    llm = _FakeJudgeLLM(EvidenceJudgeBatch(
        assessments=[_assessment("c1", supporting=["F999"])]
    ))
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
    llm = _FakeJudgeLLM(EvidenceJudgeBatch(
        assessments=[_assessment("c1", action="drop", severity=Severity.WARNING)]
    ))
    batch = judge_with_evidence(
        _assembly([candidate]),
        {"c1": _verification("c1")},
        _artifacts(),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].reason_code == "verification_failed"


def test_supporting_counter重叠_违约():
    candidate = _candidate("c1")
    llm = _FakeJudgeLLM(EvidenceJudgeBatch(
        assessments=[_assessment("c1", supporting=["F001"], counter=["F001"])]
    ))
    batch = judge_with_evidence(
        _assembly([candidate]),
        {"c1": _verification("c1")},
        _artifacts(),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].reason_code == "verification_failed"


# ── 失败策略:二分拆批 / fail-closed ────────────────────────────────────


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
    # 批(2 候选)返回 None → 二分;单候选也返回 None → fail-closed。
    assert all(v.reason_code == "verification_failed" for v in batch.verdicts)
    assert llm.calls >= 3  # 1 次整批 + 2 次单候选


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
    assert any(event == "evidence_judge_batch_failed" for event, _ in batch.trace)


def test_输出未知候选ID_该候选fail_closed():
    candidates = [_candidate("c1"), _candidate("c2")]
    llm = _FakeJudgeLLM(EvidenceJudgeBatch(
        assessments=[
            _assessment("c1"),
            _assessment("c-unknown"),
        ]
    ))
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


# ── 消融档 ─────────────────────────────────────────────────────────────


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


def test_direct_LLM不可用_保留提案严重度_基线语义():
    candidate = _candidate("c1")
    llm = _FakeJudgeLLM(RuntimeError("boom"))
    batch = judge_direct(
        _assembly([candidate]),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].reason_code == "direct_assessment_missing"
    assert batch.final_issues[0].severity is Severity.WARNING


def test_direct_drop裁决_不产出():
    candidate = _candidate("c1")
    llm = _FakeJudgeLLM(EvidenceJudgeAssessment(
        candidate_id="C001", action="drop", severity=None, reason="patch 不足以成立"
    ))
    batch = judge_direct(
        _assembly([candidate]),
        judge_llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )
    assert batch.verdicts[0].action == "drop"
    assert batch.final_issues == []
