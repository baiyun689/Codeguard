"""Prompt 与当前调用粒度、路由和结构化模型之间的稳定契约。"""

from __future__ import annotations

from pathlib import Path

from codeguard_agent.models.tasks import RiskTag
from codeguard_agent.pipeline.risk_rules.catalog import RISK_TAG_REVIEWERS
from codeguard_agent.pipeline.stages.reviewer_stage import (
    DEFAULT_REVIEWERS,
    _build_user_prompt,
)
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


def test_reviewer_user_prompt_labels_input_as_current_task_patch() -> None:
    rendered = _build_user_prompt("@@ patch @@", "summary")
    assert "当前任务代码变更(task patch)" in rendered
    assert "<task_patch>" in rendered
    assert "<diff_input>" not in rendered


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


def test_knowledge_tree_exactly_matches_routed_reviewer_tag_pairs() -> None:
    expected = {
        (
            REVIEWER_KNOWLEDGE_DIRS[reviewer],
            f"{tag.value}.txt",
        )
        for tag, reviewers in RISK_TAG_REVIEWERS.items()
        if tag is not RiskTag.GENERAL_REVIEW
        for reviewer in reviewers
    }
    actual = {
        (path.parent.name, path.name)
        for path in (PROMPT_DIR / "knowledge").glob("*/*.txt")
    }
    assert actual == expected


def test_every_knowledge_fragment_has_complete_review_guidance() -> None:
    headings = (
        "### 典型模式",
        "### 判定要点",
        "### 严重级别参考",
        "### 已知误报判例",
        "### 排除项",
    )
    for path in (PROMPT_DIR / "knowledge").glob("*/*.txt"):
        text = path.read_text(encoding="utf-8")
        assert all(heading in text for heading in headings), path


def test_maintainability_knowledge_does_not_claim_runtime_critical_severity() -> None:
    for path in (PROMPT_DIR / "knowledge" / "maintainability").glob("*.txt"):
        assert "CRITICAL" not in path.read_text(encoding="utf-8"), path


def test_base_severity_guidance_matches_routed_knowledge() -> None:
    behavior = _prompt("behavior-base.txt")
    assert "只要触发依赖特定输入/时序，就不是 CRITICAL" not in behavior

    maintainability = _prompt("maintainability-base.txt")
    assert "有改进空间但偏主观" not in maintainability

    threat = _prompt("threat-model-base.txt")
    assert "安全最佳实践或加固建议，无明确攻击路径" not in threat
    assert "在 diff 内" not in threat


def test_message_delivery_does_not_misclassify_http_retry_as_ssrf() -> None:
    message_delivery = (
        PROMPT_DIR / "knowledge" / "behavior" / "MESSAGE_DELIVERY.txt"
    ).read_text(encoding="utf-8")
    assert "HTTP 重试属于 SSRF_OUTBOUND" not in message_delivery


def test_threat_knowledge_does_not_recommend_unproven_best_practice_issues() -> None:
    stale_guidance = (
        "没有可达越权路径",
        "非生产调试登录提示",
        "仅建议统一使用安全路径 helper",
        "可选的字段最小化建议",
        "无秘密/攻击路径",
        "缺少推荐安全响应头但无可利用路径",
        "可读性或防御性约束建议",
        "可信合作方 URL 增加超时/审计",
    )
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (PROMPT_DIR / "knowledge" / "threat_model").glob("*.txt")
    )
    assert all(phrase not in combined for phrase in stale_guidance)


def test_evidence_and_judge_prompts_describe_wrapper_contracts() -> None:
    analysis = _prompt("evidence-analysis.txt")
    assert all(
        f"`{field}`" in analysis
        for field in ("relation", "strength", "observation", "limitation")
    )
    assert "relation 始终相对于候选主张" in analysis
    assert "不得建议 CRITICAL、WARNING 或 INFO" in analysis

    judge = _prompt("council-judge.txt")
    assert "`candidate_id`" in judge
    assert "`claim_status`" in judge
    assert "`counter_effect`" in judge
    assert "`severity_factors`" in judge
    assert "`factor_id`" in judge
    assert "`status`" in judge
    assert "`evidence_ids`" in judge
    assert "`conflicts`" in judge
    for forbidden in (
        "needs_more_evidence",
        "merge_target_id",
        "adjusted_severity",
        "requested_purpose",
    ):
        assert forbidden not in judge
    assert "不得输出最终 severity" in judge
    assert "任务 RiskTag 只能作为背景" in judge


def test_summary_and_classifier_prompts_name_structured_fields() -> None:
    summary = _prompt("summary-system.txt") + _prompt("summary-user.txt")
    assert "`summary`" in summary
    assert "唯一字段" in summary

    classifier = _prompt("evidence-tag-classifier-system.txt")
    assert all(
        f"`{field}`" in classifier for field in ("tag", "confidence", "reason")
    )
    assert "恰好选择一个" in classifier


def test_judge_prompt_names_every_synthesis_field() -> None:
    judge = _prompt("council-judge.txt")
    fields = {
        "candidate_id",
        "claim_status",
        "counter_effect",
        "severity_factors",
        "factor_id",
        "status",
        "evidence_ids",
        "conflicts",
        "reason",
    }
    assert all(f"`{field}`" in judge for field in fields)
    assert "CandidateEvidenceAssessment" in judge


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
