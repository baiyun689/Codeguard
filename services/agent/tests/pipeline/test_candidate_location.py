"""候选定位护栏的公共接口测试。"""

import json

from codeguard_agent.models.schemas import DiscoveredIssue, Severity
from codeguard_agent.models.tasks import ReviewTask
from codeguard_agent.pipeline.location import locate_issues


class _FakeRelocator:
    def __init__(self, result):
        self.result = result
        self.calls = 0
        self.payloads = []

    def with_structured_output(self, _schema, method=None):
        return self

    def invoke(self, messages):
        self.calls += 1
        self.payloads.append(json.loads(messages[1][1]))
        return self.result


def _task() -> ReviewTask:
    return ReviewTask(
        id="src/A.java#h1",
        file="src/A.java",
        hunk_header="@@ -8,3 +8,4 @@",
        patch=(
            "diff --git a/src/A.java b/src/A.java\n"
            "--- a/src/A.java\n"
            "+++ b/src/A.java\n"
            "@@ -8,3 +8,4 @@\n"
            " class A {\n"
            "+    execute(userInput);\n"
            "     return;\n"
            " }\n"
        ),
        changed_lines=[9],
    )


def _issue(**updates) -> DiscoveredIssue:
    values = {
        "severity": Severity.WARNING,
        "file": "src/A.java",
        "line": 99,
        "location_snippet": "    execute(userInput);",
        "type": "命令注入",
        "message": "外部输入直接进入命令执行",
        "suggestion": "使用参数化执行接口",
        "confidence": 0.9,
    }
    values.update(updates)
    return DiscoveredIssue(**values)


def test_unique_added_snippet_corrects_reported_line_without_llm():
    batch = locate_issues(
        [_issue()],
        _task(),
        llm=None,
        structured_method="function_calling",
        max_retries=1,
    )

    assert batch.issues[0].line == 9
    assert batch.issues[0].location_snippet == "    execute(userInput);"
    assert batch.records[0].status == "corrected"
    assert batch.records[0].original_line == 99


def test_valid_added_line_is_accepted_when_snippet_is_missing():
    batch = locate_issues(
        [_issue(line=9, location_snippet="")],
        _task(),
        llm=None,
        structured_method="function_calling",
        max_retries=1,
    )

    assert batch.issues[0].line == 9
    assert batch.records[0].status == "verified"
    assert batch.records[0].reason == "reported_added_line"


def test_context_line_is_not_a_valid_inline_location():
    batch = locate_issues(
        [_issue(line=10, location_snippet="    return;")],
        _task(),
        llm=None,
        structured_method="function_calling",
        max_retries=1,
    )

    assert batch.issues[0].line == 0
    assert batch.records[0].status == "file_level"


def test_leading_blank_line_is_not_trimmed_from_snippet():
    batch = locate_issues(
        [_issue(line=99, location_snippet="\n    execute(userInput);")],
        _task(),
        llm=None,
        structured_method="function_calling",
        max_retries=1,
    )

    assert batch.issues[0].line == 0
    assert batch.records[0].status == "file_level"


def test_unresolved_issues_share_one_batch_relocation_call():
    llm = _FakeRelocator({
        "locations": [
            {
                "candidate_id": "L001",
                "location_snippet": "    execute(userInput);",
            },
            {
                "candidate_id": "L002",
                "location_snippet": "    execute(userInput);",
            },
        ]
    })

    batch = locate_issues(
        [
            _issue(line=88, location_snippet="missingOne();"),
            _issue(line=89, location_snippet="missingTwo();"),
        ],
        _task(),
        llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )

    assert llm.calls == 1
    assert len(llm.payloads[0]["candidates"]) == 2
    assert [issue.line for issue in batch.issues] == [9, 9]
    assert [record.status for record in batch.records] == ["relocated", "relocated"]


def test_whole_diff_matches_snippet_within_candidate_file():
    task = ReviewTask(
        id="whole-diff",
        file="<whole-diff>",
        patch=(
            "diff --git a/src/A.java b/src/A.java\n"
            "--- a/src/A.java\n"
            "+++ b/src/A.java\n"
            "@@ -1 +1 @@\n"
            "+    run();\n"
            "diff --git a/src/B.java b/src/B.java\n"
            "--- a/src/B.java\n"
            "+++ b/src/B.java\n"
            "@@ -20 +20 @@\n"
            "+    run();\n"
        ),
        changed_lines=[],
    )

    batch = locate_issues(
        [_issue(file="src/B.java", line=99, location_snippet="    run();")],
        task,
        llm=None,
        structured_method="function_calling",
        max_retries=1,
    )

    assert batch.issues[0].line == 20
    assert batch.records[0].status == "corrected"


def test_ambiguous_snippet_and_invalid_relocation_stays_file_level():
    task = ReviewTask(
        id="src/A.java#file",
        file="src/A.java",
        patch=(
            "diff --git a/src/A.java b/src/A.java\n"
            "--- a/src/A.java\n"
            "+++ b/src/A.java\n"
            "@@ -1,2 +1,2 @@\n"
            "+    run();\n"
            "+    run();\n"
        ),
        changed_lines=[1, 2],
    )
    llm = _FakeRelocator({
        "locations": [{"candidate_id": "L001", "location_snippet": "    run();"}]
    })

    batch = locate_issues(
        [_issue(line=99, location_snippet="    run();")],
        task,
        llm=llm,
        structured_method="function_calling",
        max_retries=1,
    )

    assert llm.calls == 1
    assert batch.issues[0].line == 0
    assert batch.records[0].status == "file_level"
    assert [event for event, _detail in batch.trace].count("relocation_completed") == 1
    assert [event for event, _detail in batch.trace].count("location_unresolved") == 1
