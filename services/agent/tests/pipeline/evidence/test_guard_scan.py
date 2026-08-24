"""guard_scan 确定性反证扫描测试(Evidence Ledger 保留的确定性反证)。"""
from __future__ import annotations

from codeguard_agent.models.council import CandidateIssue
from codeguard_agent.models.schemas import Severity
from codeguard_agent.models.tasks import (
    ResolvedSymbol,
    ReviewTask,
    SymbolResolutionStatus,
    TaskSymbolContext,
)
from codeguard_agent.pipeline.evidence.guard_scan import scan_guard_content
from codeguard_agent.pipeline.evidence.planner import CandidateDossier


def _dossier_for_method(
    method: str = "update",
    start_line: int = 1,
    end_line: int = 2,
    annotations: list[str] | None = None,
    source_agent: str = "threat_model",
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
        source_agent=source_agent,
        file=task.file,
        line=start_line,
        type="authorization",
        severity_proposal=Severity.WARNING,
        claim=f"{method} lacks authorization guard",
        confidence=0.8,
    )
    context = TaskSymbolContext(
        task_id=task.id,
        status=SymbolResolutionStatus.RESOLVED,
        symbols=(
            ResolvedSymbol(
                file=task.file,
                symbol_id=f"java:Service#{method}()",
                kind="method",
                start_line=start_line,
                end_line=end_line,
                signature=f"public void {method}()",
                annotations=tuple(annotations or ()),
                source_set="MAIN",
            ),
        ),
    )
    return CandidateDossier(
        candidate=candidate, task=task, symbol_context=context
    )


def test_scan_guard_detects_preauthorize():
    dossier = _dossier_for_method(annotations=["PreAuthorize"])
    observation = scan_guard_content(
        dossier, '@PreAuthorize("hasRole(\'ADMIN\')")\npublic void update() {}',
        "threat_model",
    )
    assert observation is not None
    assert observation.strip()


def test_scan_guard_detects_transactional_for_behavior():
    dossier = _dossier_for_method(method="placeOrder", source_agent="behavior")
    observation = scan_guard_content(
        dossier, "@Transactional\npublic void placeOrder() {}",
        "behavior",
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
    assert scan_guard_content(dossier, content, "threat_model") is None


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
    assert scan_guard_content(dossier, content, "threat_model") is not None


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
    observation = scan_guard_content(dossier, content, "threat_model")
    assert observation is not None
    assert "PreAuthorize" in observation


def test_scan_guard_silent_for_maintainability():
    # guard 过滤按发现者分工:maintainability 候选不扫任何注解。
    dossier = _dossier_for_method(source_agent="maintainability")
    assert scan_guard_content(dossier, "@PreAuthorize(...)\nvoid f() {}", "maintainability") is None


def test_scan_guard_silent_without_annotation():
    dossier = _dossier_for_method(method="f")
    assert scan_guard_content(dossier, "public void f() {}", "threat_model") is None


def test_scan_guard_silent_for_empty_raw():
    dossier = _dossier_for_method()
    assert scan_guard_content(dossier, "", "threat_model") is None


def test_scan_guard_detects_class_level_guard():
    # 类声明块分支:guard 在类声明上、被审方法本体无注解 → 命中所属类声明文案。
    dossier = _dossier_for_method(start_line=3, end_line=4)
    content = (
        "@PreAuthorize(\"hasRole('ADMIN')\")\n"
        "public class Service {\n"
        "    public void update() { save(); }\n"
        "}"
    )
    observation = scan_guard_content(dossier, content, "threat_model")
    assert observation is not None
    assert "所属类声明" in observation
