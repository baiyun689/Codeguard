"""build_llm 的 disable-thinking 请求体格式按厂商分派(harness 修复:千问 thinking 模式拒 tool_choice)。"""

from __future__ import annotations

from codeguard_agent.llm.client import _disable_thinking_body


def test_dashscope_uses_enable_thinking_flag():
    # 通义千问 / dashscope:enable_thinking=false(早先误发 DeepSeek 格式导致裁判全挂回退规则尺)。
    body = _disable_thinking_body("https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert body == {"enable_thinking": False}


def test_deepseek_uses_thinking_disabled_object():
    body = _disable_thinking_body("https://api.deepseek.com")
    assert body == {"thinking": {"type": "disabled"}}


def test_empty_base_url_defaults_to_deepseek_format():
    assert _disable_thinking_body("") == {"thinking": {"type": "disabled"}}


def test_provider_has_explicit_timeout_without_nested_sdk_retries(monkeypatch):
    import langchain_openai
    from codeguard_agent.config import Settings
    from codeguard_agent.llm.client import build_llm

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", lambda **kwargs: kwargs)
    options = build_llm(Settings(
        provider="openai", model="test", api_key="test", api_base_url="",
        max_retries=3, structured_method="function_calling", disable_thinking=False,
        llm_timeout_seconds=17,
    ))
    assert options["timeout"] == 17
    assert options["max_retries"] == 0
    assert options["disable_streaming"] is True


def test_single_attempt_does_not_sleep_after_failure(monkeypatch):
    import pytest
    from types import SimpleNamespace
    from codeguard_agent.llm import client

    sleeps = []
    monkeypatch.setattr(client.time, "sleep", sleeps.append)
    def fail(_messages):
        raise TimeoutError("request timed out")
    with pytest.raises(RuntimeError):
        client.invoke_with_retry(SimpleNamespace(invoke=fail), [], max_retries=1)
    assert sleeps == []
