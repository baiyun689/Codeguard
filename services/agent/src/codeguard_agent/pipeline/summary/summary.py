"""变更摘要。

放在 ReviewCouncil 之前，用一次结构化 LLM 调用产出本次变更的摘要。
摘要只作为背景透传给后续节点，不再承担文件分派或风险分类职责。

健壮性（沿用本项目 None 防御惯例）：
- mock 模式不发起真实调用，直接跳过；
- LLM 调用失败 / 返回 None / 结果非法 → 一律退回"无摘要"路径，绝不中断管线。
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel

from codeguard_agent.llm.client import invoke_with_retry

logger = logging.getLogger("codeguard")

# prompts/ 目录在 codeguard_agent 包下（同 reviewers 的定位方式）。
_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"

class _DiffSummary(BaseModel):
    """摘要的结构化产出。"""

    summary: str = ""


def _load_prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


def _build_user_prompt(diff_text: str) -> str:
    """构造摘要的 user 消息，带提示注入防御（diff 包进标签、声明为数据非指令）。"""
    tpl = _load_prompt("summary-user.txt")
    return tpl.replace("{{diff}}", diff_text)


def build_diff_summary(
    diff_text: str,
    *,
    llm=None,
    max_retries: int = 3,
    structured_method: str = "function_calling",
) -> str:
    """生成变更摘要。

    这是一个显式输入/输出模块：失败、空响应或 mock 模式均返回空字符串，
    不通过可变共享上下文向调用者回写状态。
    """
    if not diff_text.strip():
        return ""

    # mock 模式:不发起真实调用,跳过摘要。
    if llm is None:
        logger.info("mock 模式，跳过摘要（不发起真实 LLM 调用）")
        return ""

    system = _load_prompt("summary-system.txt")
    user = _build_user_prompt(diff_text)

    structured_llm = llm.with_structured_output(
        _DiffSummary, method=structured_method
    )
    try:
        result = invoke_with_retry(
            structured_llm,
            [("system", system), ("human", user)],
            max_retries=max_retries,
        )
    except Exception as exc:  # noqa: BLE001 摘要失败不应拖垮整条管线
        logger.warning("摘要调用失败，退回无摘要路径：%s", exc)
        return ""

    # None 防御:结构化输出可能返回 None / 非预期类型 —— 退回无摘要路径。
    if result is None or not isinstance(result, _DiffSummary):
        logger.warning("摘要未返回有效结果，退回无摘要路径")
        return ""

    logger.info("[summary] 摘要长度=%d", len(result.summary))
    return result.summary
