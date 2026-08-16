"""guard_scan 确定性反证扫描测试(ADR-046)。"""
from __future__ import annotations

import json

from codeguard_agent.models.council import CandidateFact, CandidateIssue
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import (
    ContextFact,
    ReviewTask,
    RiskTag,
    TaskContextBundle,
)
from codeguard_agent.pipeline.evidence.guard_scan import scan_guard_fact
from codeguard_agent.pipeline.evidence.planner import CandidateDossier


def _dossier_for_method(
    method: str = "update",
    start_line: int = 1,
    end_line: int = 2,
    annotations: list[str] | None = None,
) -> CandidateDossier:
    """构造锚定指定方法的 dossier(symbol_context 形状按 resolve_change_context 输出)。"""
    task = ReviewTask(
        id="src/Service.java#h0",
        file="src/Service.java",
        hunk_header="@@ -1,2 +1,2 @@",
        patch=f"+public void {method}() {{ save(); }}",
        changed_lines=[start_line],
    )
    candidate = CandidateIssue(
        id="c1",
        task_id=task.id,
        source_agent="threat_model",
        file=task.file,
        line=start_line,
        type="authorization",
        severity_proposal=Severity.WARNING,
        claim=f"{method} lacks authorization guard",
        confidence=0.8,
    )
    bundle = TaskContextBundle(
        task_id=task.id,
        facts=[
            ContextFact(
                source="tool:resolve_change_context",
                kind="symbol_context",
                content=json.dumps(
                    {
                        "file": task.file,
                        "file_id": f"file:{task.file}",
                        "symbol_id": f"java:Service#{method}()",
                        "kind": "method",
                        "start_line": start_line,
                        "end_line": end_line,
                        "signature": f"public void {method}()",
                        "annotations": [] if annotations is None else annotations,
                        "control_flow": [],
                        "resolution": "resolved",
                    },
                    sort_keys=True,
                ),
            )
        ],
    )
    return CandidateDossier(
        candidate=candidate, task=task, context_bundle=bundle, requests=(), notes=()
    )


def _fact(raw: str) -> CandidateFact:
    return CandidateFact(
        fact_id="f1", source="tool:get_file_content", raw=raw, replay_status="verified"
    )


def test_scan_guard_detects_preauthorize_as_direct_contradicts():
    dossier = _dossier_for_method(annotations=["PreAuthorize"])
    fact = _fact('@PreAuthorize("hasRole(\'ADMIN\')")\npublic void update() {}')
    relation = scan_guard_fact(dossier, fact, RiskTag.AUTHORIZATION)
    assert relation is not None
    assert relation.relation == "contradicts"
    assert relation.strength == "direct"
    assert relation.observation.strip()


def test_scan_guard_detects_transactional_for_transaction_tag():
    dossier = _dossier_for_method(method="placeOrder")
    fact = _fact("@Transactional\npublic void placeOrder() {}")
    relation = scan_guard_fact(dossier, fact, RiskTag.TRANSACTION_ATOMICITY)
    assert relation is not None
    assert relation.relation == "contradicts"
    assert relation.strength == "direct"
    assert "Transactional" in relation.observation


def test_scan_guard_silent_for_guard_on_other_method():
    # 多方法文件:guard 在 admin() 上,候选锚定 update()——不得误报为直接反证。
    dossier = _dossier_for_method(start_line=4, end_line=4)
    fact = _fact(
        "public class Service {\n"
        "    @PreAuthorize(\"hasRole('ADMIN')\")\n"
        "    public void admin() { }\n"
        "    public void update() { save(); }\n"
        "}"
    )
    assert scan_guard_fact(dossier, fact, RiskTag.AUTHORIZATION) is None


def test_scan_guard_detects_guard_on_candidate_method():
    # 多方法文件:guard 在候选锚定的 update() 声明块上——命中 direct contradicts。
    dossier = _dossier_for_method(start_line=4, end_line=5)
    fact = _fact(
        "public class Service {\n"
        "    public void admin() { }\n"
        "\n"
        "    @PreAuthorize(\"hasRole('ADMIN')\")\n"
        "    public void update() { save(); }\n"
        "}"
    )
    relation = scan_guard_fact(dossier, fact, RiskTag.AUTHORIZATION)
    assert relation is not None
    assert relation.relation == "contradicts"
    assert relation.strength == "direct"


def test_scan_guard_field_initializer_does_not_hijack_anchor():
    # 方法声明前的字段初始化器带括号调用,不得劫持方法锚点;
    # 候选方法上的 guard 仍应被命中(锚定来自 symbol_context 而非首个括号)。
    dossier = _dossier_for_method(start_line=4, end_line=5)
    fact = _fact(
        "public class Service {\n"
        "    private final ThreadLocal<Context> t = ThreadLocal.withInitial(() -> new Context());\n"
        "\n"
        "    @PreAuthorize(\"hasRole('ADMIN')\")\n"
        "    public void update() { save(); }\n"
        "}"
    )
    relation = scan_guard_fact(dossier, fact, RiskTag.AUTHORIZATION)
    assert relation is not None
    assert relation.relation == "contradicts"
    assert relation.strength == "direct"
    assert "PreAuthorize" in relation.observation


def test_scan_guard_silent_for_non_security_tags():
    dossier = _dossier_for_method()
    fact = _fact("@PreAuthorize(...)\nvoid f() {}")
    assert scan_guard_fact(dossier, fact, RiskTag.PERFORMANCE) is None


def test_scan_guard_silent_without_annotation():
    dossier = _dossier_for_method(method="f")
    fact = _fact("public void f() {}")
    assert scan_guard_fact(dossier, fact, RiskTag.AUTHORIZATION) is None


def test_scan_guard_silent_for_empty_raw():
    dossier = _dossier_for_method()
    fact = _fact("")
    assert scan_guard_fact(dossier, fact, RiskTag.AUTHORIZATION) is None
