"""摘要 / 文件软分派阶段(前置软路由)。

放在并行审查之前,用**一次**结构化 LLM 调用产出本次变更的结构化摘要与文件分派 `file_focus`
(把每个改动文件映射到 security/logic/quality 一个或多个维度)。审查阶段据此为各审查员拼出
按域裁剪的 diff,缩小重叠面(见 design.md D1/D2)。

软路由,不门控(代码审查里漏报远比重复严重):
- 三个领域审查员**始终全跑**;file_groups 只用于裁剪各自看到的 diff,绝不据此跳过任何审查员。
- 未被分派到任一维度的改动文件,默认归入全部三个维度(保 recall 的兜底,见 _normalise_file_groups)。

健壮性(沿用本项目 None 防御惯例):
- mock 模式不发起真实调用,直接跳过(file_groups 留空 → 审查员吃整份 diff,行为同现状)。
- LLM 调用失败 / 返回 None / 结果非法 → 一律退回"无摘要"路径,绝不中断管线。

与现有 stage 一致是**同步** execute,不引入 async。
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from codeguard_agent.git.diff_collector import parse_changed_files
from codeguard_agent.llm.client import invoke_with_retry
from codeguard_agent.pipeline.stages.base import PipelineContext, PipelineStage

logger = logging.getLogger("codeguard")

# prompts/ 目录在 codeguard_agent 包下(同 reviewer_stage 的定位方式)。
_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"

# 三个领域审查员的规范名;file_focus 的键会被归一化到这三个之一。
_REVIEWER_NAMES = ("security", "logic", "quality")


class _DiffSummary(BaseModel):
    """摘要阶段的结构化产出。

    summary:2~4 句话的变更摘要(中文),作为背景透传给审查员。
    changed_files / change_types:变更文件与类型,主要用于日志与诊断。
    estimated_risk_level:1~5 的风险等级。
    file_focus:reviewer 维度 → 相关文件路径列表(软路由的依据)。
    """

    summary: str = ""
    changed_files: list[str] = Field(default_factory=list)
    change_types: list[str] = Field(default_factory=list)
    estimated_risk_level: int = Field(default=3, ge=1, le=5)
    file_focus: dict[str, list[str]] = Field(default_factory=dict)


def _load_prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


def _build_user_prompt(diff_text: str) -> str:
    """构造摘要阶段的 user 消息,带提示注入防御(diff 包进标签、声明为数据非指令)。"""
    tpl = _load_prompt("summary-user.txt")
    return tpl.replace("{{diff}}", diff_text)


def _normalise_file_groups(
    file_focus: dict[str, list[str]],
    all_files: list[str],
) -> dict[str, list[str]]:
    """把 LLM 给出的 file_focus 归一化为 {security/logic/quality: [文件...]}。

    规则:
    - 键模糊匹配到三个规范维度名(sec*→security,log*→logic,qual*→quality)。
    - 只保留确实出现在本次变更里的文件(all_files),过滤掉 LLM 臆造的路径。
    - **未被任何维度分派到的文件,默认归入全部三个维度**(保 recall 的兜底)。
    - file_focus 为空 / 全部无效时,退化为"所有文件发给所有审查员"。
    """
    groups: dict[str, list[str]] = {name: [] for name in _REVIEWER_NAMES}
    valid = set(all_files)

    assigned: set[str] = set()
    for key, paths in (file_focus or {}).items():
        key_lower = (key or "").lower()
        if "sec" in key_lower:
            matched = "security"
        elif "log" in key_lower:
            matched = "logic"
        elif "qual" in key_lower:
            matched = "quality"
        else:
            continue
        for p in paths or []:
            if p in valid and p not in groups[matched]:
                groups[matched].append(p)
                assigned.add(p)

    # 未分派的文件(含 LLM 完全没给分派的情况)默认发给所有审查员。
    for f in all_files:
        if f not in assigned:
            for name in _REVIEWER_NAMES:
                if f not in groups[name]:
                    groups[name].append(f)

    return groups


class SummaryStage(PipelineStage):
    """前置摘要 / 文件软分派阶段。"""

    @property
    def name(self) -> str:
        return "summary"

    def execute(self, context: PipelineContext) -> PipelineContext:
        diff_text = context.diff_text
        if not diff_text.strip():
            return context

        # mock 模式:不发起真实调用,跳过分派。file_groups 留空 → 审查员吃整份 diff。
        if context.llm is None:
            logger.info("mock 模式,跳过摘要阶段(不发起真实 LLM 调用)")
            return context

        changed_files = parse_changed_files(diff_text)
        system = _load_prompt("summary-system.txt")
        user = _build_user_prompt(diff_text)

        structured_llm = context.llm.with_structured_output(
            _DiffSummary, method=context.structured_method
        )
        try:
            result = invoke_with_retry(
                structured_llm,
                [("system", system), ("human", user)],
                max_retries=context.max_retries,
            )
        except Exception as exc:  # noqa: BLE001 摘要失败不应拖垮整条管线
            logger.warning("摘要阶段调用失败,退回无摘要路径: %s", exc)
            return context

        # None 防御:结构化输出可能返回 None / 非预期类型 —— 退回无摘要路径。
        if result is None or not isinstance(result, _DiffSummary):
            logger.warning("摘要阶段未返回有效结果,退回无摘要路径")
            return context

        context.diff_summary = result.summary
        context.change_types = result.change_types
        context.risk_level = result.estimated_risk_level
        context.file_groups = _normalise_file_groups(result.file_focus, changed_files)
        logger.info(
            "管线阶段 [summary]:%d 个文件,风险=%d,分派=%s",
            len(changed_files),
            context.risk_level,
            {k: len(v) for k, v in context.file_groups.items()},
        )
        return context
