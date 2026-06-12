"""聚合去重(AggregationStage)的工程正确性测试。

去重是确定性规则,适合用 pytest 死磕(对比 evals 量化的是不确定的 LLM 质量)。
"""

from __future__ import annotations

from codeguard_agent.models.schemas import Issue, Severity
from codeguard_agent.pipeline.stages.aggregation import deduplicate


def _issue(severity=Severity.WARNING, file="A.java", line=10, type="空指针",
           message="msg", confidence=1.0) -> Issue:
    return Issue(severity=severity, file=file, line=line, type=type,
                 message=message, confidence=confidence)


def test_同文件同行同类型_去重为一条():
    issues = [_issue(), _issue(message="措辞不同但同一处")]
    assert len(deduplicate(issues)) == 1


def test_去重保留最高severity():
    issues = [
        _issue(severity=Severity.INFO),
        _issue(severity=Severity.CRITICAL),
        _issue(severity=Severity.WARNING),
    ]
    out = deduplicate(issues)
    assert len(out) == 1
    assert out[0].severity == Severity.CRITICAL


def test_severity相同时保留更高confidence():
    issues = [
        _issue(severity=Severity.WARNING, confidence=0.4),
        _issue(severity=Severity.WARNING, confidence=0.9),
    ]
    out = deduplicate(issues)
    assert len(out) == 1
    assert out[0].confidence == 0.9


def test_不同行号视为不同问题_不去重():
    issues = [_issue(line=10), _issue(line=20)]
    assert len(deduplicate(issues)) == 2


def test_不同类型同行号_不去重():
    # 同一行可能同时有不同种类的问题,不应错误合并
    issues = [_issue(type="空指针"), _issue(type="资源泄漏")]
    assert len(deduplicate(issues)) == 2


def test_路径前缀不同但文件名相同_视为同处():
    # 不同审查员可能报不同前缀路径,按 basename 归一
    issues = [_issue(file="src/main/A.java"), _issue(file="A.java")]
    assert len(deduplicate(issues)) == 1


def test_无行号时退化按message去重():
    a = _issue(line=0, message="同样的问题描述")
    b = _issue(line=0, message="同样的问题描述")
    c = _issue(line=0, message="完全不同的问题")
    assert len(deduplicate([a, b, c])) == 2


def test_保留首次出现顺序():
    issues = [
        _issue(line=30, type="A"),
        _issue(line=10, type="B"),
        _issue(line=20, type="C"),
    ]
    out = deduplicate(issues)
    assert [i.line for i in out] == [30, 10, 20]


def test_空列表():
    assert deduplicate([]) == []
