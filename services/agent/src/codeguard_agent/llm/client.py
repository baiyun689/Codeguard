"""LLM 客户端工厂 + 重试封装。

职责:根据配置创建对应的 LLM(Claude / OpenAI / Mock),
并提供一个带重试的统一调用入口,屏蔽不同提供商的差异。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from codeguard_agent.config import Settings
from codeguard_agent.models.schemas import Issue, ReviewResult, Severity

logger = logging.getLogger("codeguard")


def build_llm(settings: Settings) -> Any:
    """根据配置创建一个 LangChain Chat 模型。

    返回的对象都实现了 LangChain 的 BaseChatModel 接口,
    因此上层代码无需关心底层到底是 Claude 还是 OpenAI。

    provider='mock' 时返回 None,由调用方走假数据分支(见 reviewer.py)。
    """
    if settings.provider == "mock":
        return None

    if settings.provider == "claude":
        # 延迟导入:没装对应包 / 用 mock 模式时不强制依赖
        from langchain_anthropic import ChatAnthropic

        kwargs: dict[str, Any] = {"model": settings.model, "api_key": settings.api_key}
        if settings.api_base_url:
            kwargs["base_url"] = settings.api_base_url
        return ChatAnthropic(**kwargs)

    if settings.provider == "openai":
        from langchain_openai import ChatOpenAI

        kwargs = {"model": settings.model, "api_key": settings.api_key}
        if settings.api_base_url:
            kwargs["base_url"] = settings.api_base_url
        return ChatOpenAI(**kwargs)

    raise ValueError(f"不支持的 provider: {settings.provider}")


def invoke_with_retry(llm: Any, messages: list[tuple[str, str]], max_retries: int = 3) -> Any:
    """带指数退避重试的 LLM 调用。

    LLM API 偶发超时/限流很常见,简单重试能显著提升稳定性。
    这里用最朴素的指数退避(1s, 2s, 4s...),阶段 5 再升级成熔断/限流的完整韧性体系。
    """
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            return llm.invoke(messages)
        except Exception as exc:  # noqa: BLE001 阶段1先粗粒度兜底,后续再细分异常类型
            last_error = exc
            wait = 2**attempt
            logger.warning("LLM 调用失败(第 %d 次),%ds 后重试: %s", attempt + 1, wait, exc)
            time.sleep(wait)
    raise RuntimeError(f"LLM 调用在 {max_retries} 次重试后仍失败") from last_error


def mock_review_result() -> ReviewResult:
    """mock 模式下返回的假审查结果。

    作用:让整条流水线在没有真实 API 密钥时也能跑通,
    方便阶段 0/1 验证"读 diff → 审查 → 输出"的骨架是否打通。
    """
    return ReviewResult(
        summary="【Mock 模式】这是一条假的审查结果,用于验证流水线是否打通。配置 CODEGUARD_API_KEY 后接入真实 LLM。",
        issues=[
            Issue(
                severity=Severity.WARNING,
                file="example/Demo.java",
                line=42,
                type="示例问题",
                message="这是 mock 模式生成的示例问题,证明数据流是通的。",
                suggestion="配置真实 LLM 后,这里会是模型给出的真实建议。",
                confidence=0.5,
            )
        ],
    )
