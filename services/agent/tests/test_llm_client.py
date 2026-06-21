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
