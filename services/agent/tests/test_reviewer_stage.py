"""Default discoverer prompt configuration tests."""

from codeguard_agent.models.schemas import DiscoveryReviewResult
from codeguard_agent.models.tasks import ReviewTask
from codeguard_agent.pipeline.execution.engines import ReviewOutcome
from codeguard_agent.pipeline.orchestration import graph as graph_module
from codeguard_agent.pipeline.reviewers.reviewers import (
    DEFAULT_REVIEWERS,
    _load_prompt,
    build_reviewer_system_prompt,
)


def test_default_reviewers_point_to_base_prompt_files():
    names = {reviewer.name: reviewer.prompt_file for reviewer in DEFAULT_REVIEWERS}

    assert names["ThreatModelAgent"] == "threat-model-base.txt"
    assert names["BehaviorAgent"] == "behavior-base.txt"
    assert names["MaintainabilityAgent"] == "maintainability-base.txt"


def test_default_reviewers_share_all_fact_tools():
    expected = [
        "get_file_content",
        "inspect_structure",
        "inspect_change_impact",
        "inspect_path",
    ]
    assert all(reviewer.tool_allowlist == expected for reviewer in DEFAULT_REVIEWERS)


def test_system_prompts_include_shared_tool_contract_once():
    for reviewer in DEFAULT_REVIEWERS:
        prompt = build_reviewer_system_prompt(reviewer)
        assert prompt.count("## 共享图谱工具合同") == 1
        assert prompt.count("inspect_path") >= 3


def test_base_prompts_do_not_contain_knowledge_graph_heading():
    for filename in (
        "threat-model-base.txt",
        "behavior-base.txt",
        "maintainability-base.txt",
    ):
        assert "知识图谱" not in _load_prompt(filename)


def test_all_discovery_prompts_define_location_snippet_contract():
    prompts = [build_reviewer_system_prompt(reviewer) for reviewer in DEFAULT_REVIEWERS]
    prompts.append(_load_prompt("eval-direct-reviewer.txt"))
    for prompt in prompts:
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


def test_reviewer_subgraph_合法clean结果不再次直审(monkeypatch):
    class CleanEngine:
        def review(self, *_args, **_kwargs):
            return ReviewOutcome(
                DiscoveryReviewResult(summary="clean", issues=[]),
                execution_events=["react_inline_structured"],
            )

    class FailIfDirectLLM:
        def with_structured_output(self, *_args, **_kwargs):
            raise AssertionError("合法 clean 结果不应再次直审")

    monkeypatch.setattr(graph_module, "_make_engine", lambda *_args, **_kwargs: CleanEngine())
    reviewer = DEFAULT_REVIEWERS[1]
    subgraph = graph_module.build_reviewer_subgraph(
        reviewer,
        llm=FailIfDirectLLM(),
        tool_client=object(),
    )
    task = ReviewTask(
        id="task-1",
        file="src/A.java",
        patch="@@ -1 +1 @@\n-old\n+new",
        changed_lines=[1],
    )

    result = subgraph.invoke({
        "diff_text": task.patch,
        "review_task": task,
        "tier": "react",
        "review_tool_client": object(),
        "evidence_revision": "rev-1",
        "max_retries": 1,
        "structured_method": "function_calling",
        "task_scope": "current_hunk",
    })

    assert result["issues"] == []
    assert "react_inline_structured" in {
        trace.event for trace in result.get("council_trace", [])
    }
