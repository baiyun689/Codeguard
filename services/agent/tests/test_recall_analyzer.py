"""历史回放分析器的命中口径测试。"""

from evals.recall_analyzer import evidence_match, match


def _bug() -> dict:
    return {
        "file": "src/Entry.java",
        "line": 10,
        "tolerance": 5,
        "desc": "状态传播 Store clear",
        "evidence_anchors": ["Store.java:30"],
        "evidence_scope": "local",
    }


def test_回放命中不再依赖证据位置():
    issue = {
        "file": "src/Entry.java",
        "line": 10,
        "message": "状态传播",
        "summary": "",
    }

    assert match(issue, _bug()) is True
    assert evidence_match(issue, _bug()) is False


def test_回放仍可单独统计证据覆盖():
    issue = {
        "file": "src/Entry.java",
        "line": 10,
        "message": "状态传播",
        "summary": "",
        "evidence_locations": [
            {
                "file": "src/Store.java",
                "symbol": "Store#clear()",
                "start_line": 30,
                "end_line": 42,
            }
        ],
    }

    assert match(issue, _bug()) is True
    assert evidence_match(issue, _bug()) is True
