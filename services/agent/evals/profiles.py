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
    mode: str = "pipeline"          # 当前仅 pipeline(基线 single 已移除)
    tools: list[str] = field(default_factory=list)  # 启用的工具名,如 ["get_file_content"]
    model: str | None = None        # 可选模型覆盖;None 表示沿用全局 Settings 的模型
    fp_verify: bool = False         # 是否启用误报过滤第二段的独立 LLM 复核(对照的独立变量)
    # supervisor 智能调度开关。默认 **关**:受控对照档(notools/file/repomap)保持确定性全派,
    # 不引入额外非确定性 LLM 决策、保住"工具是唯一变量"的纯净度(见 design D9)。
    # 仅 pipeline-supervisor 观测档显式置开,单独画像调度行为。
    enable_supervisor: bool = False

    @property
    def wants_tools(self) -> bool:
        """该 profile 是否意图启用工具(pipeline + 非空工具集才有意义)。"""
        return self.mode == "pipeline" and bool(self.tools)


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
            enable_supervisor=bool(cfg.get("enable_supervisor", False)),
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
        name=f"adhoc-{mode}{'-tools' if tools else ''}",
        mode=mode,
        tools=["get_file_content"] if tools else [],
        fp_verify=False,  # ad-hoc 档默认不开复核;要对照复核请用具名 profile
    )


def tools_effective(profile: Profile, *, has_llm: bool, tool_server_url: str) -> bool:
    """工具是否真正启用:profile 想开工具 + 有真实 LLM + 配了工具服务地址,三者齐备。

    任一不满足即降级为无工具(评测照常进行,只是 file 等能力测不出增益)。
    抽成纯函数便于单测,也让 runner 据它如实记录"工具实际启用状态"。
    """
    return profile.wants_tools and has_llm and bool(tool_server_url)


def case_repo_root(case_repo_path: str | None, repo_base: str | None) -> str | None:
    """该用例供工具读取的**真实** repo 根;没有则返回 None(=本条不建工具会话)。

    - repo-backed 用例自带 `repo_path`(磁盘上的工程快照)→ 直接用它。
    - 合成内联用例无 `repo_path`:**仅当**用户显式传了 `--repo-base`(断言这些 diff 对应某真实
      工程)才用它;否则返回 None。

    绝不隐式回退到 cwd:cwd 是 agent 自己的源码树(且恰好含 `evals/dataset/repo/**` 夹具),
    对它建工具会话会让 `get_repo_map`/`get_file_content` 返回**真实但与本 diff 完全无关**的内容,
    诱使 ReAct 审查员在无关文件间无界乱逛、永不收尾,直到撞 `recursion_limit` 失败——这正是
    ADR-016 里 clean/complex 合成用例在工具档下 ~40% 审查员失败、recall 崩塌的根因。
    """
    if case_repo_path:
        return case_repo_path
    if repo_base:
        return os.path.abspath(repo_base)
    return None
