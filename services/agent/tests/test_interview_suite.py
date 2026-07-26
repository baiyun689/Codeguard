"""面试版 suite 的可复现准备接缝。"""

from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path

import yaml
import pytest

from evals.dataset import load_cases
from evals.interview_suite import (
    load_suite_manifest,
    prepare_suite,
    validate_prepared_suite,
)


def test_interview_v1_manifest_has_frozen_balanced_case_inventory() -> None:
    manifest = load_suite_manifest(
        Path(__file__).parents[1] / "evals/suites/interview-v1.yaml"
    )

    assert manifest.version == "interview-v1"
    assert len(manifest.cases) == 60
    assert len({case.id for case in manifest.cases}) == 60
    assert Counter(case.source for case in manifest.cases) == {
        "Vul4J": 25,
        "GitBug-Java": 35,
    }
    assert Counter(case.direction for case in manifest.cases) == {
        "reversed-fix": 50,
        "forward-clean": 10,
    }
    assert all(
        (case.expected is not None) == (case.direction == "reversed-fix")
        for case in manifest.cases
    )
    assert all(
        case.repository_url
        and case.fix_revision
        and case.license
        and case.capability
        for case in manifest.cases
    )


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_prepare_reversed_fix_case_creates_vulnerable_snapshot_and_review_diff(
    tmp_path: Path,
) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init")
    _git(upstream, "config", "user.email", "eval@example.invalid")
    _git(upstream, "config", "user.name", "Eval")
    source = upstream / "src/main/java/demo/NameService.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        "package demo;\nclass NameService { int size(String value) { return value.length(); } }\n",
        encoding="utf-8",
    )
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "vulnerable")
    vulnerable = _git(upstream, "rev-parse", "HEAD")
    source.write_text(
        "package demo;\nclass NameService { int size(String value) { return value == null ? 0 : value.length(); } }\n",
        encoding="utf-8",
    )
    _git(upstream, "commit", "-am", "fix null handling")
    fixed = _git(upstream, "rev-parse", "HEAD")

    manifest = tmp_path / "suite.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "version": "test-suite",
                "cases": [
                    {
                        "id": "real-null-001",
                        "source": "gitbug-java",
                        "repository_url": str(upstream),
                        "fix_revision": fixed,
                        "parent_revision": vulnerable,
                        "direction": "reversed-fix",
                        "category": "null-safety",
                        "dimension": "logic",
                        "ground_truth_mode": "known-issue-only",
                        "capability": ["whole-file"],
                        "expected": {
                            "id": "E1",
                            "type_keywords": ["null", "空指针"],
                            "root_cause": "null 输入被直接解引用",
                        },
                    },
                    {
                        "id": "real-null-clean-001",
                        "source": "gitbug-java",
                        "repository_url": str(upstream),
                        "fix_revision": fixed,
                        "parent_revision": vulnerable,
                        "direction": "forward-clean",
                        "category": "clean",
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    output = tmp_path / "prepared"
    prepare_suite(
        manifest,
        output,
        tmp_path / "cache",
        case_ids={"real-null-001"},
    )

    cases = load_cases(output)
    assert len(cases) == 1
    case = cases[0]
    assert "return value.length()" in (
        Path(case.repo_path) / "src/main/java/demo/NameService.java"
    ).read_text(encoding="utf-8")
    assert "+class NameService" in case.diff or "+package demo" in case.diff
    assert case.expected[0].file.endswith("NameService.java")
    assert case.expected[0].line == 0
    assert case.provenance.base_revision == fixed
    assert case.provenance.head_revision == vulnerable

    (output / "repo/real-null-001/changes.diff").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="diff 为空"):
        validate_prepared_suite(
            manifest,
            output,
            case_ids={"real-null-001"},
        )
