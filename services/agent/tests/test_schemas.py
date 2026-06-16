"""数据模型与 mock 流程的基础测试。

阶段 1 先覆盖最核心的数据结构。路线图提醒:测试是活文档,
看不懂某个模块时,它的测试会告诉你输入输出长什么样。
"""

from codeguard_agent.llm.client import mock_review_result
from codeguard_agent.models.schemas import Issue, Severity


def test_issue_默认值():
    """Issue 的可选字段应有合理默认值。"""
    issue = Issue(severity=Severity.WARNING, file="A.java", type="空指针", message="可能 NPE")
    assert issue.line == 0
    assert issue.suggestion == ""
    assert issue.confidence == 1.0


def test_confidence_边界约束():
    """confidence 必须在 0~1 之间,越界应报错。"""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Issue(severity=Severity.INFO, file="A.java", type="x", message="y", confidence=1.5)


def test_mock_流程连通():
    """mock 模式应能产出一条示例问题,证明数据流是通的。"""
    result = mock_review_result()
    assert len(result.issues) == 1
    assert result.issues[0].severity == Severity.WARNING
