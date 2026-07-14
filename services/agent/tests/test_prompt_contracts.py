"""Prompt 与当前调用粒度、路由和结构化模型之间的稳定契约。"""

from __future__ import annotations

from pathlib import Path

from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.risk_rules.catalog import RISK_TAG_REVIEWERS
from codeguard_agent.pipeline.stages.reviewer_stage import DEFAULT_REVIEWERS
from evals.matcher import _JUDGE_CASE_PROMPT


PROMPT_DIR = Path(__file__).parents[1] / "src" / "codeguard_agent" / "prompts"
REVIEWER_KNOWLEDGE_DIRS = {
    "ThreatModelAgent": "threat_model",
    "BehaviorAgent": "behavior",
    "MaintainabilityAgent": "maintainability",
}


def _prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def test_default_reviewer_prompt_files_exist() -> None:
    for reviewer in DEFAULT_REVIEWERS:
        assert (PROMPT_DIR / reviewer.prompt_file).is_file()


def test_reviewer_prompts_describe_task_scoped_conditional_tool_contract() -> None:
    for reviewer in DEFAULT_REVIEWERS:
        text = _prompt(reviewer.prompt_file)
        assert "当前 task patch" in text
        assert "完整 diff" not in text
        assert "运行时提供" in text
        assert "当前任务文件" in text
        assert "diff 外部问题" not in text
        assert "低置信候选" not in text


def test_reviewer_output_contract_names_every_review_result_field() -> None:
    fields = {
        "summary",
        "issues",
        "severity",
        "file",
        "line",
        "type",
        "message",
        "suggestion",
        "confidence",
    }
    for reviewer in DEFAULT_REVIEWERS:
        text = _prompt(reviewer.prompt_file)
        assert all(f"`{field}`" in text for field in fields)
        assert all(value in text for value in ("CRITICAL", "WARNING", "INFO"))


def test_every_routed_risk_tag_has_reviewer_knowledge() -> None:
    for tag, reviewers in RISK_TAG_REVIEWERS.items():
        if tag is RiskTag.GENERAL_REVIEW:
            continue
        for reviewer in reviewers:
            path = (
                PROMPT_DIR
                / "knowledge"
                / REVIEWER_KNOWLEDGE_DIRS[reviewer]
                / f"{tag.value}.txt"
            )
            assert path.is_file(), f"missing knowledge: {reviewer}/{tag.value}"


def test_evidence_and_judge_prompts_describe_wrapper_contracts() -> None:
    analysis = _prompt("evidence-analysis.txt")
    assert all(
        f"`{field}`" in analysis
        for field in ("relation", "strength", "observation", "limitation")
    )

    judge = _prompt("council-judge.txt")
    assert "`decisions`" in judge
    assert "`candidate_id`" in judge
    assert "不要选择 `merge`" in judge
    assert "仅在输入明确允许补证" in judge


def test_summary_and_classifier_prompts_name_structured_fields() -> None:
    summary = _prompt("summary-system.txt") + _prompt("summary-user.txt")
    assert "`summary`" in summary
    assert "唯一字段" in summary

    classifier = _prompt("evidence-tag-classifier-system.txt")
    assert all(
        f"`{field}`" in classifier for field in ("tag", "confidence", "reason")
    )
    assert "恰好选择一个" in classifier


def test_judge_prompt_names_every_judge_decision_field() -> None:
    judge = _prompt("council-judge.txt")
    fields = {
        "decisions",
        "candidate_id",
        "action",
        "reason",
        "merge_target_id",
        "adjusted_severity",
        "requested_purpose",
    }
    assert all(f"`{field}`" in judge for field in fields)
    assert "恰好包含一项" in judge


def test_aggregation_prompts_name_merge_plan_fields_and_index_base() -> None:
    aggregation = _prompt("aggregation-system.txt") + _prompt(
        "aggregation-user.txt"
    )
    assert "`groups`" in aggregation
    assert "`members`" in aggregation
    assert "从 1 开始" in aggregation
    assert "每组至少 2" in aggregation


def test_eval_judge_prompt_names_case_judgement_fields() -> None:
    assert all(
        f"`{field}`" in _JUDGE_CASE_PROMPT
        for field in ("matches", "expected_id", "reported_id", "reason", "comment")
    )
    assert "每一条标准答案恰好一项" in _JUDGE_CASE_PROMPT
