"""配置加载。

阶段 1 保持极简:所有配置从环境变量读取(可配合 .env 文件)。
后续阶段需要更复杂的配置(YAML、多层覆盖)时再演进,现在不要过度设计。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    """运行时配置。"""

    provider: str          # LLM 提供商:claude | openai | mock
    model: str             # 模型名
    api_key: str           # API 密钥
    api_base_url: str      # 自定义 API 地址(走代理时用),为空则用官方默认
    max_retries: int       # LLM 调用最大重试次数

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量构造配置。

        provider 默认 'mock':没配密钥时也能跑通整条流水线(返回假数据),
        方便阶段 0/1 先验证骨架是否通,再接真实 LLM。
        """
        provider = os.environ.get("CODEGUARD_PROVIDER", "mock").strip().lower()
        return cls(
            provider=provider,
            model=os.environ.get("CODEGUARD_MODEL", "claude-sonnet-4-20250514").strip(),
            api_key=os.environ.get("CODEGUARD_API_KEY", "").strip(),
            api_base_url=os.environ.get("CODEGUARD_API_BASE_URL", "").strip(),
            max_retries=int(os.environ.get("CODEGUARD_MAX_RETRIES", "3")),
        )
