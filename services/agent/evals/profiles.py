"""被测目标 profile:把"用什么配置跑评测"显式化、可插拔。

一个 profile = mode(pipeline) + 启用的工具集 + 可选模型覆盖 + 是否启用误报复核。
核心思想(design.md D3):**被测系统与评测标准解耦**——加一个工具、换一种编排、
开关误报复核,都只表现为新增/调整一个 profile,数据集与指标定义零改动。

未指定 profile 时,用 --tools 合成一个 ad-hoc profile(管线 + 工具开/关)。
"""

from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
import yaml

_PROFILES_FILE = Path(__file__).resolve().parent / "profiles.yaml"


@dataclass
class Profile:
    """一个被测目标的配置。"""

    name: str
    mode: str = "pipeline"
    tools: list[str] = field(default_factory=list)
    model: str | None = None
    fp_verify: bool = False
    orchestration: str = "change-review"
    execution: str = "pipeline"
    evidence_tools: list[str] | None = None
    strict_tools: bool = False
    evidence_mode: str = "full"
    discovery_mode: str | None = None

    @property
    def wants_tools(self) -> bool:
        """该 profile 是否意图启用工具(pipeline + 非空工具集才有意义)。"""
        return (
            self.execution == "pipeline"
            and self.mode == "pipeline"
            and bool(self.tools)
        )


def load_profiles(path: Path | None = None) -> dict[str, Profile]:
    """加载 profiles.yaml,返回 {name: Profile}。文件不存在则返回空表。"""
    src = path or _PROFILES_FILE
    if not src.is_file():
        return {}
    raw = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
    profiles: dict[str, Profile] = {}
    for name, cfg in (raw.get("profiles") or {}).items():
        cfg = cfg or {}
        profiles[name] = Profile(
            name=name,
            mode=cfg.get("mode", "pipeline"),
            tools=list(cfg.get("tools") or []),
            model=cfg.get("model"),
            fp_verify=bool(cfg.get("fp_verify", False)),
            orchestration=cfg.get("orchestration", "change-review"),
            execution=cfg.get("execution", "pipeline"),
            evidence_tools=list(cfg.get("evidence_tools") or [])
            if "evidence_tools" in cfg
            else None,
            strict_tools=bool(cfg.get("strict_tools", False)),
            evidence_mode=cfg.get("evidence_mode", "full"),
            discovery_mode=cfg.get("discovery_mode"),
        )
    return profiles


def resolve_profile(
    name: str | None,
    *,
    mode: str = "pipeline",
    tools: bool = False,
    path: Path | None = None,
) -> Profile:
    """解析被测目标。

    - 指定 name:从 profiles.yaml 取;找不到则报错并列出可选项。
    - 未指定:用 --tools 合成一个 ad-hoc profile(管线 + 工具开/关)。
    """
    if name:
        profiles = load_profiles(path)
        if name not in profiles:
            avail = ", ".join(sorted(profiles)) or "(无)"
            raise KeyError(f"未知 profile {name!r};可选:{avail}")
        return profiles[name]
    return Profile(
        name=f"adhoc-{mode}{('-tools' if tools else '')}",
        mode=mode,
        tools=["read_symbol"] if tools else [],
        fp_verify=False,
    )


def tools_effective(profile: Profile, *, has_llm: bool, tool_server_url: str) -> bool:
    """工具是否真正启用:profile 想开工具 + 有真实 LLM + 配了工具服务地址,三者齐备。

    任一不满足即降级为无工具(评测照常进行,只是 file 等能力测不出增益)。
    抽成纯函数便于单测,也让 runner 据它如实记录"工具实际启用状态"。
    """
    return profile.wants_tools and has_llm and bool(tool_server_url)


def case_repo_root(case_repo_path: str | None, repo_base: str | None) -> str | None:
    """返回用例对应的真实仓库路径，供工具创建查询会话。

    优先使用用例的 repo_path；内联用例仅在显式指定 repo_base 时使用该目录。
    缺少仓库路径时返回 None，不回退到当前工作目录。
    """
    if case_repo_path:
        return case_repo_path
    if repo_base:
        return os.path.abspath(repo_base)
    return None
