"""LLM-facing Prompt 的结构和关键边界契约测试。"""

from pathlib import Path

import pytest

from codeguard_agent.pipeline.prompting import render_prompt_template
from codeguard_agent.pipeline.reviewers.reviewers import (
    DEFAULT_REVIEWERS,
    build_reviewer_system_prompt,
)


PROMPT_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "codeguard_agent"
    / "prompts"
)


def _prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def test_reviewer_prompts_have_shared_review_contract():
    for reviewer in DEFAULT_REVIEWERS:
        prompt = build_reviewer_system_prompt(reviewer)
        assert "## 审查步骤" in prompt
        assert prompt.count("## ReAct 终止与输出合同") == 1
        assert "EvidenceJudge" in prompt
        assert "只输出有明确代码依据" in prompt
        assert "宁可多报" not in prompt
        assert "只有存在明确事实缺口时" in prompt
        assert '"summary"' in prompt
        assert '"issues"' in prompt
        assert "issues=[]" in prompt
        assert "assistant 消息只能包含工具调用" in prompt
        assert "最终消息不能同时包含工具调用和审查结果" in prompt


def test_discovery_evidence_contract_requires_minimal_sufficient_references():
    prompt = _prompt("discovery-evidence-contract.txt")
    for text in (
        "最小充分证据集",
        "删除该引用",
        "探索性",
        "重复性",
        "被后续事实推翻",
        "最多选择 3 条",
    ):
        assert text in prompt


def test_reviewer_prompts_define_tool_decision_protocol():
    expected_tools = {
        "threat-model-base.txt": "inspect_security_path",
        "behavior-base.txt": "inspect_change_impact",
        "maintainability-base.txt": "inspect_structure",
    }
    for name, specialist_tool in expected_tools.items():
        prompt = _prompt(name)
        for text in (
            "## 工具决策协议",
            "只能使用 `symbol_context` 中已有的稳定 `symbol_id`",
            "不要为了收集信息调用所有工具",
            "工具返回已经足以确认或否定",
            "工具失败、`partial` 或 `indeterminate`",
            "工具返回的事实必须通过 `evidence_refs` 引用",
        ):
            assert text in prompt
        assert specialist_tool in prompt


def test_plan_prompt_has_field_contract_and_final_check():
    prompt = _prompt("review-plan.txt")
    for text in (
        "## 规划步骤",
        "## 输出前自检",
        "`reviewers` 是否与 `reviewer_plans[].reviewer` 完全一致",
        "每个 Reviewer 是否至少有一个具体 objective",
        "fallback、fallback_reason",
    ):
        assert text in prompt


def test_judge_prompts_define_evidence_boundaries():
    evidence_prompt = _prompt("evidence-judge.txt")
    direct_prompt = _prompt("direct-judge.txt")
    for prompt in (evidence_prompt, direct_prompt):
        assert "# Input Contract" in prompt
        assert "# Decision Procedure" in prompt
        assert "## 输出前自检" in prompt
        assert "severity 必须为 null" in prompt
        assert "keep" in prompt and "drop" in prompt
    assert "evidence_gaps" in evidence_prompt
    assert "evidence_ids` 在本模式固定为空数组" in direct_prompt


def test_judge_prompts_define_dimension_specific_severity_rubric():
    for name in ("evidence-judge.txt", "direct-judge.txt"):
        prompt = _prompt(name)
        for text in (
            "consequence",
            "reachability",
            "scope",
            "reversibility",
            "CRITICAL",
            "WARNING",
            "INFO",
            "threat_model",
            "behavior",
            "maintainability",
            "证据不足不能降为 INFO",
            "不能单独支撑 CRITICAL",
            "也不能自动否定 CRITICAL",
        ):
            assert text in prompt


def test_causal_and_location_prompts_define_uncertainty_contracts():
    causal = _prompt("causal-merge-system.txt")
    relocation = _prompt("candidate-relocation.txt")
    for text in (
        "`unknown` 不等于相同",
        "无法确认 same_cause 或 same_effect 时必须返回 null",
        "只输出 profiles 和 comparisons",
    ):
        assert text in causal
    for text in (
        "# Input Contract",
        "# Output Field Contract",
        "## 输出前自检",
        "连续 1～5 行",
        "禁止猜测",
    ):
        assert text in relocation


def test_summary_prompt_is_fact_only_and_single_field():
    prompt = _prompt("summary-system.txt")
    assert "## Scope" in prompt
    assert "## Input Contract" in prompt
    assert "唯一字段 `summary`" in prompt
    assert "## 输出前自检" in prompt
    assert "不提出修复建议" in prompt


def test_all_runtime_stages_have_explicit_user_templates():
    for name in (
        "review-plan-user.txt",
        "threat-model-user.txt",
        "behavior-user.txt",
        "maintainability-user.txt",
        "evidence-judge-user.txt",
        "direct-judge-user.txt",
        "causal-merge-user.txt",
        "candidate-relocation-user.txt",
        "summary-user.txt",
    ):
        assert (PROMPT_DIR / name).is_file()


def test_prompt_template_rendering_is_strict():
    assert render_prompt_template("A {{value}}", {"value": "data"}) == "A data"
    with pytest.raises(ValueError, match="missing"):
        render_prompt_template("{{value}}", {})
    with pytest.raises(ValueError, match="extra"):
        render_prompt_template("plain", {"value": "data"})


def test_user_template_rendering_keeps_dynamic_data_out_of_system_prompts():
    marker = "TASK-DYNAMIC-001"
    user = render_prompt_template(
        _prompt("review-plan-user.txt"),
        {"plan_unit": f'<plan_unit id="{marker}">patch</plan_unit>'},
    )
    assert marker in user
    assert marker not in _prompt("review-plan.txt")

    for name in (
        "threat-model-base.txt",
        "behavior-base.txt",
        "maintainability-base.txt",
        "evidence-judge.txt",
        "direct-judge.txt",
        "causal-merge-system.txt",
        "candidate-relocation.txt",
        "summary-system.txt",
    ):
        assert marker not in _prompt(name)
