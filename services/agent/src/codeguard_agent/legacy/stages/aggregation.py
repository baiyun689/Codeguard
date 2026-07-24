"""聚合阶段(两段式:规则去重 + LLM 语义综合)。

把多个并行审查员产出的 issues 合并成一份干净结果。沿用本项目"先确定性、再 LLM"的惯例
(同 FalsePositiveFilterStage):

- **第一段(规则去重,零成本、可复现)**:按 文件+行号+type 精确指纹合并完全重复项,
  同一指纹保留最高 severity(severity 相同则保留 confidence 更高的那条)。

- **第二段(LLM 语义综合,新增)**:第一段的精确规则抓不住"同一处问题、不同审查员、措辞不同、
  **行号相邻**"的同源重复(这正是 18→18 去重失效的根因)。第二段把第一段结果喂给一次 LLM,
  让它只判断"哪些 issue 指向同一底层问题",再由**确定性代码**按其分组合并。

第二段的关键纪律(见 design.md D3):
- **不臆造新问题**:LLM 只输出"分组",最终 issue 一律从第一段的原始对象里挑,绝不采纳 LLM
  生成的新文本——从结构上杜绝凭空造问题。
- **保守合并**:只合并明显同源;有疑问就分别保留(不强行并不同问题)。
- **失败回退**:LLM 返回 None / 不可解析 / 合并后反而变多 → 回退第一段结果并告警(沿用 None 防御)。
- 与现有 stage 一致是**同步** execute,不引入 async。

本阶段只去重/合并,**不删误报**(那是误报过滤阶段的事),以便分别量化各段收益。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from pydantic import BaseModel, Field

from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.models.schemas import Issue, Severity
from codeguard_agent.pipeline.context.base import PipelineContext, PipelineStage

logger = logging.getLogger("codeguard")

_SEVERITY_RANK = {Severity.CRITICAL: 3, Severity.WARNING: 2, Severity.INFO: 1}

_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"


def _basename(path: str) -> str:
    """取文件名(忽略目录与大小写),不同审查员可能报不同前缀的路径。"""
    return (path or "").replace("\\", "/").rsplit("/", 1)[-1].strip().lower()


def _norm_text(text: str) -> str:
    """归一化文本:折叠空白、转小写、截断,抵消无意义的措辞差异。"""
    return re.sub(r"\s+", " ", text or "").strip().lower()[:160]


def _dedup_key(issue: Issue) -> tuple:
    """构造去重指纹。

    有行号(line>0)时:同一 文件+行号+type 视为重复(最强信号);
    无行号时:退化为 文件+type+归一化message,避免把无法定位的不同问题误并。
    """
    file_key = _basename(issue.file)
    type_key = (issue.type or "").strip().lower()
    if issue.line and issue.line > 0:
        return (file_key, issue.line, type_key)
    return (file_key, type_key, _norm_text(issue.message))


def _better(a: Issue, b: Issue) -> Issue:
    """同一指纹下选更值得保留的一条:先比 severity,再比 confidence。"""
    ra, rb = _SEVERITY_RANK.get(a.severity, 0), _SEVERITY_RANK.get(b.severity, 0)
    if ra != rb:
        return a if ra > rb else b
    return a if a.confidence >= b.confidence else b


def deduplicate(issues: list[Issue]) -> list[Issue]:
    """跨审查员去重,保留首次出现顺序。"""
    best: dict[tuple, Issue] = {}
    order: list[tuple] = []
    for issue in issues:
        key = _dedup_key(issue)
        if key not in best:
            best[key] = issue
            order.append(key)
        else:
            best[key] = _better(best[key], issue)
    return [best[key] for key in order]


# ---------------------------------------------------------------------------
# 第二段:LLM 语义综合
# ---------------------------------------------------------------------------


class _MergeGroup(BaseModel):
    """一组指向同一底层问题的 issue 序号(从 1 开始,对应喂给 LLM 的编号清单)。"""

    members: list[int] = Field(
        default_factory=list,
        description="同一处问题的 issue 序号集合(1-based);只把确属同源的放进同一组",
    )


class _MergePlan(BaseModel):
    """LLM 的合并方案:若干分组。未出现在任何分组里的 issue 保持独立。"""

    groups: list[_MergeGroup] = Field(default_factory=list)


def _format_issues(issues: list[Issue]) -> str:
    """把待综合的 issues 编成带序号的清单(1-based),供 LLM 判断同源关系。"""
    lines = []
    for i, it in enumerate(issues, 1):
        lines.append(
            f"[{i}] 文件={it.file} 行={it.line} 类型={it.type} "
            f"级别={it.severity.value} 置信度={it.confidence:.2f}\n    描述:{it.message}"
        )
    return "\n".join(lines)


def _apply_merge_plan(issues: list[Issue], plan: _MergePlan) -> list[Issue]:
    """按 LLM 给出的分组,确定性地合并 issues。

    纪律:
    - 序号越界、不足 2 个有效成员的分组一律忽略(不构成合并)。
    - 一个 issue 至多属于一个最终分组(先到先得,避免重叠分组互相吞)。
    - 每组保留"更值得留"的那条(_better:先 severity 再 confidence),在该组**最早成员**的位置输出,
      尽量贴近原始顺序与位置。
    - 不属于任何有效分组的 issue 原样保留。
    返回的每一条都来自入参 issues 的原始对象——绝不产生新问题。
    """
    n = len(issues)
    rep_for: dict[int, int] = {}  # 成员下标(0-based) → 该组代表的下标
    used: set[int] = set()

    for group in plan.groups:
        members = []
        for m in group.members:
            idx = m - 1  # 1-based → 0-based
            if 0 <= idx < n and idx not in used and idx not in members:
                members.append(idx)
        if len(members) < 2:
            continue
        best = members[0]
        for m in members[1:]:
            if _better(issues[best], issues[m]) is issues[m]:
                best = m
        for m in members:
            used.add(m)
            rep_for[m] = best

    result: list[Issue] = []
    emitted: set[int] = set()
    for i in range(n):
        if i in rep_for:
            rep = rep_for[i]
            if rep not in emitted:
                result.append(issues[rep])
                emitted.add(rep)
        else:
            result.append(issues[i])
    return result


def _load_prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


class AggregationStage(PipelineStage):
    """聚合阶段:规则去重(第一段)+ LLM 语义综合(第二段)。

    参数:
        enable_llm_synthesis: 是否启用第二段 LLM 语义综合(默认开)。关闭或无可用 LLM(mock)、
            或第一段后剩余 < 2 条时,只跑第一段规则去重。
    """

    def __init__(self, enable_llm_synthesis: bool = True) -> None:
        self._enable_llm_synthesis = enable_llm_synthesis
        self._prompt_cache: tuple[str, str] | None = None

    @property
    def name(self) -> str:
        return "aggregation"

    def execute(self, context: PipelineContext) -> PipelineContext:
        before = len(context.issues)

        # 第一段:确定性规则去重(零成本、可复现)。
        deduped = deduplicate(context.issues)
        after_rules = len(deduped)

        # 第二段:LLM 语义综合(仅在启用、有真实 LLM、且还剩 ≥2 条时跑)。
        issues = deduped
        if self._enable_llm_synthesis and context.llm is not None and len(deduped) >= 2:
            issues = self._llm_synthesize(context, deduped)

        context.issues = issues
        logger.info(
            "管线阶段 [aggregation]:规则去重 %d → %d,LLM 综合 → %d 条",
            before, after_rules, len(issues),
        )
        return context

    def _llm_synthesize(self, context: PipelineContext, issues: list[Issue]) -> list[Issue]:
        """第二段:让 LLM 给出同源分组,再确定性合并。任何异常/无效输出一律回退第一段结果。"""
        if self._prompt_cache is None:
            self._prompt_cache = (
                _load_prompt("aggregation-system.txt"),
                _load_prompt("aggregation-user.txt"),
            )
        system, user_tpl = self._prompt_cache
        user = (
            user_tpl
            .replace("{{summary}}", context.diff_summary or "(无变更摘要)")
            .replace("{{issues}}", _format_issues(issues))
        )

        structured_llm = context.llm.with_structured_output(
            _MergePlan, method=context.structured_method
        )
        try:
            plan = invoke_with_retry(
                structured_llm,
                [("system", system), ("human", user)],
                max_retries=context.max_retries,
            )
        except Exception as exc:  # noqa: BLE001 综合失败不该丢真问题
            logger.warning("[aggregation] LLM 综合调用失败,回退规则去重结果: %s", exc)
            return issues

        # None 防御:结构化输出可能返回 None / 非预期类型 —— 回退。
        if plan is None or not isinstance(plan, _MergePlan):
            logger.warning("[aggregation] LLM 综合未返回有效结果,回退规则去重结果")
            return issues

        merged = _apply_merge_plan(issues, plan)
        # 安全阀:综合只应"合并",结果不该为空、也不该比输入更多(那意味着臆造/出错)。
        if not merged or len(merged) > len(issues):
            logger.warning("[aggregation] LLM 综合结果异常(%d→%d),回退规则去重结果",
                           len(issues), len(merged))
            return issues
        return merged
