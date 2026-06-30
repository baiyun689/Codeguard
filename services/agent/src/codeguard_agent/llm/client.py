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


def _is_non_retryable(exc: Exception) -> bool:
    """判断异常是否来自不可重试的客户端错误(400/401/402/422 等不重试;429/5xx/网络错误可重试)。

    尝试从异常链里取 HTTP 状态码——不直接 import openai/anthropic 的错误类型，
    避免 mock 模式 / 未装 SDK 时报导入错误。
    """
    status: int | None = None
    for attr in ("status_code", "http_status", "status"):
        s = getattr(exc, attr, None)
        if isinstance(s, int):
            status = s
            break
    # 沿 __cause__ 链深入一层(openai 库的 APIStatusError 常有 status_code)
    if status is None:
        cause = getattr(exc, "__cause__", None)
        if cause is not None:
            for attr in ("status_code", "http_status", "status"):
                s = getattr(cause, attr, None)
                if isinstance(s, int):
                    status = s
                    break
    if status is None:
        return False  # 无法判定 → 重试(宁可多试、不丢数据)
    # 429(Rate Limit) → 可重试;其余 4xx → 客户端错误,不重试
    if status == 429:
        return False
    if 400 <= status < 500:
        return True
    return False



def _disable_thinking_body(api_base_url: str) -> dict[str, Any]:
    """按厂商返回"关闭 thinking"的请求体字段——格式厂商相关,塞错家会被无视(关不掉)或报错。

    DeepSeek 与通义千问都借 `provider=openai` 这条路,但 base_url 不同、是两家:
    - 通义千问 / dashscope:``{"enable_thinking": false}``
    - DeepSeek(及默认):``{"thinking": {"type": "disabled"}}``

    背景:千问推理模型在 thinking 模式下不支持 ``tool_choice=required``(会 400),
    评测裁判走结构化输出必须先关 thinking;早先发的是 DeepSeek 格式,千问无视 → 关不掉 → 裁判全挂回退规则尺。
    """
    if "dashscope" in (api_base_url or "").lower():
        return {"enable_thinking": False}
    return {"thinking": {"type": "disabled"}}


def build_llm(settings: Settings, temperature: float | None = None) -> Any:
    """根据配置创建一个 LangChain Chat 模型。

    返回的对象都实现了 LangChain 的 BaseChatModel 接口,
    因此上层代码无需关心底层到底是 Claude 还是 OpenAI。

    provider='mock' 时返回 None,由调用方走假数据分支(见 reviewer.py)。

    temperature:显式传入时透传给底层模型;评测裁判用 temperature=0 锁住确定性,
        让"尺子"自身尽量不抖(见 ADR-005)。None 表示不设,用 provider 默认。
    """
    if settings.provider == "mock":
        return None

    # 调真实 API 前先校验密钥,缺失时给出清晰可操作的报错,
    # 而不是等到 invoke 时才抛一个晦涩的 401。
    if settings.needs_api_key and not settings.api_key:
        raise ValueError(
            f"provider='{settings.provider}' 需要 API 密钥,但 CODEGUARD_API_KEY 为空。\n"
            "请在 .env 或环境变量中设置 CODEGUARD_API_KEY;"
            "若只想验证流水线连通,可设 CODEGUARD_PROVIDER=mock 走假数据。"
        )

    if settings.provider == "openai":
        # 延迟导入:没装对应包 / 用 mock 模式时不强制依赖
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = {"model": settings.model, "api_key": settings.api_key}
        if settings.api_base_url:
            kwargs["base_url"] = settings.api_base_url
        if temperature is not None:
            kwargs["temperature"] = temperature
        # extra_body:合并 thinking 开关、推理深度等厂商扩展参数。
        extra: dict[str, Any] = {}
        if settings.disable_thinking:
            # 推理模型默认开启 thinking,会与 function_calling/结构化输出(裁判走 tool_choice=required)冲突。
            # 通过 extra_body 显式关闭;字段格式厂商相关(DeepSeek vs 千问),按 base_url 选对。
            # (真正的 OpenAI 不认此字段,故仅按需启用)
            extra.update(_disable_thinking_body(settings.api_base_url))
        if settings.reasoning_effort:
            # DeepSeek v4 推理深度:"high"(默认) | "max"。非 DeepSeek 端点静默无视。
            # 注:thinking mode 下 temperature/top_p 等均被静默无视;reasoning_effort 是独立轴。
            extra["reasoning_effort"] = settings.reasoning_effort
        if extra:
            kwargs["extra_body"] = extra
        return ChatOpenAI(**kwargs)

    if settings.provider == "claude":
        from langchain_anthropic import ChatAnthropic

        kwargs = {"model": settings.model, "api_key": settings.api_key}
        if settings.api_base_url:
            kwargs["base_url"] = settings.api_base_url
        if temperature is not None:
            kwargs["temperature"] = temperature
        return ChatAnthropic(**kwargs)

    raise ValueError(f"不支持的 provider: {settings.provider}(可选:openai | claude | mock)")


def invoke_with_retry(llm: Any, messages: list[tuple[str, str]], max_retries: int = 3) -> Any:
    """带指数退避重试的 LLM 调用。

    客户端错误(400/401/402/422)不重试——立刻抛断，避免余额不足/密钥错误白白消耗。
    429(限流)和 5xx/网络错误用指数退避重试(1s, 2s, 4s...)。

    阶段 5 再升级成熔断/限流的完整韧性体系。
    """
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            return llm.invoke(messages)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if _is_non_retryable(exc):
                logger.error(
                    "LLM 调用失败(客户端错误,不重试): status=%s, %s",
                    getattr(exc, "status_code", "?"), exc,
                )
                raise
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
