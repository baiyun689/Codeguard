"""面试版评测的一站式运行、盲审与最终重评分入口。"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from pathlib import Path
from statistics import mean

from evals.adjudication import (
    build_blind_bundle,
    finalize_decisions,
    load_bundle,
    load_decisions,
    rescore_with_adjudication,
    save_bundle,
    serve_adjudication,
)
from evals.dataset import load_cases
from evals.interview_suite import prepare_suite
from evals.metrics import aggregate, aggregate_by_capability, compute_stability
from evals.schema import EvalCase, MatchOutcome

DEFAULT_PROFILES = (
    "eval-direct-diff",
    "eval-council-diff",
    "eval-council-codegraph",
    "eval-codeguard-full",
)


def _read_profile_archives(runs_dir: Path) -> tuple[dict[str, list[list[MatchOutcome]]], dict[str, dict]]:
    latest: dict[str, dict] = {}
    for path in runs_dir.glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        profile = str((record.get("profile") or {}).get("name") or "")
        if not profile:
            continue
        if profile not in latest or record.get("timestamp", "") >= latest[profile].get(
            "timestamp", ""
        ):
            latest[profile] = record
    if not latest:
        raise ValueError(f"没有找到评测归档:{runs_dir}")
    profile_runs = {
        profile: [
            [MatchOutcome.model_validate(item) for item in run]
            for run in record.get("run_outcomes", [])
        ]
        for profile, record in latest.items()
    }
    empty = [profile for profile, runs in profile_runs.items() if not runs]
    if empty:
        raise ValueError(f"归档缺少逐轮结果:{', '.join(sorted(empty))}")
    identity_keys = (
        "dataset_digest",
        "code_digest",
        "git_sha",
        "provider",
        "model",
        "judge_provider",
        "judge_model",
        "runs",
    )
    identities = {
        profile: tuple((record.get("experiment") or {}).get(key) for key in identity_keys)
        for profile, record in latest.items()
    }
    required_indexes = tuple(identity_keys.index(key) for key in (
        "dataset_digest", "code_digest", "git_sha", "provider", "model", "runs"
    ))
    if any(
        any(not identity[index] for index in required_indexes)
        for identity in identities.values()
    ):
        raise ValueError("profile 归档不可比较:缺少完整 experiment 身份")
    reference = next(iter(identities.values()))
    mismatched = [profile for profile, identity in identities.items() if identity != reference]
    if mismatched:
        raise ValueError(
            "profile 归档不可比较:实验身份不一致 " + ", ".join(sorted(mismatched))
        )
    case_shapes = {
        profile: tuple(tuple(outcome.case_id for outcome in run) for run in runs)
        for profile, runs in profile_runs.items()
    }
    reference_shape = next(iter(case_shapes.values()))
    if any(shape != reference_shape for shape in case_shapes.values()):
        raise ValueError("profile 归档不可比较:案例或轮次不一致")
    return profile_runs, latest


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def _profile_summary(
    runs: list[list[MatchOutcome]],
    case_capabilities: dict[str, list[str]],
) -> dict:
    metrics = aggregate(runs)
    stability = compute_stability(runs)
    durations = [outcome.total_duration_ms for run in runs for outcome in run]
    confidence = _bootstrap_confidence_interval(runs)
    by_capability = aggregate_by_capability(runs, case_capabilities)
    tool_rows = [
        outcome.tool_usage
        for run in runs
        for outcome in run
        if outcome.tool_usage is not None
    ]
    council_rows = [
        outcome.council_trace
        for run in runs
        for outcome in run
        if outcome.council_trace is not None
    ]
    return {
        "cases": metrics.num_cases,
        "runs": metrics.runs,
        "precision": metrics.precision,
        "recall": metrics.recall,
        "f1": metrics.f1,
        "precision_std": metrics.precision_std,
        "recall_std": metrics.recall_std,
        "false_positives_on_clean": metrics.false_positives_on_clean,
        "localization_accuracy": metrics.localization_accuracy,
        "localization_checked": metrics.localization_checked,
        "severity_accuracy": metrics.severity_accuracy,
        "stability": stability.model_dump(mode="json"),
        "latency_ms": {
            "mean": mean(durations) if durations else 0.0,
            "p50": _percentile(durations, 0.50),
            "p95": _percentile(durations, 0.95),
        },
        "bootstrap_95_ci": confidence,
        "by_capability": {
            capability: {
                "precision": sliced.precision,
                "recall": sliced.recall,
                "f1": sliced.f1,
                "cases": sliced.num_cases,
            }
            for capability, sliced in by_capability.items()
        },
        "tool_usage": {
            "mean_calls_per_case_run": (
                sum(row.tool_calls for row in tool_rows)
                / sum(len(run) for run in runs)
                if runs
                else 0.0
            ),
            "tools_used": sorted(
                {tool for row in tool_rows for tool in row.tools_used}
            ),
            "mean_evidence_calls_per_case_run": (
                sum(row.actual_evidence_tool_calls for row in council_rows)
                / sum(len(run) for run in runs)
                if runs
                else 0.0
            ),
        },
    }


def _bootstrap_confidence_interval(
    runs: list[list[MatchOutcome]],
    *,
    samples: int = 500,
) -> dict[str, list[float]]:
    """按 case 聚类重采样，避免把同一 case 的重复轮次误当独立样本。"""
    case_ids = [outcome.case_id for outcome in runs[0]] if runs else []
    if not case_ids:
        return {name: [0.0, 0.0] for name in ("precision", "recall", "f1")}
    by_run = [{outcome.case_id: outcome for outcome in run} for run in runs]
    randomizer = random.Random(20260726)
    values: dict[str, list[float]] = {name: [] for name in ("precision", "recall", "f1")}
    for _ in range(samples):
        selected = [randomizer.choice(case_ids) for _ in case_ids]
        sampled_runs = [
            [outcome_by_id[case_id] for case_id in selected]
            for outcome_by_id in by_run
        ]
        metrics = aggregate(sampled_runs)
        values["precision"].append(metrics.precision)
        values["recall"].append(metrics.recall)
        values["f1"].append(metrics.f1)
    return {
        name: [_percentile(rows, 0.025), _percentile(rows, 0.975)]
        for name, rows in values.items()
    }


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _render_comparison(summary: dict, *, final: bool) -> str:
    title = "Codeguard 面试版最终评测" if final else "Codeguard 面试版自动评测（暂定）"
    status = (
        "已完成人工双盲裁决并按共享补充标答重评分。"
        if final
        else "等待双人人工盲审；同源 LLM Judge 结果仅作为自动暂定分数。"
    )
    lines = [f"# {title}", "", status, "", "| Profile | Precision | Recall | F1（95% CI） | 稳定 Recall | 最差轮 Recall | P95 耗时(ms) |", "|---|---:|---:|---:|---:|---:|---:|"]
    for profile, values in summary["profiles"].items():
        stability = values["stability"]
        f1_ci = values["bootstrap_95_ci"]["f1"]
        lines.append(
            f"| {profile} | {values['precision']:.3f} | {values['recall']:.3f} | "
            f"{values['f1']:.3f} [{f1_ci[0]:.3f}, {f1_ci[1]:.3f}] | "
            f"{stability['stable_recall']:.3f} | "
            f"{stability['worst_run_recall']:.3f} | {values['latency_ms']['p95']:.0f} |"
        )
    capabilities = sorted(
        {
            capability
            for values in summary["profiles"].values()
            for capability in values["by_capability"]
        }
    )
    if capabilities:
        profiles = list(summary["profiles"])
        lines.extend(
            [
                "",
                "## 能力切片 Recall",
                "",
                "| 能力 | " + " | ".join(profiles) + " |",
                "|---|" + "---:|" * len(profiles),
            ]
        )
        for capability in capabilities:
            cells = [
                (
                    f"{summary['profiles'][profile]['by_capability'][capability]['recall']:.3f}"
                    if capability in summary["profiles"][profile]["by_capability"]
                    else "—"
                )
                for profile in profiles
            ]
            lines.append(f"| {capability} | " + " | ".join(cells) + " |")
    lines.extend(
        [
            "",
            "## 工具使用",
            "",
            "| Profile | 平均发现工具调用/案例轮次 | 平均举证工具调用/案例轮次 | 使用工具 |",
            "|---|---:|---:|---|",
        ]
    )
    for profile, values in summary["profiles"].items():
        usage = values["tool_usage"]
        lines.append(
            f"| {profile} | {usage['mean_calls_per_case_run']:.2f} | "
            f"{usage['mean_evidence_calls_per_case_run']:.2f} | "
            f"{', '.join(usage['tools_used']) or '—'} |"
        )
    lines.extend(
        [
            "",
            "说明：每轮独立计分，稳定性不使用多轮结果并集；人工确认的额外真实问题会成为所有 profile 共享的补充标答。",
            "",
        ]
    )
    return "\n".join(lines)


def build_provisional_artifacts(
    cases: list[EvalCase],
    runs_dir: Path,
    output_dir: Path,
) -> dict:
    """从四档（或选定档）归档生成自动暂定报告和来源盲化任务池。"""
    profile_runs, records = _read_profile_archives(runs_dir)
    case_capabilities = {case.id: case.capability for case in cases}
    bundle_path = (output_dir / "blind-bundle.json").resolve()
    save_bundle(build_blind_bundle(cases, profile_runs), bundle_path)
    same_source = any(
        (record.get("assessment") or {}).get("judge_same_source") is True
        for record in records.values()
    )
    summary = {
        "status": "automatic-provisional",
        "judge_caveat": "same-source" if same_source else "independent-or-rules",
        "case_count": len(cases),
        "blind_bundle": str(bundle_path),
        "profiles": {
            profile: _profile_summary(runs, case_capabilities)
            for profile, runs in sorted(profile_runs.items())
        },
    }
    _write_json(output_dir / "provisional-summary.json", summary)
    (output_dir / "provisional-report.md").write_text(
        _render_comparison(summary, final=False), encoding="utf-8"
    )
    return summary


def finalize_interview_artifacts(
    cases: list[EvalCase],
    runs_dir: Path,
    bundle_path: Path,
    decisions_path: Path,
    output_dir: Path,
    *,
    required_reviewers: int = 2,
) -> dict:
    """固化双人裁决并对所有 profile 离线重评分，产出最终报告。"""
    profile_runs, _ = _read_profile_archives(runs_dir)
    case_capabilities = {case.id: case.capability for case in cases}
    bundle = load_bundle(bundle_path)
    finalized = finalize_decisions(
        bundle,
        load_decisions(decisions_path),
        required_reviewers=required_reviewers,
    )
    _write_json(
        output_dir / "finalized-adjudication.json",
        finalized.model_dump(mode="json"),
    )
    if finalized.conflicts or finalized.missing:
        raise ValueError(
            "人工盲审尚未完成:"
            f"conflicts={len(finalized.conflicts)} missing={len(finalized.missing)}"
        )
    rescored = rescore_with_adjudication(cases, profile_runs, bundle, finalized)
    summary = {
        "status": "human-adjudicated-final",
        "case_count": len(cases),
        "adjudication": {
            "resolved": len(finalized.resolved),
            "conflicts": len(finalized.conflicts),
            "missing": len(finalized.missing),
        },
        "profiles": {
            profile: _profile_summary(runs, case_capabilities)
            for profile, runs in sorted(rescored.items())
        },
    }
    _write_json(output_dir / "final-summary.json", summary)
    (output_dir / "final-report.md").write_text(
        _render_comparison(summary, final=True), encoding="utf-8"
    )
    return summary


def _run_profiles(
    dataset: Path,
    workspace: Path,
    profiles: list[str],
    *,
    runs: int,
    judge: bool,
    case_ids: list[str] | None = None,
) -> None:
    archive_dir = workspace / "runs"
    report_dir = workspace / "profile-reports"
    for profile in profiles:
        command = [
            sys.executable,
            "-m",
            "evals.runner",
            "--dataset",
            str(dataset),
            "--profile",
            profile,
            "--runs",
            str(runs),
            "--archive-dir",
            str(archive_dir),
            "--report",
            str(report_dir / f"{profile}.md"),
            "--checkpoint",
            str(workspace / "checkpoints" / f"{profile}.json"),
        ]
        if judge:
            command.append("--judge")
        for case_id in case_ids or []:
            command.extend(("--case", case_id))
        subprocess.run(command, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.interview_eval")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="准备数据并依次运行四个面试 profile")
    run.add_argument("--workspace", required=True, type=Path)
    run.add_argument("--dataset", type=Path)
    run.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).resolve().parent / "suites/interview-v1.yaml",
    )
    run.add_argument(
        "--cache",
        type=Path,
        default=Path(__file__).resolve().parents[3] / ".eval-cache/interview-v1",
    )
    run.add_argument("--profile", action="append", default=[])
    run.add_argument("--case", action="append", default=[])
    run.add_argument("--limit", type=int)
    run.add_argument("--runs", type=int, default=3)
    run.add_argument("--judge", action="store_true")

    serve = sub.add_parser("serve", help="启动本地人工盲审工作台")
    serve.add_argument("--bundle", required=True, type=Path)
    serve.add_argument("--decisions", required=True, type=Path)
    serve.add_argument("--reviewer", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--resolution", action="store_true")

    final = sub.add_parser("finalize", help="按双人裁决生成最终重评分报告")
    final.add_argument("--dataset", required=True, type=Path)
    final.add_argument("--runs-dir", required=True, type=Path)
    final.add_argument("--bundle", required=True, type=Path)
    final.add_argument("--decisions", required=True, type=Path)
    final.add_argument("--output", required=True, type=Path)
    final.add_argument("--required-reviewers", type=int, default=2)

    args = parser.parse_args(argv)
    if args.command == "serve":
        serve_adjudication(
            args.bundle,
            args.decisions,
            reviewer_id=args.reviewer,
            host=args.host,
            port=args.port,
            resolution_mode=args.resolution,
        )
        return 0
    if args.command == "finalize":
        cases = load_cases(args.dataset)
        finalize_interview_artifacts(
            cases,
            args.runs_dir,
            args.bundle,
            args.decisions,
            args.output,
            required_reviewers=args.required_reviewers,
        )
        return 0

    args.workspace.mkdir(parents=True, exist_ok=True)
    dataset = args.dataset or (args.workspace / "dataset")
    if args.dataset is None:
        prepare_suite(
            args.manifest,
            dataset,
            args.cache,
            case_ids=set(args.case) or None,
            limit=args.limit,
        )
    cases = load_cases(dataset)
    if args.case:
        requested = set(args.case)
        unknown = requested - {case.id for case in cases}
        if unknown:
            parser.error(f"数据集中不存在用例:{', '.join(sorted(unknown))}")
        cases = [case for case in cases if case.id in requested]
    profiles = args.profile or list(DEFAULT_PROFILES)
    _run_profiles(
        dataset,
        args.workspace,
        profiles,
        runs=args.runs,
        judge=args.judge,
        case_ids=args.case,
    )
    summary = build_provisional_artifacts(cases, args.workspace / "runs", args.workspace)
    print(f"provisional_report={args.workspace / 'provisional-report.md'}")
    print(f"blind_bundle={summary['blind_bundle']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
