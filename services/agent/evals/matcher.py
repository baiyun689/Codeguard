"""把"审查器报出的 Issue"对到"标准答案",产出 TP/FP/FN 判定。

两层:
    1. 规则匹配(默认,零成本):file + 行号邻近 + 类型关键词。确定、可复现。
    2. LLM-as-judge(可选,--judge):对规则匹配命中的项,再用一次 LLM 判定
       "语义上是否真的命中"并给 message/suggestion 打分。规则匹配可能把
       "关键词撞对但其实说的不是一回事"的误判成命中,judge 用来纠偏 + 量化质量。

匹配是多对一/一对多的简化处理:对每条标准答案,找是否存在"能匹配上它"的报告;
匹配上即一个 TP,没匹配上即一个 FN。报告中匹配不上任何标准答案的,计为 FP。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from codeguard_agent.models.schemas import Issue

from evals.schema import EvalCase, ExpectedIssue, JudgeScore, MatchOutcome

logger = logging.getLogger("codeguard.evals")


def _file_matches(reported_file: str, expected_file: str) -> bool:
    """文件名匹配:按 basename 相等,或一方是另一方的后缀。

    LLM 报的路径可能只有文件名、也可能带不同前缀,故做宽松匹配。
    """
    r = reported_file.replace("\\", "/").strip().lower()
    e = expected_file.replace("\\", "/").strip().lower()
    if not r:
        return False
    r_base, e_base = r.rsplit("/", 1)[-1], e.rsplit("/", 1)[-1]
    return r_base == e_base or r.endswith(e) or e.endswith(r)


def _line_matches(reported_line: int, expected: ExpectedIssue) -> bool:
    """行号匹配:expected.line=0 时不校验;否则要求落在容差区间内。"""
    if expected.line <= 0:
        return True
    return abs(reported_line - expected.line) <= expected.tolerance


def _keyword_matches(issue: Issue, expected: ExpectedIssue) -> bool:
    """类型关键词匹配:report 的 type/message 命中任一关键词(忽略大小写)。"""
    haystack = f"{issue.type} {issue.message}".lower()
    return any(kw.lower() in haystack for kw in expected.type_keywords)


def rule_match(issue: Issue, expected: ExpectedIssue) -> bool:
    """规则匹配:文件 + 行号 + 关键词三者皆满足才算命中。"""
    return (
        _file_matches(issue.file, expected.file)
        and _line_matches(issue.line, expected)
        and _keyword_matches(issue, expected)
    )


# --------------------------------------------------------------------------
# LLM-as-judge(可选)
# --------------------------------------------------------------------------

_JUDGE_PROMPT = """你是代码审查评测的裁判。给你一条"标准答案"(这段代码确实存在的安全问题)\
和一条"审查器报出的问题",判断报出的问题是否在语义上命中了这条标准答案,并为其描述与建议打分。

标准答案:
- 类型关键词:{keywords}
- 说明:{note}
- 位置:{file}:{line}

审查器报出的问题:
- 类型:{r_type}
- 描述:{r_message}
- 建议:{r_suggestion}

请客观判断 semantic_match(是否真的指同一个问题),并给 message_quality / suggestion_quality \
打 1~5 分(无建议则 suggestion_quality=1)。"""


def judge_match(
    llm: Any, issue: Issue, expected: ExpectedIssue
) -> JudgeScore | None:
    """用 LLM 对一条规则命中的报告做语义复核 + 质量打分。

    llm 为 None(mock 模式)时返回 None——judge 需要真实模型。
    """
    if llm is None:
        return None
    prompt = _JUDGE_PROMPT.format(
        keywords="、".join(expected.type_keywords),
        note=expected.note or "(无)",
        file=expected.file,
        line=expected.line,
        r_type=issue.type,
        r_message=issue.message,
        r_suggestion=issue.suggestion or "(无)",
    )
    try:
        structured = llm.with_structured_output(
            JudgeScore, method=os.environ.get("CODEGUARD_STRUCTURED_METHOD", "function_calling")
        )
        return structured.invoke([("human", prompt)])
    except Exception as exc:  # noqa: BLE001  judge 失败不该让整个评测崩
        logger.warning("LLM-as-judge 调用失败,跳过该条打分: %s", exc)
        return None


# --------------------------------------------------------------------------
# 单条用例判定
# --------------------------------------------------------------------------


def evaluate_case(
    case: EvalCase,
    reported: list[Issue],
    judge_llm: Any = None,
) -> MatchOutcome:
    """对单条用例的一次审查结果做判定。

    参数:
        case: 用例(含标准答案)
        reported: 审查器报出的问题列表
        judge_llm: 传入则对命中项做 LLM-as-judge;None 表示只用规则匹配

    返回:
        MatchOutcome,含 TP/FP/FN、定位/级别命中、judge 打分明细
    """
    outcome = MatchOutcome(
        case_id=case.id,
        is_clean=case.is_clean,
        expected_total=len(case.expected),
        reported_total=len(reported),
    )

    matched_report_idx: set[int] = set()

    # 对每条标准答案,找一条尚未被占用、且规则匹配的报告
    for expected in case.expected:
        hit_idx: int | None = None
        for idx, issue in enumerate(reported):
            if idx in matched_report_idx:
                continue
            if not rule_match(issue, expected):
                continue
            # 可选:LLM-as-judge 语义复核,若判定语义不符则不算命中
            if judge_llm is not None:
                score = judge_match(judge_llm, issue, expected)
                if score is not None:
                    outcome.judge_scores.append(score)
                    if not score.semantic_match:
                        continue
            hit_idx = idx
            break

        if hit_idx is None:
            outcome.false_negatives += 1
            continue

        matched_report_idx.add(hit_idx)
        outcome.true_positives += 1
        issue = reported[hit_idx]
        # 定位准确率:命中项里行号也对得上的(expected.line>0 才计)
        if expected.line > 0:
            if abs(issue.line - expected.line) <= expected.tolerance:
                outcome.localization_hits += 1
        # 级别准确率:仅对标注了 severity 的标准答案统计
        if expected.severity is not None:
            outcome.severity_checked += 1
            hit = issue.severity == expected.severity
            if hit:
                outcome.severity_hits += 1
            # 记录逐项诊断:到底哪条期望 X 却报成了 Y,便于定位级别误判
            outcome.severity_detail.append({
                "type": issue.type or (expected.type_keywords[0] if expected.type_keywords else ""),
                "file": expected.file,
                "expected": expected.severity.value,
                "reported": issue.severity.value,
                "match": "✓" if hit else "✗",
            })

    # 没匹配上任何标准答案的报告 = 误报
    outcome.false_positives = len(reported) - len(matched_report_idx)
    return outcome
