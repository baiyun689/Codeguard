"""审查器(阶段 1 版本:单次直接 LLM 调用)。

这是 Codeguard 的"无 Agent 基准版":diff 进来,一次 LLM 调用,结构化结果出去。
没有工具调用、没有多轮迭代——这是故意的。

⚠️ 路线图提醒:请保留这个基准。到阶段 3 加入工具调用 Agent 后,
要用同一个 diff 对比"有工具 vs 无工具"的审查质量差异,
那是整个项目最关键的一次"体感 Agent 是什么"的实验。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from codeguard_agent.llm.client import invoke_with_retry, mock_review_result
from codeguard_agent.models.schemas import ReviewResult

logger = logging.getLogger("codeguard")

# 提示词文件目录(prompts/ 与本文件同属一个包)
_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    """从 prompts/ 目录加载提示词模板。

    把提示词单独放文件、而非写死在代码里,是为了:
    1. 改提示词不用动代码;2. 提示词本身就是"每个审查员意图"的最佳文档。
    """
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


def review(
    llm: Any,
    diff_text: str,
    max_retries: int = 3,
    structured_method: str = "function_calling",
) -> ReviewResult:
    """对一段 diff 执行安全审查。

    参数:
        llm: LangChain Chat 模型;为 None 时走 mock 模式
        diff_text: unified diff 文本
        max_retries: LLM 调用重试次数
        structured_method: 结构化输出方式。默认 function_calling(工具调用),
            兼容性最好;DeepSeek 等不支持 json_schema(response_format)的端点必须用它。

    返回:
        结构化的 ReviewResult
    """
    if not diff_text.strip():
        return ReviewResult(summary="没有检测到代码变更,无需审查。")

    # mock 模式:不调真实 LLM,直接返回假数据,验证流水线连通性
    if llm is None:
        logger.info("当前为 mock 模式,返回示例审查结果")
        return mock_review_result()

    system_prompt = _load_prompt("security.txt")
    user_prompt = f"请审查以下代码变更(diff):\n\n{diff_text}"

    # with_structured_output:让 LLM 直接吐出符合 ReviewResult schema 的结构化结果,
    # 省去自己解析 JSON 的麻烦。这是 LangChain 提供的关键能力。
    # method 决定底层机制:function_calling(工具调用,兼容性最好)/ json_schema(OpenAI 原生,
    # DeepSeek 不支持)/ json_mode。默认走 function_calling,避免打到不支持 json_schema 的端点。
    structured_llm = llm.with_structured_output(ReviewResult, method=structured_method)
    result = invoke_with_retry(
        structured_llm,
        [("system", system_prompt), ("human", user_prompt)],
        max_retries=max_retries,
    )

    # with_structured_output 可能返回 None:当模型那一轮没有正确发起工具调用、
    # 或没吐出可解析的结构化结果时(function_calling 模式下尤其常见,DeepSeek 等
    # 工具调用稳定性较弱的端点更容易触发)。这里兜底成空结果,避免把 None 抛给
    # 上层(CLI 打印、评测管线都会因 None.issues 崩溃)。
    if result is None:
        logger.warning("LLM 未返回可解析的结构化结果,本次按'无问题'处理(可能是模型未发起工具调用)")
        return ReviewResult(summary="模型未返回结构化结果,本次审查为空。")

    return result
