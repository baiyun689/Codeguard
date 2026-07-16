"""大 diff 降级策略在主编排图中的集成测试。"""

from __future__ import annotations

from codeguard_agent.models.schemas import ReviewResult
from codeguard_agent.models.tasks import (
    ReviewBudget,
    ReviewTask,
    RiskProfile,
    RiskTag,
    TaskSelection,
)
from codeguard_agent.pipeline import graph as G
from codeguard_agent.pipeline.engines import ReviewOutcome
from codeguard_agent.pipeline.orchestrator import PipelineOrchestrator


def _tasks(count: int = 51) -> list[ReviewTask]:
    return [
        ReviewTask(
            id=f"F{i}.java#h0",
            file=f"F{i}.java",
            hunk_header="@@ -1 +1 @@",
            patch=f"@@ -1 +1 @@\n-old{i}\n+selected{i}",
            changed_lines=[1],
        )
        for i in range(count)
    ]


def _diff(tasks: list[ReviewTask]) -> str:
    return "\n".join(
        f"diff --git a/{task.file} b/{task.file}\n"
        f"--- a/{task.file}\n+++ b/{task.file}\n{task.patch}"
        for task in tasks
    )


def test_task_rank_applies_large_diff_budget_and_emits_trace():
    tasks = _tasks()
    profiles = {task.id: RiskProfile(task_id=task.id) for task in tasks}

    out = G._task_rank_node()(
        {
            "diff_text": _diff(tasks),
            "review_tasks": tasks,
            "risk_profiles": profiles,
            "review_budget": ReviewBudget(),
        }
    )

    assert len(out["task_selection"].selected_task_ids) == 20
    assert any(trace.event == "large_diff_degraded" for trace in out["council_trace"])


def test_summary_receives_only_selected_task_scope(monkeypatch):
    tasks = _tasks()
    captured: dict[str, str] = {}

    def capture(stage, context):
        captured["diff"] = context.diff_text
        context.diff_summary = "summary"
        return context

    monkeypatch.setattr(G.SummaryStage, "execute", capture)
    out = G._summary_node(None, None)(
        {
            "diff_text": _diff(tasks),
            "review_tasks": tasks,
            "task_selection": TaskSelection(selected_task_ids=[tasks[0].id]),
            "review_budget": ReviewBudget(),
        }
    )

    assert out["diff_summary"] == "summary"
    assert "selected0" in captured["diff"]
    assert "selected1" not in captured["diff"]


class _Response:
    success = True
    error = ""

    def __init__(self, content: str) -> None:
        self.content = content

    def as_tool_output(self) -> str:
        return self.content


class _ToolClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def find_sensitive_apis(self):
        self.calls.append(("find_sensitive_apis", ""))
        return _Response("sensitive")

    def get_diff_ast(self, diff_text: str):
        self.calls.append(("get_diff_ast", diff_text))
        return _Response("AST for: F0.java\n  class: F0")


def test_context_provider_large_diff_uses_selected_scope_and_skips_broad_scan():
    tasks = _tasks()
    client = _ToolClient()

    G._context_provider_node(client)(
        {
            "diff_text": _diff(tasks),
            "review_tasks": tasks,
            "risk_profiles": {task.id: RiskProfile(task_id=task.id) for task in tasks},
            "task_selection": TaskSelection(selected_task_ids=[tasks[0].id]),
            "review_budget": ReviewBudget(),
        }
    )

    assert not any(name == "find_sensitive_apis" for name, _ in client.calls)
    ast_query = next(value for name, value in client.calls if name == "get_diff_ast")
    assert "selected0" in ast_query
    assert "selected1" not in ast_query


def test_large_diff_reviewer_includes_bounded_patch_only_once(monkeypatch):
    tasks = _tasks()
    tasks[0] = tasks[0].model_copy(update={"patch": "+" + "x" * 20_000})
    prompts: list[str] = []

    class CaptureEngine:
        def review(self, *args, **kwargs):
            prompts.append(kwargs["user_prompt"])
            return ReviewOutcome(ReviewResult(summary=""))

    monkeypatch.setattr(G, "_make_engine", lambda state, tool_client=None: CaptureEngine())
    reviewer = G.DEFAULT_REVIEWERS[0]
    task = tasks[0]
    G.make_reviewer_node(reviewer, llm=object())(
        {
            "diff_text": _diff(tasks),
            "review_tasks": tasks,
            "risk_profiles": {
                task.id: RiskProfile(
                    task_id=task.id,
                    tag_scores={RiskTag.GENERAL_REVIEW: 1},
                )
            },
            "task_selection": TaskSelection(selected_task_ids=[task.id]),
            "review_budget": ReviewBudget(),
        }
    )

    assert len(prompts) == 1
    assert prompts[0].count("大 diff 单任务 patch 已截断") == 1
    assert prompts[0].count("x") < 12_100


def test_summary_runs_after_task_selection_in_graph():
    graph = G.build_review_graph(enable_summary=True, llm=None).get_graph()
    edges = {(edge.source, edge.target) for edge in graph.edges}

    assert ("task_rank", "summary") in edges
    assert ("summary", "context_provider") in edges
    assert ("__start__", "summary") not in edges


def test_large_diff_result_discloses_partial_coverage():
    tasks = _tasks()

    result = PipelineOrchestrator(enable_summary=False).run(None, _diff(tasks))

    assert "大变更降级审查" in result.summary
    assert "共 51 个任务" in result.summary
    assert "审查 20 个" in result.summary
    assert "不代表完整覆盖" in result.summary
