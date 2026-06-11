"""配置加载。

阶段 1 保持极简:所有配置从环境变量读取(可配合 .env 文件)。
后续阶段需要更复杂的配置(YAML、多层覆盖)时再演进,现在不要过度设计。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# 各 provider 的默认模型:用户不显式指定 CODEGUARD_MODEL 时按 provider 回退到对应默认值,
# 避免出现"provider=openai 却用着 claude 模型名"的错配。
_DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "claude": "claude-sonnet-4-20250514",
}


def _load_dotenv() -> None:
    """从项目里就近向上查找并加载 .env 文件。

    设计要点:
    - override=False:已显式设置的环境变量优先于 .env,方便临时覆盖。
    - 没装 python-dotenv 时静默跳过,不影响"纯环境变量"用法。
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return
    # usecwd=True:从当前工作目录向上找,无论在仓库哪一层运行都能定位到 .env。
    load_dotenv(find_dotenv(usecwd=True), override=False)


@dataclass
class Settings:
    """运行时配置。"""

    provider: str           # LLM 提供商:openai | claude | mock
    model: str              # 模型名
    api_key: str            # API 密钥
    api_base_url: str       # 自定义 API 地址(走代理时用),为空则用官方默认
    max_retries: int        # LLM 调用最大重试次数
    structured_method: str  # 结构化输出方式:function_calling | json_schema | json_mode
    disable_thinking: bool  # 是否禁用思考模式(DeepSeek 等推理模型需要)

    @property
    def needs_api_key(self) -> bool:
        """是否为需要真实 API 密钥的 provider(mock 不需要)。"""
        return self.provider in _DEFAULT_MODELS

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量构造配置(会先就近加载 .env 文件)。

        provider 默认 'openai':开箱即用调真实 API。
        想零成本验证流水线连通时,可显式设 CODEGUARD_PROVIDER=mock 走假数据分支。
        """
        _load_dotenv()
        provider = os.environ.get("CODEGUARD_PROVIDER", "openai").strip().lower()
        # 模型名:用户没指定时,按 provider 回退到该 provider 的默认模型。
        model = os.environ.get("CODEGUARD_MODEL", "").strip() or _DEFAULT_MODELS.get(provider, "")
        # 结构化输出方式默认 function_calling:兼容性最好(OpenAI/DeepSeek/Anthropic 都支持)。
        # 注意:DeepSeek 等不支持 OpenAI 的 json_schema(response_format),用 function_calling 才能跑通。
        structured_method = os.environ.get(
            "CODEGUARD_STRUCTURED_METHOD", "function_calling"
        ).strip()
        # 是否禁用思考模式。DeepSeek 的推理模型(thinking 模式)与 function_calling/结构化输出
        # 冲突,需要显式关闭。默认 false:真正的 OpenAI 不认这个字段,发了反而会报错。
        disable_thinking = os.environ.get(
            "CODEGUARD_DISABLE_THINKING", "false"
        ).strip().lower() in ("1", "true", "yes", "on")
        return cls(
            provider=provider,
            model=model,
            api_key=os.environ.get("CODEGUARD_API_KEY", "").strip(),
            api_base_url=os.environ.get("CODEGUARD_API_BASE_URL", "").strip(),
            max_retries=int(os.environ.get("CODEGUARD_MAX_RETRIES", "3")),
            structured_method=structured_method,
            disable_thinking=disable_thinking,
        )
