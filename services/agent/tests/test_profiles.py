"""被测目标 profile 的工程正确性测试。

重点:profiles.yaml 解析、ad-hoc 合成(等价旧 --mode/--tools)、未知 profile 报错、
工具实际启用的降级判定。这些是确定性逻辑。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from evals.profiles import Profile, load_profiles, resolve_profile, tools_effective

_REAL_PROFILES = Path(__file__).resolve().parents[1] / "evals" / "profiles.yaml"


def _write_profiles(tmp_path) -> Path:
    p = tmp_path / "profiles.yaml"
    p.write_text(textwrap.dedent("""
        profiles:
          pipeline-notools:
            mode: pipeline
            tools: []
          pipeline-file:
            mode: pipeline
            tools: [get_file_content]
          custom-model:
            mode: pipeline
            tools: [get_file_content]
            model: some-model
    """), encoding="utf-8")
    return p


def test_load_profiles(tmp_path):
    profiles = load_profiles(_write_profiles(tmp_path))
    assert set(profiles) == {"pipeline-notools", "pipeline-file", "custom-model"}
    assert profiles["pipeline-file"].tools == ["get_file_content"]
    assert profiles["custom-model"].model == "some-model"


def test_resolve_named_profile(tmp_path):
    prof = resolve_profile("pipeline-file", path=_write_profiles(tmp_path))
    assert prof.mode == "pipeline"
    assert prof.wants_tools is True


def test_resolve_unknown_profile_raises(tmp_path):
    with pytest.raises(KeyError):
        resolve_profile("does-not-exist", path=_write_profiles(tmp_path))


def test_resolve_adhoc_from_tools_flag():
    # 不指定 --profile:用 --tools 合成 ad-hoc(管线 + 工具开/关)。
    notools = resolve_profile(None, tools=False)
    assert notools.mode == "pipeline" and notools.tools == [] and not notools.wants_tools

    pipe_tools = resolve_profile(None, tools=True)
    assert pipe_tools.mode == "pipeline"
    assert pipe_tools.tools == ["get_file_content"]
    assert pipe_tools.wants_tools is True


def test_wants_tools_only_pipeline_with_tools():
    assert Profile("x", mode="pipeline", tools=[]).wants_tools is False
    assert Profile("x", mode="pipeline", tools=["get_file_content"]).wants_tools is True


def test_tools_effective_degrades():
    prof = Profile("pipeline-file", mode="pipeline", tools=["get_file_content"])
    # 三者齐备才启用
    assert tools_effective(prof, has_llm=True, tool_server_url="http://x") is True
    # 缺 LLM(mock)→ 降级
    assert tools_effective(prof, has_llm=False, tool_server_url="http://x") is False
    # 缺工具服务地址 → 降级
    assert tools_effective(prof, has_llm=True, tool_server_url="") is False
    # profile 本就不想开工具 → 永远 False
    notools = Profile("pipeline-notools", mode="pipeline", tools=[])
    assert tools_effective(notools, has_llm=True, tool_server_url="http://x") is False


def test_shipped_profiles_valid():
    # 校验仓库里实际 profiles.yaml 的内置 profile。
    profiles = load_profiles(_REAL_PROFILES)
    assert {"pipeline-notools", "pipeline-file", "pipeline-repomap"} <= set(profiles)
    assert profiles["pipeline-file"].tools == ["get_file_content"]
    assert profiles["pipeline-notools"].tools == []
    assert profiles["pipeline-repomap"].mode == "pipeline"
