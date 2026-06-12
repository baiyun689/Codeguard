"""把"审查器报出的 Issue"对到"标准答案",产出 TP/FP/FN 判定。

判分有两把尺,核心动作都是**配对**(哪条报告命中哪条标准答案),配对定下来后
TP/FP/FN/定位/级别 全由代码确定性算出(_build_outcome):

    1. 规则尺(_rule_pairing):file + 行号邻近 + 类型关键词,贪心配对。零成本、确定、
       可复现、可 pytest。缺点是"关键词撞对但其实不是一回事"会错配,偏乐观。
    2. 裁判尺(_llm_pairing → judge_case):案例级一次 LLM 调用,把全部报告对到全部
       标准答案,做**双向语义配对**(能捞回关键词没撞上的真命中,也能踢掉撞词的假命中)。

evaluate_case 的策略(见 ADR-005):
    - vuln 用例 + 传了裁判:LLM 主判;裁判失败/返回空则回退规则尺。
    - clean 用例:标准答案为空,报出来的按定义全是误报,**不调裁判**(省那次最易被
      自我评判偏差污染的调用)。
    - 无论是否用裁判,都顺手算一遍规则尺结果存进 rule_* 字段,供报告做交叉校验。

一条标准答案最多由一条报告命中,一条报告最多命中一条标准答案;没被任何标准答案认领的
报告计为 FP。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from codeguard_agent.models.schemas import Issue

from evals.schema import CaseJudgement, EvalCase, ExpectedIssue, MatchOutcome

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
# 规则尺:贪心配对
# --------------------------------------------------------------------------


def _rule_pairing(case: EvalCase, reported: list[Issue]) -> dict[int, int]:
    """规则尺配对:对每条标准答案,贪心找第一条尚未占用且规则匹配的报告。

    返回 {expected_idx: reported_idx},只含命中项。
    """
    pairing: dict[int, int] = {}
    used: set[int] = set()
    for exp_idx, expected in enumerate(case.expected):
        for idx, issue in enumerate(reported):
            if idx in used:
                continue
            if rule_match(issue, expected):
                pairing[exp_idx] = idx
                used.add(idx)
                break
    return pairing


# --------------------------------------------------------------------------
# 裁判尺:案例级 LLM 语义配对
# --------------------------------------------------------------------------

_JUDGE_CASE_PROMPT = """你是代码审查评测的裁判,只做一件事:把"审查器报出的问题"对到"标准答案"。

下面给你一段代码变更(diff)、它的标准答案(这段代码**确实存在**的问题清单)、以及审查器报出的问题。

判定规则:
- 只有当报出的问题与某条标准答案指的是**同一个根因问题、且位置大致一致**时,才算命中。
- 仅仅类型词面撞上、但实际说的不是一回事,**不算命中**。
- 一条标准答案最多由一条报告命中;一条报告最多命中一条标准答案。
- 宁可判漏报,不可为凑数而错配。

diff:
{diff}

标准答案([E#] 是编号):
{expected}

审查器报出的问题([R#] 是编号):
{reported}

请对**每一条标准答案**输出一条 match:expected_id 填它的编号,reported_id 填命中它的报告编号\
(找不到则填 -1),reason 简述判定依据。"""


def _fmt_expected(case: EvalCase) -> str:
    rows = []
    for i, e in enumerate(case.expected):
        loc = f"{e.file}:{e.line}" if e.line > 0 else e.file
        rows.append(
            f"[E{i}] 关键词:{'、'.join(e.type_keywords)} | 位置:{loc} | 说明:{e.note or '(无)'}"
        )
    return "\n".join(rows)


def _fmt_reported(reported: list[Issue]) -> str:
    rows = []
    for j, x in enumerate(reported):
        loc = f"{x.file}:{x.line}" if x.line else x.file
        rows.append(f"[R{j}] 类型:{x.type} | 位置:{loc} | 描述:{x.message}")
    return "\n".join(rows)


def judge_case(
    judge_llm: Any, case: EvalCase, reported: list[Issue]
) -> CaseJudgement | None:
    """案例级裁判:一次 LLM 调用,把全部报告语义配对到全部标准答案。

    judge_llm 为 None 时返回 None(mock / 未开裁判)。调用失败也返回 None,由上层回退规则尺。
    """
    if judge_llm is None:
        return None
    prompt = _JUDGE_CASE_PROMPT.format(
        diff=case.diff,
        expected=_fmt_expected(case),
        reported=_fmt_reported(reported),
    )
    try:
        structured = judge_llm.with_structured_output(
            CaseJudgement,
            method=os.environ.get("CODEGUARD_STRUCTURED_METHOD", "function_calling"),
        )
        return structured.invoke([("human", prompt)])
    except Exception as exc:  # noqa: BLE001  裁判失败不该让整个评测崩
        logger.warning("[%s] 案例级裁判调用失败,回退规则尺: %s", case.id, exc)
        return None


def _llm_pairing(
    judge_llm: Any, case: EvalCase, reported: list[Issue]
) -> dict[int, int] | None:
    """把裁判输出收敛成干净的 {expected_idx: reported_idx} 配对。

    对裁判可能给出的脏数据做防御:越界编号、重复认领同一条报告、重复给同一标准答案配对,
    一律丢弃(取首次有效的)。完全拿不到判定时返回 None,交由上层回退规则尺。
    """
    judgement = judge_case(judge_llm, case, reported)
    if judgement is None:
        return None
    pairing: dict[int, int] = {}
    used: set[int] = set()
    for m in judgement.matches:
        if not (0 <= m.expected_id < len(case.expected)):
            continue
        if m.expected_id in pairing:  # 同一标准答案重复出现,取首次
            continue
        rid = m.reported_id
        if rid is None or rid < 0 or rid >= len(reported) or rid in used:
            continue  # -1/越界/重复认领 → 视为该标准答案漏报
        pairing[m.expected_id] = rid
        used.add(rid)
    return pairing


# --------------------------------------------------------------------------
# 由配对确定性算出判定结果
# --------------------------------------------------------------------------


def _build_outcome(
    case: EvalCase,
    reported: list[Issue],
    pairing: dict[int, int],
    judged_by: str,
) -> MatchOutcome:
    """给定配对,确定性地算出 TP/FP/FN、定位命中、级别命中与逐项级别诊断。"""
    outcome = MatchOutcome(
        case_id=case.id,
        is_clean=case.is_clean,
        expected_total=len(case.expected),
        reported_total=len(reported),
        primary_judge=judged_by,
    )
    matched_reports: set[int] = set()
    for exp_idx, expected in enumerate(case.expected):
        rep_idx = pairing.get(exp_idx, -1)
        if rep_idx < 0 or rep_idx >= len(reported) or rep_idx in matched_reports:
            outcome.false_negatives += 1
            continue
        matched_reports.add(rep_idx)
        outcome.true_positives += 1
        issue = reported[rep_idx]
        # 定位准确率:命中项里行号也对得上的(expected.line>0 才计)
        if expected.line > 0 and abs(issue.line - expected.line) <= expected.tolerance:
            outcome.localization_hits += 1
        # 级别准确率:仅对标注了 severity 的标准答案统计
        if expected.severity is not None:
            outcome.severity_checked += 1
            hit = issue.severity == expected.severity
            if hit:
                outcome.severity_hits += 1
            outcome.severity_detail.append({
                "type": issue.type or (expected.type_keywords[0] if expected.type_keywords else ""),
                "file": expected.file,
                "expected": expected.severity.value,
                "reported": issue.severity.value,
                "match": "✓" if hit else "✗",
            })
    outcome.false_positives = len(reported) - len(matched_reports)
    return outcome


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
        judge_llm: 传入则 vuln 用例走 LLM 主判;None 表示只用规则尺

    返回:
        MatchOutcome(主判结果),并附带规则尺交叉校验数(rule_* 字段)。
    """
    rule_pairing = _rule_pairing(case, reported)

    # clean 用例报出来的按定义全是误报,无需裁判;vuln 用例且有报告时才值得调裁判
    use_llm = judge_llm is not None and not case.is_clean and case.expected and reported
    pairing = _llm_pairing(judge_llm, case, reported) if use_llm else None
    judged_by = "llm" if pairing is not None else "rule"
    if pairing is None:
        pairing = rule_pairing

    outcome = _build_outcome(case, reported, pairing, judged_by)

    # 交叉校验:无论主判是谁,都记一份规则尺的 TP/FP/FN,供报告对比分歧
    rule_outcome = _build_outcome(case, reported, rule_pairing, "rule")
    outcome.rule_true_positives = rule_outcome.true_positives
    outcome.rule_false_positives = rule_outcome.false_positives
    outcome.rule_false_negatives = rule_outcome.false_negatives
    return outcome
