"""guard_scan 确定性反证扫描测试(Evidence Ledger 保留的确定性反证)。"""
from __future__ import annotations

import json

from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import (
    ContextFact,
    ReviewTask,
    RiskTag,
    TaskContextBundle,
)
from codeguard_agent.pipeline.evidence.guard_scan import scan_guard_content
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
        candidate=candidate, task=task, context_bundle=bundle
    )


def _ast_fallback_dossier(hunk_header: str) -> CandidateDossier:
    """构造只有 ast_structure fact(legacy 兜底)的 dossier,无 symbol_context。"""
    task = ReviewTask(
        id="src/Service.java#h0",
        file="src/Service.java",
        hunk_header=hunk_header,
        patch="+public void update() { save(); }",
        changed_lines=[10],
    )
    candidate = CandidateIssue(
        id="c1",
        task_id=task.id,
        source_agent="threat_model",
        file=task.file,
        line=10,
        type="authorization",
        severity_proposal=Severity.WARNING,
        claim="update lacks authorization guard",
        confidence=0.8,
    )
    bundle = TaskContextBundle(
        task_id=task.id,
        facts=[
            ContextFact(
                source="tool:get_diff_ast",
                kind="ast_structure",
                content=(
                    "AST for: src/Service.java\n"
                    "  class: Service\n"
                    "    public void update() [L9-L12]"
                ),
            )
        ],
    )
    return CandidateDossier(
        candidate=candidate, task=task, context_bundle=bundle
    )


# 12 行文件:update() 位于 1-based 第 10 行(ast_structure 兜底声明的 [L9-L12] 区间内),
# 其上一行带 @PreAuthorize。
_METHOD_GUARD_FILE = (
    "public class Service {\n"
    "    private int a = 1;\n"
    "    private int b = 2;\n"
    "    private int c = 3;\n"
    "    private int d = 4;\n"
    "    private int e = 5;\n"
    "    private int f = 6;\n"
    "    private int g = 7;\n"
    "    @PreAuthorize(\"hasRole('ADMIN')\")\n"
    "    public void update() { save(); }\n"
    "    }\n"
    "}"
)


def test_scan_guard_detects_preauthorize():
    dossier = _dossier_for_method(annotations=["PreAuthorize"])
    observation = scan_guard_content(
        dossier, '@PreAuthorize("hasRole(\'ADMIN\')")\npublic void update() {}',
        RiskTag.AUTHORIZATION,
    )
    assert observation is not None
    assert observation.strip()


def test_scan_guard_detects_transactional_for_transaction_tag():
    dossier = _dossier_for_method(method="placeOrder")
    observation = scan_guard_content(
        dossier, "@Transactional\npublic void placeOrder() {}",
        RiskTag.TRANSACTION_ATOMICITY,
    )
    assert observation is not None
    assert "Transactional" in observation


def test_scan_guard_silent_for_guard_on_other_method():
    # 多方法文件:guard 在 admin() 上,候选锚定 update()——不得误报为直接反证。
    dossier = _dossier_for_method(start_line=4, end_line=4)
    content = (
        "public class Service {\n"
        "    @PreAuthorize(\"hasRole('ADMIN')\")\n"
        "    public void admin() { }\n"
        "    public void update() { save(); }\n"
        "}"
    )
    assert scan_guard_content(dossier, content, RiskTag.AUTHORIZATION) is None


def test_scan_guard_detects_guard_on_candidate_method():
    # 多方法文件:guard 在候选锚定的 update() 声明块上——命中。
    dossier = _dossier_for_method(start_line=4, end_line=5)
    content = (
        "public class Service {\n"
        "    public void admin() { }\n"
        "\n"
        "    @PreAuthorize(\"hasRole('ADMIN')\")\n"
        "    public void update() { save(); }\n"
        "}"
    )
    assert scan_guard_content(dossier, content, RiskTag.AUTHORIZATION) is not None


def test_scan_guard_field_initializer_does_not_hijack_anchor():
    # 方法声明前的字段初始化器带括号调用,不得劫持方法锚点;
    # 候选方法上的 guard 仍应被命中(锚定来自 symbol_context 而非首个括号)。
    dossier = _dossier_for_method(start_line=4, end_line=5)
    content = (
        "public class Service {\n"
        "    private final ThreadLocal<Context> t = ThreadLocal.withInitial(() -> new Context());\n"
        "\n"
        "    @PreAuthorize(\"hasRole('ADMIN')\")\n"
        "    public void update() { save(); }\n"
        "}"
    )
    observation = scan_guard_content(dossier, content, RiskTag.AUTHORIZATION)
    assert observation is not None
    assert "PreAuthorize" in observation


def test_scan_guard_silent_for_non_security_tags():
    dossier = _dossier_for_method()
    assert scan_guard_content(dossier, "@PreAuthorize(...)\nvoid f() {}", RiskTag.PERFORMANCE) is None


def test_scan_guard_silent_without_annotation():
    dossier = _dossier_for_method(method="f")
    assert scan_guard_content(dossier, "public void f() {}", RiskTag.AUTHORIZATION) is None


def test_scan_guard_silent_for_empty_raw():
    dossier = _dossier_for_method()
    assert scan_guard_content(dossier, "", RiskTag.AUTHORIZATION) is None


def test_scan_guard_detects_class_level_guard():
    # 类声明块分支:guard 在类声明上、被审方法本体无注解 → 命中所属类声明文案。
    dossier = _dossier_for_method(start_line=3, end_line=4)
    content = (
        "@PreAuthorize(\"hasRole('ADMIN')\")\n"
        "public class Service {\n"
        "    public void update() { save(); }\n"
        "}"
    )
    observation = scan_guard_content(dossier, content, RiskTag.AUTHORIZATION)
    assert observation is not None
    assert "所属类声明" in observation


def test_scan_guard_legacy_ast_structure_fallback_resolves_candidate_method():
    # 无 method 类 symbol_context,只有 ast_structure fact:
    # _resolved_method 走 _METHOD_RANGE 匹配 + task_span 过滤兜底,命中方法声明块 guard。
    dossier = _ast_fallback_dossier("@@ -9,3 +9,3 @@")
    observation = scan_guard_content(dossier, _METHOD_GUARD_FILE, RiskTag.AUTHORIZATION)
    assert observation is not None


def test_scan_guard_legacy_ast_structure_fallback_filters_outside_task_span():
    # task_span 不覆盖 ast 声明的 [L9-L12] 区间时兜底不命中 → 不产出。
    dossier = _ast_fallback_dossier("@@ -50,3 +50,3 @@")
    assert scan_guard_content(dossier, _METHOD_GUARD_FILE, RiskTag.AUTHORIZATION) is None
