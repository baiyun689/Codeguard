"""评测数据契约的公开行为测试。"""

from codeguard_agent.models.schemas import Issue, Severity

from evals.matcher import evaluate_case
from evals.archive import build_archive_record
from evals.metrics import aggregate
from evals.schema import EvalCase, MatchOutcome


def test_repo_case_preserves_provenance_and_ground_truth_contract() -> None:
    case = EvalCase.model_validate(
        {
            "id": "vul4j-1-reversed",
            "category": "path-traversal",
            "dimension": "security",
            "diff": "diff --git a/A.java b/A.java",
            "ground_truth_mode": "known-issue-only",
            "difficulty": "cross-file",
            "capability": ["call-path", "framework-entry"],
            "provenance": {
                "source": "vul4j",
                "repository_url": "https://example.invalid/project.git",
                "base_revision": "fixed-sha",
                "head_revision": "vulnerable-sha",
                "patch_direction": "reversed-fix",
                "license": "Apache-2.0",
            },
            "expected": [
                {
                    "id": "E1",
                    "type_keywords": ["path traversal"],
                    "file": "A.java",
                    "line": 12,
                    "root_cause": "未规范化的用户路径进入文件系统",
                    "cwe": "CWE-22",
                    "risk_tag": "path-traversal",
                    "evidence_anchors": ["A.open(String):12", "Path.resolve(String)"],
                }
            ],
        }
    )

    assert case.ground_truth_mode == "known-issue-only"
    assert case.provenance.source == "vul4j"
    assert case.provenance.patch_direction == "reversed-fix"
    assert case.expected[0].id == "E1"
    assert case.expected[0].cwe == "CWE-22"
    assert case.capability == ["call-path", "framework-entry"]


def test_match_outcome_keeps_raw_findings_and_stable_pairing_for_later_rescore() -> None:
    case = EvalCase.model_validate(
        {
            "id": "authz",
            "category": "authorization",
            "diff": "diff",
            "expected": [
                {
                    "id": "missing-authz",
                    "type_keywords": ["鉴权"],
                    "file": "OrderController.java",
                    "line": 20,
                }
            ],
        }
    )
    reported = [
        Issue(
            severity=Severity.CRITICAL,
            file="src/OrderController.java",
            line=20,
            type="缺少鉴权",
            message="更新订单前没有鉴权",
        ),
        Issue(
            severity=Severity.WARNING,
            file="src/OrderController.java",
            line=31,
            type="空指针",
            message="返回值可能为空",
        ),
    ]

    outcome = evaluate_case(case, reported)

    assert outcome.reported_issues == reported
    assert outcome.matched_expected_by_report == {0: "missing-authz"}
    assert outcome.unmatched_report_indices == [1]


def test_archive_preserves_every_run_raw_finding_for_offline_adjudication() -> None:
    case = EvalCase(id="c", category="clean", diff="diff")
    issue = Issue(
        severity=Severity.WARNING,
        file="A.java",
        line=3,
        type="空指针",
        message="可能为空",
    )
    first = MatchOutcome(
        case_id=case.id,
        is_clean=True,
        reported_total=1,
        false_positives=1,
        reported_issues=[issue],
        unmatched_report_indices=[0],
        gold_issue_ids=["E1"],
        detected_issue_ids=[],
    )
    second = first.model_copy(deep=True)
    all_runs = [[first], [second]]
    metrics = aggregate(all_runs)

    record = build_archive_record(
        profile_name="full",
        profile_mode="pipeline",
        profile_tools=[],
        tools_enabled=False,
        provider="openai",
        model="deepseek-v4-pro",
        runs=2,
        metrics=metrics,
        by_capability={},
        last_run=all_runs[-1],
        all_runs=all_runs,
        git_sha="abc",
        timestamp="2026-07-26",
        judge_provider="openai",
        judge_model="deepseek-v4-pro",
        judge_same_source=True,
        dataset_digest="dataset-abc",
        code_digest="code-def",
    )

    assert len(record["run_outcomes"]) == 2
    assert record["run_outcomes"][0][0]["reported_issues"][0]["message"] == "可能为空"
    assert record["run_outcomes"][1][0]["unmatched_report_indices"] == [0]
    restored = MatchOutcome.model_validate(record["run_outcomes"][0][0])
    assert restored == first
    assert record["assessment"] == {
        "status": "automatic-provisional",
        "judge_provider": "openai",
        "judge_model": "deepseek-v4-pro",
        "judge_same_source": True,
    }
    assert record["experiment"]["dataset_digest"] == "dataset-abc"
    assert record["experiment"]["code_digest"] == "code-def"
