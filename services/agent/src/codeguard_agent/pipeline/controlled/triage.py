"""受控 DirectTriage：三个固定领域 reviewer 的无工具初筛。"""

from __future__ import annotations

import logging
import hashlib
from pathlib import Path
from typing import Any

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.tasks import (
    CandidateSeed,
    CoverageDecision,
    DirectTriageResult,
    ReviewTask,
    ReviewerKind,
    TaskSymbolContext,
)
from codeguard_agent.pipeline.controlled.routing import (
    bind_seed_ids,
    route_seed,
    validate_coverage,
    validate_seed,
)

logger = logging.getLogger("codeguard")
_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts" / "controlled"

_DOMAIN_PROMPTS = {
    ReviewerKind.BEHAVIOR: "direct-triage-behavior.txt",
    ReviewerKind.THREAT_MODEL: "direct-triage-threat.txt",
    ReviewerKind.MAINTAINABILITY: "direct-triage-maintainability.txt",
}


def _prompt_for(reviewer: ReviewerKind) -> str:
    common = (_PROMPT_DIR / "direct-triage-common.txt").read_text(encoding="utf-8")
    domain = (_PROMPT_DIR / _DOMAIN_PROMPTS[reviewer]).read_text(encoding="utf-8")
    return f"{common.strip()}\n\n{domain.strip()}"


def prompt_hash(reviewer: ReviewerKind) -> str:
    """返回 DirectTriage system prompt 的内容哈希，供 Trace 审计。"""

    return hashlib.sha256(_prompt_for(reviewer).encode("utf-8")).hexdigest()[:16]


def build_triage_user_prompt(
    *,
    task: ReviewTask,
    symbol_context: TaskSymbolContext | None,
    diff_summary: str = "",
    task_knowledge: str = "",
) -> str:
    """渲染 DirectTriage 的有界输入；不包含工具目录或历史 few-shot。"""

    symbols = []
    if symbol_context is not None:
        symbols = [symbol.model_dump_json() for symbol in symbol_context.symbols]
    symbol_text = "\n".join(symbols) if symbols else "(没有解析到 symbol；只能依据 diff 做局部判断)"
    knowledge = task_knowledge.strip() or "(无专项知识；使用领域基础方法)"
    summary = diff_summary.strip() or "(无变更摘要；直接阅读 task patch)"
    return (
        f'<task id="{task.id}" file="{task.file}" changed_lines="{",".join(map(str, task.changed_lines))}">\n'
        f"<task_patch>\n{task.patch}\n</task_patch>\n"
        f"<diff_summary>{summary}</diff_summary>\n"
        f"<symbol_context>\n{symbol_text}\n</symbol_context>\n"
        f"<change_units>\n  <change_unit id=\"CU-{task.id}\">"
        "当前 task 的全部 diff 变更；请声明该单元是否需要图谱事实。"
        "</change_unit>\n</change_units>\n"
        f"<knowledge_bundle>\n{knowledge}\n</knowledge_bundle>\n"
        "返回严格的 DirectTriageResult。"
    )


def _invoke_once(
    *,
    llm: Any,
    system_prompt: str,
    user_prompt: str,
    max_retries: int,
    structured_method: str,
) -> tuple[DirectTriageResult | None, str]:
    try:
        raw = invoke_with_retry(
            llm.with_structured_output(DirectTriageResult, method=structured_method),
            [("system", system_prompt), ("human", user_prompt)],
            max_retries=max_retries,
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"triage_llm_error:{type(exc).__name__}"
    if raw is None:
        return None, "triage_structured_output_missing"
    try:
        return DirectTriageResult.model_validate(raw), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"triage_schema_invalid:{type(exc).__name__}"


def run_direct_triage(
    *,
    reviewer: ReviewerKind,
    task: ReviewTask,
    symbol_context: TaskSymbolContext | None,
    llm: Any,
    diff_summary: str,
    task_knowledge: str,
    max_retries: int,
    structured_method: str,
    max_seeds_per_change_unit: int = 2,
    max_seeds_per_reviewer: int = 4,
) -> tuple[DirectTriageResult | None, tuple[str, ...]]:
    """执行一次 reviewer DirectTriage；非法结果只允许一次修复重试。"""

    if llm is None:
        return None, ("triage_llm_unavailable",)
    system = _prompt_for(reviewer)
    user = build_triage_user_prompt(
        task=task,
        symbol_context=symbol_context,
        diff_summary=diff_summary,
        task_knowledge=task_knowledge,
    )
    result, diagnostic = _invoke_once(
        llm=llm,
        system_prompt=system,
        user_prompt=user,
        max_retries=max_retries,
        structured_method=structured_method,
    )
    diagnostics: list[str] = [f"triage_prompt_hash:{prompt_hash(reviewer)}"]
    if result is None:
        diagnostics.append(diagnostic)
        repair_user = (
            f"{user}\n\n上一次输出未通过受控协议校验（{diagnostic}）。"
            "请修复为严格 DirectTriageResult；不要添加字段，不要调用工具。"
        )
        result, repair_diagnostic = _invoke_once(
            llm=llm,
            system_prompt=system,
            user_prompt=repair_user,
            max_retries=1,
            structured_method=structured_method,
        )
        if result is None:
            diagnostics.append(repair_diagnostic)
            return None, tuple(diagnostics)
        diagnostics.append("triage_protocol_repaired")

    expected_ids = (f"CU-{task.id}",)
    coverage_errors = validate_coverage(change_unit_ids=expected_ids, result=result)
    if coverage_errors:
        diagnostics.extend(coverage_errors)
        return None, tuple(diagnostics)
    coverage_by_unit = {
        declaration.change_unit_id: declaration.decision
        for declaration in result.coverage
    }

    normalized_issues: list[CandidateSeed] = []
    for seed in bind_seed_ids(result).issues:
        if seed.reviewer is not reviewer:
            diagnostics.append(f"{seed.seed_id}:reviewer_mismatch:{seed.reviewer.value}")
            continue
        seed_errors = validate_seed(seed, change_unit_ids=set(expected_ids))
        if seed_errors:
            diagnostics.extend(f"{seed.seed_id}:{item}" for item in seed_errors)
            continue
        coverage_decision = coverage_by_unit.get(seed.change_unit_id)
        if coverage_decision is CoverageDecision.NOT_APPLICABLE:
            diagnostics.append(f"{seed.seed_id}:candidate_on_not_applicable_unit")
            continue
        if (
            coverage_decision is CoverageDecision.LOCAL_ONLY
            and route_seed(seed) == "graph_required"
        ):
            diagnostics.append(f"{seed.seed_id}:graph_seed_on_local_only_unit")
            continue
        normalized_location = _normalize_seed_location(seed, task)
        if normalized_location[1]:
            diagnostics.extend(
                f"{seed.seed_id}:{item}" for item in normalized_location[1]
            )
        if normalized_location[0] is None:
            continue
        seed = normalized_location[0]
        if len(normalized_issues) >= max_seeds_per_reviewer:
            diagnostics.append("seed_reviewer_limit")
            break
        if sum(item.change_unit_id == seed.change_unit_id for item in normalized_issues) >= max_seeds_per_change_unit:
            diagnostics.append(f"seed_change_unit_limit:{seed.change_unit_id}")
            continue
        normalized_issues.append(seed)
        diagnostics.append(f"seed_route:{seed.seed_id}:{route_seed(seed)}")
    return result.model_copy(update={"issues": tuple(normalized_issues)}), tuple(diagnostics)


def _normalize_seed_location(
    seed: CandidateSeed, task: ReviewTask
) -> tuple[CandidateSeed | None, tuple[str, ...]]:
    """把 DirectTriage 的位置限制在当前 task 可证明的行。"""

    def canonical(path: str) -> str:
        return path.replace("\\", "/").strip().lower()

    if canonical(seed.location_file) != canonical(task.file):
        return None, ("location_file_mismatch",)
    if seed.location_line <= 0:
        return seed, ()
    valid_lines = set(task.changed_lines)
    valid_lines.update(anchor.anchor_line for anchor in task.deletion_anchors)
    if seed.location_line in valid_lines:
        return seed, ()
    return (
        seed.model_copy(update={"location_line": 0}),
        ("candidate_location_unresolved",),
    )


__all__ = ["build_triage_user_prompt", "prompt_hash", "run_direct_triage"]
