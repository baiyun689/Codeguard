"""面试版统一运行、自动暂定评分与人工终评接缝。"""

from pathlib import Path

import pytest

from codeguard_agent.models.schemas import Issue, Severity

from evals.adjudication import AdjudicationDecision, load_bundle, save_decision
from evals.archive import build_archive_record, write_archive
from evals.interview_eval import (
    build_provisional_artifacts,
    finalize_interview_artifacts,
)
from evals.metrics import aggregate
from evals.schema import EvalCase, MatchOutcome


def _write_profile(
    runs_dir: Path,
    profile: str,
    runs: list[list[MatchOutcome]],
    *,
    dataset_digest: str = "same-dataset",
) -> None:
    record = build_archive_record(
        profile_name=profile,
        profile_mode="pipeline",
        profile_tools=[],
        tools_enabled=False,
        provider="openai",
        model="deepseek-v4-pro",
        runs=len(runs),
        metrics=aggregate(runs),
        by_capability={},
        last_run=runs[-1],
        all_runs=runs,
        git_sha="abc",
        timestamp=f"2026-07-26-{profile}",
        judge_provider="openai",
        judge_model="deepseek-v4-pro",
        judge_same_source=True,
        dataset_digest=dataset_digest,
        code_digest="same-code",
    )
    write_archive(record, runs_dir)


def test_provisional_artifacts_compare_profiles_and_create_blind_pool(tmp_path: Path) -> None:
    case = EvalCase(
        id="clean",
        category="clean",
        diff="+ risky();",
        capability=["whole-file"],
    )
    issue = Issue(
        severity=Severity.WARNING,
        file="A.java",
        line=8,
        type="空指针",
        message="返回值可能为空",
    )
    direct = MatchOutcome(case_id="clean", is_clean=True, total_duration_ms=10)
    full = MatchOutcome(
        case_id="clean",
        is_clean=True,
        reported_total=1,
        false_positives=1,
        reported_issues=[issue],
        unmatched_report_indices=[0],
        total_duration_ms=20,
    )
    runs_dir = tmp_path / "runs"
    _write_profile(runs_dir, "eval-direct-diff", [[direct], [direct]])
    _write_profile(runs_dir, "eval-codeguard-full", [[full], [full]])

    summary = build_provisional_artifacts(
        [case],
        runs_dir,
        tmp_path / "result",
    )

    assert summary["status"] == "automatic-provisional"
    assert summary["judge_caveat"] == "same-source"
    assert set(summary["profiles"]) == {"eval-direct-diff", "eval-codeguard-full"}
    assert summary["profiles"]["eval-codeguard-full"]["latency_ms"]["p95"] > 0
    assert "whole-file" in summary["profiles"]["eval-codeguard-full"]["by_capability"]
    assert Path(summary["blind_bundle"]).is_file()
    assert len(load_bundle(Path(summary["blind_bundle"])).tasks) == 1
    assert "等待双人人工盲审" in (tmp_path / "result/provisional-report.md").read_text(
        encoding="utf-8"
    )


def test_provisional_artifacts_reject_mixed_experiment_archives(tmp_path: Path) -> None:
    case = EvalCase(id="clean", category="clean", diff="diff")
    outcome = MatchOutcome(case_id="clean", is_clean=True)
    runs_dir = tmp_path / "runs"
    _write_profile(runs_dir, "direct", [[outcome]], dataset_digest="dataset-a")
    _write_profile(runs_dir, "full", [[outcome]], dataset_digest="dataset-b")

    with pytest.raises(ValueError, match="不可比较"):
        build_provisional_artifacts([case], runs_dir, tmp_path / "result")


def test_human_finalization_rescores_supplemental_gold_across_all_profiles(
    tmp_path: Path,
) -> None:
    case = EvalCase(id="clean", category="clean", diff="+ risky();")
    issue = Issue(
        severity=Severity.WARNING,
        file="A.java",
        line=8,
        type="空指针",
        message="返回值可能为空",
    )
    direct = MatchOutcome(case_id="clean", is_clean=True)
    full = MatchOutcome(
        case_id="clean",
        is_clean=True,
        reported_total=1,
        false_positives=1,
        reported_issues=[issue],
        unmatched_report_indices=[0],
    )
    runs_dir = tmp_path / "runs"
    _write_profile(runs_dir, "direct", [[direct]])
    _write_profile(runs_dir, "full", [[full]])
    result_dir = tmp_path / "result"
    provisional = build_provisional_artifacts([case], runs_dir, result_dir)
    bundle = load_bundle(Path(provisional["blind_bundle"]))
    decisions = result_dir / "decisions.jsonl"
    for reviewer in ("alice", "bob"):
        save_decision(
            decisions,
            AdjudicationDecision(
                task_id=bundle.tasks[0].id,
                reviewer_id=reviewer,
                label="novel-valid",
            ),
        )

    final = finalize_interview_artifacts(
        [case],
        runs_dir,
        Path(provisional["blind_bundle"]),
        decisions,
        result_dir,
    )

    assert final["status"] == "human-adjudicated-final"
    assert final["profiles"]["direct"]["recall"] == 0.0
    assert final["profiles"]["full"]["recall"] == 1.0
    assert (result_dir / "final-report.md").is_file()


def test_human_finalization_refuses_incomplete_blind_review(tmp_path: Path) -> None:
    case = EvalCase(id="clean", category="clean", diff="+ risky();")
    issue = Issue(
        severity=Severity.WARNING,
        file="A.java",
        line=8,
        type="空指针",
        message="返回值可能为空",
    )
    outcome = MatchOutcome(
        case_id="clean",
        is_clean=True,
        reported_total=1,
        false_positives=1,
        reported_issues=[issue],
        unmatched_report_indices=[0],
    )
    runs_dir = tmp_path / "runs"
    _write_profile(runs_dir, "full", [[outcome]])
    result_dir = tmp_path / "result"
    provisional = build_provisional_artifacts([case], runs_dir, result_dir)

    with pytest.raises(ValueError, match="尚未完成"):
        finalize_interview_artifacts(
            [case],
            runs_dir,
            Path(provisional["blind_bundle"]),
            result_dir / "missing-decisions.jsonl",
            result_dir,
        )

    assert not (result_dir / "final-report.md").exists()
