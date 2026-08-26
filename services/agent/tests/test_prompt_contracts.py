"""LLM-facing Prompt 的结构和关键边界契约测试。"""

from pathlib import Path


PROMPT_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "codeguard_agent"
    / "prompts"
)


def _prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def test_reviewer_prompts_have_shared_review_contract():
    for name in (
        "threat-model-base.txt",
        "behavior-base.txt",
        "maintainability-base.txt",
    ):
        prompt = _prompt(name)
        assert "## 审查步骤" in prompt
        assert "## 输出前自检" in prompt
        assert "EvidenceJudge" in prompt
        assert "只输出有明确代码依据" in prompt
        assert "宁可多报" not in prompt
        assert "只有存在明确事实缺口时" in prompt


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
