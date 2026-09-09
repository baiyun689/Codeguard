from pathlib import Path
from codeguard_agent.models.tasks import ReviewerKind, ReviewTask, TaskRoute
from codeguard_agent.pipeline.orchestration.graph import _task_route_node
from codeguard_agent.pipeline.tasks.task_builder import classify_task_route


def _task(task_id: str, file: str, patch: str) -> ReviewTask:
    return ReviewTask(id=task_id, file=file, patch=patch)


def test_task_route_is_conservative_for_code_and_allows_documentation():
    assert (
        classify_task_route(_task("README.md#h0", "README.md", "+clarify usage")).route
        == "direct"
    )
    assert (
        classify_task_route(
            _task("A.java#h0", "A.java", "+if (authorized) save();")
        ).route
        == "full"
    )


def test_task_route_node_emits_task_level_routes():
    tasks = [
        _task("README.md#h0", "README.md", "+docs"),
        _task("A.java#h0", "A.java", "+return value;"),
    ]
    output = _task_route_node()({"review_tasks": tasks})
    assert output["task_routes"]["README.md#h0"].route == "direct"
    assert output["task_routes"]["A.java#h0"].route == "full"
