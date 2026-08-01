"""长时间真实评测的逐案例断点恢复接缝。"""

from codeguard_agent.models.schemas import ReviewResult

from evals.runner import _strict_tool_failures, run_once
from evals.schema import EvalCase, MatchOutcome


def test_run_once_skips_checkpointed_cases_and_preserves_dataset_order() -> None:
    cases = [
        EvalCase(id="first", category="clean", diff="diff-1"),
        EvalCase(id="second", category="clean", diff="diff-2"),
    ]
    existing = [MatchOutcome(case_id="first", is_clean=True)]
    reviewed: list[str] = []
    checkpoints: list[list[str]] = []

    def review(case):
        reviewed.append(case.id)
        return ReviewResult(summary=""), [], {}

    outcomes = run_once(
        cases,
        review,
        None,
        existing_outcomes=existing,
        on_checkpoint=lambda rows: checkpoints.append([row.case_id for row in rows]),
    )

    assert reviewed == ["second"]
    assert [outcome.case_id for outcome in outcomes] == ["first", "second"]
    assert checkpoints == [["first", "second"]]


def test_strict_tool_profile_allows_policy_selected_direct_tasks() -> None:
    failures, warnings = _strict_tool_failures(
        [type("Trace", (), {"status": "failed", "tool": "inspect_structure", "content": ""})()],
        {
            "context_diagnostics": {"symbol_context": "graph_coverage_partial"},
            "council": {
                "react_degraded_empty_count": 1,
                "direct_tier_task_count": 2,
            },
        },
    )

    assert "symbol_context:graph_coverage_partial" in failures
    assert "tool_failed:inspect_structure" in failures
    assert "react_degraded_empty_count=1" in failures
    assert "direct_tier_task_count=2" not in failures
    assert warnings == []


def test_strict_tool_agent_misuse_is_warning_not_fatal() -> None:
    # 沙箱护栏拒绝(agent 传目录/白名单外路径)属于 agent 误用,工具侧正常,不中断。
    failures, warnings = _strict_tool_failures(
        [
            type("Trace", (), {
                "status": "failed",
                "tool": "get_file_content",
                "content": "Error: 文件类型不可读(仅限源码文件): src/.../references",
            })(),
        ],
        {"context_diagnostics": {}, "council": {}},
    )

    assert failures == []
    assert warnings == ["tool_rejected:get_file_content"]
