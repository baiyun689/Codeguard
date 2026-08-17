"""Evidence Ledger 内容寻址 Artifact 的工程正确性测试。

覆盖:内容寻址 ID 的稳定性与敏感性、Artifact 构造、fan-in 归并 reducer、
ToolClient/编排器/评测 runner 的 revision 贯通(源文档 §4.3/§5.1)。
"""

from __future__ import annotations

import hashlib

import httpx

from codeguard_agent.models.evidence import (
    EvidenceArtifact,
    EvidenceArtifactStatus,
    EvidenceCaptureMode,
    EvidenceSourceKind,
    compute_artifact_id,
    merge_evidence_artifacts,
)
from codeguard_agent.pipeline.orchestrator import resolve_evidence_revision
from codeguard_agent.tools.tool_client import ToolClient, create_tool_session

from evals.runner import case_evidence_revision
from evals.schema import CaseProvenance, EvalCase

REV = "abc123:deadbeef"
TASK = "task-1"


def _artifact(**overrides) -> EvidenceArtifact:
    defaults = dict(
        task_id=TASK,
        reviewer="threat_model",
        revision=REV,
        source_kind=EvidenceSourceKind.TOOL_CALL,
        tool="get_file_content",
        arguments={"file_path": "src/A.java"},
        payload="public void run() {\n    exec(cmd);\n}\n",
        status=EvidenceArtifactStatus.COMPLETE,
        capture_mode=EvidenceCaptureMode.EXECUTED,
    )
    defaults.update(overrides)
    return EvidenceArtifact.build(**defaults)


def test_内容寻址_相同输入稳定():
    a = compute_artifact_id(
        REV, TASK, EvidenceSourceKind.TOOL_CALL, "get_file_content",
        {"file_path": "src/A.java"}, "payload",
    )
    b = compute_artifact_id(
        REV, TASK, EvidenceSourceKind.TOOL_CALL, "get_file_content",
        {"file_path": "src/A.java"}, "payload",
    )
    assert a == b
    assert a.startswith("ev-")
    assert len(a) == 19


def test_内容寻址_revision_变化换ID():
    base = compute_artifact_id("r1", TASK, EvidenceSourceKind.TASK_PATCH, "", {}, "p")
    other = compute_artifact_id("r2", TASK, EvidenceSourceKind.TASK_PATCH, "", {}, "p")
    assert base != other


def test_内容寻址_payload_变化换ID():
    base = compute_artifact_id(REV, TASK, EvidenceSourceKind.TASK_PATCH, "", {}, "p")
    other = compute_artifact_id(REV, TASK, EvidenceSourceKind.TASK_PATCH, "", {}, "p2")
    assert base != other


def test_内容寻址_task_或_工具_变化换ID():
    base = compute_artifact_id(
        REV, TASK, EvidenceSourceKind.TOOL_CALL, "inspect_structure", {}, "p"
    )
    assert base != compute_artifact_id(
        REV, "task-2", EvidenceSourceKind.TOOL_CALL, "inspect_structure", {}, "p"
    )
    assert base != compute_artifact_id(
        REV, TASK, EvidenceSourceKind.TOOL_CALL, "get_file_content", {}, "p"
    )


def test_内容寻址_参数键序无关():
    a = compute_artifact_id(REV, TASK, EvidenceSourceKind.TOOL_CALL, "t", {"a": "1", "b": "2"}, "p")
    b = compute_artifact_id(REV, TASK, EvidenceSourceKind.TOOL_CALL, "t", {"b": "2", "a": "1"}, "p")
    assert a == b


def test_build_与_内容寻址一致():
    art = _artifact()
    assert art.id == compute_artifact_id(
        art.revision, art.task_id, art.source_kind, art.tool, art.arguments, art.payload
    )
    assert art.payload_hash == hashlib.sha256(art.payload.encode("utf-8")).hexdigest()
    assert art.limitations == ()


def test_merge_reducer_合并左右字典():
    a = _artifact()
    b = _artifact(task_id="task-2")
    merged = merge_evidence_artifacts({a.id: a}, {b.id: b})
    assert set(merged) == {a.id, b.id}
    assert merge_evidence_artifacts(None, {a.id: a})[a.id] is a
    assert merge_evidence_artifacts({a.id: a}, None)[a.id] is a
    # 同 ID 后写覆盖
    c = _artifact(status=EvidenceArtifactStatus.FAILED)
    assert merge_evidence_artifacts({a.id: a}, {a.id: c})[a.id].status is EvidenceArtifactStatus.FAILED


def test_tool_client_保存_revision():
    assert ToolClient("http://x", "sess").revision == ""
    assert ToolClient("http://x", "sess", revision="head:tree").revision == "head:tree"


def test_创建会话_把_revision_传给客户端(monkeypatch):
    captured: dict = {}

    class _FakeResp:
        def raise_for_status(self):  # noqa: D401
            return None

        def json(self):
            return {"success": True, "session_id": "sess-9"}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **k):
            captured["json"] = k["json"]
            return _FakeResp()

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    client = create_tool_session(
        "http://toolserver", "/repo", ["a.java"], revision="abc:def"
    )
    assert captured["json"]["revision"] == "abc:def"
    assert client.revision == "abc:def"


def test_编排器_revision_回退链():
    diff = "diff body"
    digest = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    assert resolve_evidence_revision("explicit", None, diff) == "explicit"
    assert (
        resolve_evidence_revision("", ToolClient("http://x", "s", revision="abc:def"), diff)
        == "abc:def"
    )
    assert resolve_evidence_revision("", None, diff) == f"diff:{digest}"
    assert (
        resolve_evidence_revision("", ToolClient("http://x", "s"), diff) == f"diff:{digest}"
    )


def test_评测用例_revision_组合():
    case = EvalCase(
        id="c1",
        category="x",
        diff="diff body",
        provenance=CaseProvenance(head_revision="cd521160"),
    )
    digest = hashlib.sha256("diff body".encode("utf-8")).hexdigest()
    assert case_evidence_revision(case) == f"cd521160:{digest}"
    assert case_evidence_revision(EvalCase(id="c2", category="x", diff="d")) == ""
