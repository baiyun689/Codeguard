"""Default discoverer prompt configuration tests."""

from codeguard_agent.pipeline.reviewers.reviewers import (
    DEFAULT_REVIEWERS,
    _load_prompt,
)


def test_default_reviewers_point_to_base_prompt_files():
    names = {reviewer.name: reviewer.prompt_file for reviewer in DEFAULT_REVIEWERS}

    assert names["ThreatModelAgent"] == "threat-model-base.txt"
    assert names["BehaviorAgent"] == "behavior-base.txt"
    assert names["MaintainabilityAgent"] == "maintainability-base.txt"


def test_base_prompts_do_not_contain_knowledge_graph_heading():
    for filename in (
        "threat-model-base.txt",
        "behavior-base.txt",
        "maintainability-base.txt",
    ):
        assert "知识图谱" not in _load_prompt(filename)


def test_all_discovery_prompts_define_location_snippet_contract():
    for filename in (
        "threat-model-base.txt",
        "behavior-base.txt",
        "maintainability-base.txt",
        "eval-direct-reviewer.txt",
    ):
        prompt = _load_prompt(filename)
        assert "location_snippet" in prompt
        assert "新增行" in prompt
        assert "1～5 行" in prompt
        assert "不属于" in prompt and "证据" in prompt


def test_relocation_prompt_is_a_restricted_location_contract():
    prompt = _load_prompt("candidate-relocation.txt")

    assert "代码候选定位助手" in prompt
    assert "candidate_id" in prompt
    assert "location_snippet" in prompt
    assert "不得输出行号" in prompt
    assert "severity" in prompt
    assert "keep/drop" in prompt
    assert "无法确定时返回空字符串" in prompt
    assert "禁止猜测" in prompt
