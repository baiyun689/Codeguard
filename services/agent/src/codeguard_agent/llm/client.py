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
    """按模型端点生成关闭推理模式的请求参数。

    通义千问使用 enable_thinking=false；其他端点使用 thinking.type=disabled。
    该参数用于需要关闭推理模式才能进行强制工具调用的服务。
    """
    if "dashscope" in (api_base_url or "").lower():
        return {"enable_thinking": False}
    return {"thinking": {"type": "disabled"}}


def build_llm(settings: Settings, temperature: float | None = None) -> Any:
    """按配置创建 LangChain 聊天模型，统一提供模型调用接口。

    mock 模式返回 None，由调用方提供模拟结果。
    temperature 为 None 时使用服务商默认值，否则显式传入采样温度。
    """
    if settings.provider == "mock":
        return None

    # 请求模型前检查 API 密钥，缺失时返回配置错误。
    if settings.needs_api_key and not settings.api_key:
        raise ValueError(
            f"provider='{settings.provider}' 需要 API 密钥,但 CODEGUARD_API_KEY 为空。\n"
            "请在 .env 或环境变量中设置 CODEGUARD_API_KEY;"
            "若只想验证流水线连通,可设 CODEGUARD_PROVIDER=mock 走假数据。"
        )

    if settings.provider == "openai":
        # 延迟导入:没装对应包 / 用 mock 模式时不强制依赖
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = {
            "model": settings.model, "api_key": settings.api_key,
            "timeout": settings.llm_timeout_seconds, "max_retries": 0,
            "disable_streaming": True,
        }
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

        kwargs = {
            "model": settings.model, "api_key": settings.api_key,
            "timeout": settings.llm_timeout_seconds, "max_retries": 0,
            "disable_streaming": True,
        }
        if settings.api_base_url:
            kwargs["base_url"] = settings.api_base_url
        if temperature is not None:
            kwargs["temperature"] = temperature
        return ChatAnthropic(**kwargs)

    raise ValueError(f"不支持的 provider: {settings.provider}(可选:openai | claude | mock)")


def invoke_with_retry(llm: Any, messages: list[tuple[str, str]], max_retries: int = 3) -> Any:
    """调用模型并对可恢复错误执行指数退避重试。

    429、服务端错误及网络错误按 1 秒、2 秒、4 秒等间隔重试；
    其他 4xx 客户端错误直接抛出，不进行重试。
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
            if attempt + 1 >= max_retries:
                break
            wait = 2**attempt
            logger.warning("LLM 调用失败(第 %d 次),%ds 后重试: %s", attempt + 1, wait, exc)
            time.sleep(wait)
    raise RuntimeError(f"LLM 调用在 {max_retries} 次重试后仍失败") from last_error


def mock_review_result() -> ReviewResult:
    """生成模拟审查结果，用于无模型密钥时验证变更采集、审查和结果输出流程。"""
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
