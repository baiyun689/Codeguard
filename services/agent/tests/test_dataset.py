"""数据集加载与 EvalCase 能力标签的工程正确性测试。

重点:repo-backed 用例正确加载(diff 注入 + repo_path 指向快照)、旧合成用例零改动仍加载、
能力标签缺省与归一化。这些是确定性逻辑,该死磕。
"""

from __future__ import annotations

import textwrap

from evals.dataset import load_cases
from evals.schema import EvalCase


def _write(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


# --------- 能力标签(EvalCase) ---------

def test_capability_defaults_to_diff_only():
    case = EvalCase(id="c1", category="clean", diff="x")
    assert case.capability == ["diff-only"]
    assert case.is_repo_backed is False


def test_capability_normalises_and_drops_invalid():
    case = EvalCase(id="c1", category="clean", diff="x", capability=["File", "bogus", "file"])
    # 大小写归一、去重、丢掉非法值
    assert case.capability == ["file"]


def test_capability_empty_falls_back_to_diff_only():
    case = EvalCase(id="c1", category="clean", diff="x", capability=[])
    assert case.capability == ["diff-only"]


def test_capability_all_invalid_falls_back():
    case = EvalCase(id="c1", category="clean", diff="x", capability=["nope", "xxx"])
    assert case.capability == ["diff-only"]


# --------- 数据集加载 ---------

def test_loads_synthetic_cases_unchanged(tmp_path):
    _write(tmp_path / "vuln" / "001.yaml", """
        id: v1
        category: SQL注入
        diff: |
          some diff
        expected:
          - type_keywords: ["sql"]
            file: A.java
            line: 10
    """)
    _write(tmp_path / "clean" / "001.yaml", """
        id: c1
        category: clean
        diff: |
          clean diff
        expected: []
    """)
    cases = load_cases(tmp_path)
    assert [c.id for c in cases] == ["c1", "v1"]
    v1 = next(c for c in cases if c.id == "v1")
    assert v1.capability == ["diff-only"]  # 旧用例缺省
    assert v1.is_repo_backed is False


def test_loads_repo_backed_case(tmp_path):
    case_dir = tmp_path / "repo" / "file_001"
    _write(case_dir / "case.yaml", """
        id: rb1
        category: 路径穿越
        capability: [file]
        expected:
          - type_keywords: ["traversal"]
            file: FileController.java
            line: 12
    """)
    _write(case_dir / "changes.diff", "diff --git a/X b/X\n+evil\n")
    _write(case_dir / "repo" / "src" / "Sanitizer.java", "class Sanitizer {}\n")

    cases = load_cases(tmp_path)
    assert len(cases) == 1
    rb = cases[0]
    assert rb.id == "rb1"
    assert rb.is_repo_backed is True
    assert rb.diff == "diff --git a/X b/X\n+evil\n"  # diff 由 changes.diff 注入
    assert rb.repo_path.endswith("repo")  # 指向快照目录
    assert (tmp_path / "repo" / "file_001" / "repo").samefile(rb.repo_path)
    assert rb.capability == ["file"]


def test_repo_backed_defaults_capability_to_file(tmp_path):
    case_dir = tmp_path / "repo" / "file_002"
    _write(case_dir / "case.yaml", """
        id: rb2
        category: clean
        expected: []
    """)
    _write(case_dir / "changes.diff", "diff\n")
    _write(case_dir / "repo" / "A.java", "class A {}\n")
    cases = load_cases(tmp_path)
    assert cases[0].capability == ["file"]


def test_synthetic_and_repo_backed_coexist(tmp_path):
    _write(tmp_path / "vuln" / "001.yaml", """
        id: v1
        category: SQL注入
        diff: "d"
        expected: []
    """)
    case_dir = tmp_path / "repo" / "file_001"
    _write(case_dir / "case.yaml", "id: rb1\ncategory: clean\nexpected: []\n")
    _write(case_dir / "changes.diff", "d\n")
    _write(case_dir / "repo" / "A.java", "class A {}\n")

    cases = load_cases(tmp_path)
    assert sorted(c.id for c in cases) == ["rb1", "v1"]


def test_repo_yaml_inside_snapshot_not_picked_as_case(tmp_path):
    # 快照工程里若含 application.yaml,绝不能被当成一条用例。
    _write(tmp_path / "vuln" / "001.yaml", "id: v1\ncategory: x\ndiff: d\nexpected: []\n")
    case_dir = tmp_path / "repo" / "file_001"
    _write(case_dir / "case.yaml", "id: rb1\ncategory: clean\nexpected: []\n")
    _write(case_dir / "changes.diff", "d\n")
    _write(case_dir / "repo" / "src" / "application.yaml", "server:\n  port: 8080\n")

    cases = load_cases(tmp_path)
    assert sorted(c.id for c in cases) == ["rb1", "v1"]
