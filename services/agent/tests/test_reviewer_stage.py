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
