"""任务构建阶段的 IDE 元数据过滤测试。"""

from codeguard_agent.pipeline.tasks.task_builder import (
    build_file_tasks,
    build_tasks,
    build_whole_diff_task,
)


def _diff(paths: list[str]) -> str:
    blocks = []
    for index, path in enumerate(paths, start=1):
        blocks.append(
            "\n".join(
                [
                    f"diff --git a/{path} b/{path}",
                    "index 0000000..1111111 100644",
                    f"--- a/{path}",
                    f"+++ b/{path}",
                    "@@ -0,0 +1 @@",
                    f"+changed_{index}",
                ]
            )
        )
    return "\n".join(blocks)


def test_ide_metadata_is_filtered_from_all_task_granularities() -> None:
    diff = _diff([
        ".idea/workspace.xml",
        "module.iml",
        ".classpath",
        ".project",
        "src/App.java",
    ])

    for builder in (build_tasks, build_file_tasks, build_whole_diff_task):
        tasks = builder(diff)
        assert [task.file for task in tasks] == ["src/App.java"]
        assert all(".idea" not in task.patch for task in tasks)
        assert all(".iml" not in task.patch for task in tasks)
        assert all(".classpath" not in task.patch for task in tasks)
        assert all(".project" not in task.patch for task in tasks)


def test_metadata_only_diff_produces_no_review_task() -> None:
    diff = _diff([
        ".idea/workspace.xml",
        "module.iml",
        ".classpath",
        ".project",
    ])

    assert build_tasks(diff) == []
    assert build_file_tasks(diff) == []
    assert build_whole_diff_task(diff) == []
