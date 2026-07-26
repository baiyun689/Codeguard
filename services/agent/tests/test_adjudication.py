"""人工盲审闭环的公共接口测试。"""

from codeguard_agent.models.schemas import Issue, Severity

from evals.adjudication import (
    AdjudicationDecision,
    build_blind_bundle,
    finalize_decisions,
    load_bundle,
    load_decisions,
    public_task_view,
    render_review_page,
    rescore_with_adjudication,
    save_bundle,
    save_decision,
)
from evals.schema import EvalCase, MatchOutcome


def _extra_issue() -> Issue:
    return Issue(
        severity=Severity.WARNING,
        file="src/OrderController.java",
        line=31,
        type="空指针",
        message="查询结果可能为空，随后直接解引用",
    )


def test_pool_hides_profile_and_merges_same_unmatched_finding_across_runs() -> None:
    case = EvalCase(id="order", category="logic", diff="diff", ground_truth_mode="known-issue-only")
    issue = _extra_issue()
    first = MatchOutcome(
        case_id=case.id,
        is_clean=True,
        reported_total=1,
        reported_issues=[issue],
        unmatched_report_indices=[0],
    )
    second = first.model_copy(deep=True)

    bundle = build_blind_bundle(
        [case],
        {
            "eval-direct-diff": [[first]],
            "eval-codeguard-full": [[second]],
        },
    )

    assert len(bundle.tasks) == 1
    assert len(bundle.tasks[0].occurrences) == 2
    view = public_task_view(bundle.tasks[0])
    assert "eval-direct-diff" not in view
    assert "eval-codeguard-full" not in view
    assert view["issue"]["message"] == issue.message


def test_two_matching_reviewers_finalize_and_disagreement_requires_resolution() -> None:
    case = EvalCase(id="order", category="logic", diff="diff")
    outcome = MatchOutcome(
        case_id=case.id,
        is_clean=True,
        reported_issues=[_extra_issue()],
        reported_total=1,
        unmatched_report_indices=[0],
    )
    bundle = build_blind_bundle([case], {"full": [[outcome]]})
    task_id = bundle.tasks[0].id

    agreed = finalize_decisions(
        bundle,
        [
            AdjudicationDecision(task_id=task_id, reviewer_id="r1", label="novel-valid"),
            AdjudicationDecision(task_id=task_id, reviewer_id="r2", label="novel-valid"),
        ],
    )
    assert agreed.resolved[task_id].label == "novel-valid"
    assert agreed.conflicts == []

    disputed = finalize_decisions(
        bundle,
        [
            AdjudicationDecision(task_id=task_id, reviewer_id="r1", label="novel-valid"),
            AdjudicationDecision(task_id=task_id, reviewer_id="r2", label="invalid"),
        ],
    )
    assert disputed.resolved == {}
    assert disputed.conflicts == [task_id]


def test_valid_novel_finding_extends_shared_gold_and_rescores_every_profile() -> None:
    case = EvalCase(
        id="order",
        category="clean",
        diff="diff",
        ground_truth_mode="known-issue-only",
    )
    direct = MatchOutcome(case_id=case.id, is_clean=True)
    full = MatchOutcome(
        case_id=case.id,
        is_clean=True,
        reported_total=1,
        false_positives=1,
        reported_issues=[_extra_issue()],
        unmatched_report_indices=[0],
    )
    runs = {"direct": [[direct]], "full": [[full]]}
    bundle = build_blind_bundle([case], runs)
    task_id = bundle.tasks[0].id
    finalized = finalize_decisions(
        bundle,
        [
            AdjudicationDecision(task_id=task_id, reviewer_id="r1", label="novel-valid"),
            AdjudicationDecision(task_id=task_id, reviewer_id="r2", label="novel-valid"),
        ],
    )

    rescored = rescore_with_adjudication([case], runs, bundle, finalized)

    direct_after = rescored["direct"][0][0]
    full_after = rescored["full"][0][0]
    assert (direct_after.true_positives, direct_after.false_negatives) == (0, 1)
    assert (full_after.true_positives, full_after.false_negatives) == (1, 0)
    assert full_after.false_positives == 0
    assert full_after.novel_valid_count == 1
    assert direct_after.expected_total == full_after.expected_total == 1
    assert not direct_after.is_clean
    assert not full_after.is_clean


def test_bundle_and_latest_reviewer_decision_round_trip(tmp_path) -> None:
    case = EvalCase(id="order", category="logic", diff="diff")
    outcome = MatchOutcome(
        case_id=case.id,
        is_clean=True,
        reported_issues=[_extra_issue()],
        reported_total=1,
        unmatched_report_indices=[0],
    )
    bundle = build_blind_bundle([case], {"full": [[outcome]]})
    bundle_path = tmp_path / "bundle.json"
    decisions_path = tmp_path / "decisions.jsonl"
    save_bundle(bundle, bundle_path)

    task_id = bundle.tasks[0].id
    save_decision(
        decisions_path,
        AdjudicationDecision(task_id=task_id, reviewer_id="alice", label="uncertain"),
    )
    save_decision(
        decisions_path,
        AdjudicationDecision(task_id=task_id, reviewer_id="alice", label="novel-valid"),
    )

    assert load_bundle(bundle_path) == bundle
    decisions = load_decisions(decisions_path)
    assert len(decisions) == 1
    assert decisions[0].label == "novel-valid"


def test_review_page_is_blind_and_does_not_read_repository_source(
    tmp_path,
) -> None:
    repo = tmp_path / "repo"
    source = repo / "src/OrderController.java"
    source.parent.mkdir(parents=True)
    source.write_text("class OrderController {\n  Object value = null;\n}\n", encoding="utf-8")
    case = EvalCase(
        id="order",
        category="logic",
        diff="+ risky();",
        repo_path=str(repo),
    )
    outcome = MatchOutcome(
        case_id=case.id,
        is_clean=True,
        reported_issues=[_extra_issue()],
        reported_total=1,
        unmatched_report_indices=[0],
    )
    bundle = build_blind_bundle([case], {"secret-profile": [[outcome]]})

    page = render_review_page(bundle, [], reviewer_id="alice")

    assert "secret-profile" not in page
    assert "查询结果可能为空" in page
    assert "Object value = null" not in page
    assert "报告位置附近源码" not in page
    assert "novel-valid" in page
    assert "reviewer_id" in page


def test_two_uncertain_reviews_require_resolution() -> None:
    case = EvalCase(id="order", category="logic", diff="diff")
    outcome = MatchOutcome(
        case_id=case.id,
        is_clean=True,
        reported_issues=[_extra_issue()],
        reported_total=1,
        unmatched_report_indices=[0],
    )
    bundle = build_blind_bundle([case], {"full": [[outcome]]})
    task_id = bundle.tasks[0].id
    finalized = finalize_decisions(
        bundle,
        [
            AdjudicationDecision(task_id=task_id, reviewer_id="r1", label="uncertain"),
            AdjudicationDecision(task_id=task_id, reviewer_id="r2", label="uncertain"),
        ],
    )
    assert finalized.conflicts == [task_id]


def test_resolution_requires_two_prior_reviewers_and_distinct_chair() -> None:
    case = EvalCase(id="order", category="logic", diff="diff")
    outcome = MatchOutcome(
        case_id=case.id,
        is_clean=True,
        reported_issues=[_extra_issue()],
        reported_total=1,
        unmatched_report_indices=[0],
    )
    bundle = build_blind_bundle([case], {"full": [[outcome]]})
    task_id = bundle.tasks[0].id
    early = finalize_decisions(
        bundle,
        [AdjudicationDecision(
            task_id=task_id,
            reviewer_id="chair",
            label="novel-valid",
            is_resolution=True,
        )],
    )
    assert early.missing == [task_id]

    final = finalize_decisions(
        bundle,
        [
            AdjudicationDecision(task_id=task_id, reviewer_id="r1", label="invalid"),
            AdjudicationDecision(task_id=task_id, reviewer_id="r2", label="novel-valid"),
            AdjudicationDecision(
                task_id=task_id,
                reviewer_id="chair",
                label="novel-valid",
                is_resolution=True,
            ),
        ],
    )
    assert final.resolved[task_id].reviewer_ids == ["chair", "r1", "r2"]
