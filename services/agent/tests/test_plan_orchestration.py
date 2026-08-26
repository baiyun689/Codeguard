from pathlib import Path

from codeguard_agent.models.tasks import (
    ReviewerKind,
    ReviewTask,
    TaskAgentPlan,
    TaskRoute,
)
from codeguard_agent.pipeline.orchestration.graph import _task_route_node
from codeguard_agent.pipeline.knowledge.catalog import KnowledgeCatalog
from codeguard_agent.pipeline.knowledge.selector import select_knowledge
from codeguard_agent.models.knowledge import KnowledgeBudget
from codeguard_agent.pipeline.planning import build_plan_units, validate_plan
from codeguard_agent.pipeline.tasks.task_builder import classify_task_route


def _task(task_id: str, file: str, patch: str) -> ReviewTask:
    return ReviewTask(id=task_id, file=file, patch=patch)


def test_task_route_is_conservative_for_code_and_allows_documentation():
    assert classify_task_route(
        _task("README.md#h0", "README.md", "+clarify usage")
    ).route == "direct"
    assert classify_task_route(
        _task("A.java#h0", "A.java", "+if (authorized) save();")
    ).route == "full"


def test_large_plan_units_are_reused_per_file():
    tasks = [
        _task("A.java#h0", "A.java", "+a"),
        _task("A.java#h1", "A.java", "+b"),
        _task("B.java#h0", "B.java", "+c"),
    ]
    routes = {task.id: TaskRoute(task_id=task.id, route="full") for task in tasks}
    units = build_plan_units(tasks, routes, review_mode="large")
    assert [unit.task_ids for unit in units] == [("A.java#h0", "A.java#h1"), ("B.java#h0",)]


def test_plan_validation_rejects_cross_reviewer_topics():
    plan = TaskAgentPlan(
        plan_unit_id="A.java",
        reviewers=(ReviewerKind.THREAT_MODEL,),
        reviewer_plans=({
            "reviewer": "threat_model",
            "objectives": ["检查新增管理入口的授权边界"],
            "knowledge_topics": ["COMPLEXITY_CONTROL_FLOW", "AUTHORIZATION"],
        },),
    )
    validated, diagnostics = validate_plan(
        plan,
        plan_unit_id="A.java",
        catalog=KnowledgeCatalog(),
    )
    assert validated.reviewers == (ReviewerKind.THREAT_MODEL,)
    assert validated.reviewer_plans[0].knowledge_topics == ("AUTHORIZATION",)
    assert any("invalid_topic" in item for item in diagnostics)


def test_plan_prompt_requires_minimal_reviewer_set_and_selection_thresholds():
    prompt = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "codeguard_agent"
        / "prompts"
        / "review-plan.txt"
    ).read_text(encoding="utf-8")

    assert "最小充分 Reviewer 集合" in prompt
    assert "ThreatModelAgent 的选中门槛" in prompt
    assert "BehaviorAgent 的选中门槛" in prompt
    assert "MaintainabilityAgent 的选中门槛" in prompt
    assert ".idea" in prompt
    assert "仅仅因为工具可能发现隐藏问题" in prompt
    assert "不要为了显得全面而默认选择全部 Reviewer" in prompt
    assert "一个 Reviewer 足以覆盖当前变更时只选择一个" in prompt
    assert "普通业务计算" in prompt
    assert "不改变运行时行为的构建描述" in prompt
    assert "任何代码都可以更易维护" in prompt


def test_explicit_plan_topics_are_selected():
    bundle = select_knowledge(
        reviewer=ReviewerKind.THREAT_MODEL,
        requested_topics=("AUTHORIZATION",),
        catalog=KnowledgeCatalog(),
        budget=KnowledgeBudget(),
    )
    assert [item.fragment.topic for item in bundle.specialized] == ["AUTHORIZATION"]


def test_task_route_node_emits_task_level_routes():
    tasks = [
        _task("README.md#h0", "README.md", "+docs"),
        _task("A.java#h0", "A.java", "+return value;"),
    ]
    output = _task_route_node()({"review_tasks": tasks})
    assert output["task_routes"]["README.md#h0"].route == "direct"
    assert output["task_routes"]["A.java#h0"].route == "full"
