"""LLM-facing Prompt 的结构和关键边界契约测试。"""

from pathlib import Path
import pytest
from codeguard_agent.models.tasks import DeletionAnchor, ReviewTask
from codeguard_agent.pipeline.prompting import render_prompt_template

PROMPT_DIR = Path(__file__).resolve().parents[1] / "src" / "codeguard_agent" / "prompts"


def _prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


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


def test_all_runtime_stages_have_explicit_user_templates():
    for name in (
        "controlled/change-review.txt",
        "evidence-judge-user.txt",
        "direct-judge-user.txt",
        "causal-merge-user.txt",
        "candidate-relocation-user.txt",
    ):
        assert (PROMPT_DIR / name).is_file()


def test_prompt_template_rendering_is_strict():
    assert render_prompt_template("A {{value}}", {"value": "data"}) == "A data"
    with pytest.raises(ValueError, match="missing"):
        render_prompt_template("{{value}}", {})
    with pytest.raises(ValueError, match="extra"):
        render_prompt_template("plain", {"value": "data"})


def test_controlled_prompt_documents_extended_relation_directions_and_subjects():
    prompt = _prompt("controlled/change-review.txt")
    for relation in (
        "parents",
        "children",
        "type_users",
        "type_references",
        "entrypoints",
    ):
        assert relation in prompt
    assert "`parents` 和 `type_references`" in prompt
    assert "`children`、`type_users` 和 `entrypoints`" in prompt
    assert "`type_references` 用于方法、构造器、字段或类型主体" in prompt
