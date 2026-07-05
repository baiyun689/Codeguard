"""评测结果历史归档:每次运行落一份结构化 JSON,作为趋势分析与防退化的数据底座。

归档文件名:evals/runs/<时间>_<gitsha>_<profile>.json,追加累积、不覆盖既往。
内容:运行元信息(时间/git sha/profile/provider/model)+ 整体指标 + 按能力切片指标 +
逐用例 outcome(最后一次跑测)。

记录构建(build_archive_record)是纯函数,便于单测;写盘(write_archive)只管落文件。
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

from evals.schema import AggregateMetrics, MatchOutcome

_RUNS_DIR = Path(__file__).resolve().parent / "runs"

# git sha 取不到时的占位标识(无 git / 非仓库 / git 不可用)。
GIT_SHA_PLACEHOLDER = "nogit"


def git_short_sha(cwd: Path | None = None) -> str:
    """取当前代码的短 git sha;任何失败都回退占位标识,绝不让评测因此中断。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=5,
        )
        sha = out.stdout.strip()
        return sha or GIT_SHA_PLACEHOLDER
    except Exception:  # noqa: BLE001 git 不可用 / 超时 / 非仓库
        return GIT_SHA_PLACEHOLDER


def _metrics_dict(m: AggregateMetrics) -> dict:
    """AggregateMetrics → 可 JSON 序列化的精简字典。"""
    return {
        "precision": m.precision,
        "recall": m.recall,
        "f1": m.f1,
        "false_positives_on_clean": m.false_positives_on_clean,
        "localization_accuracy": m.localization_accuracy,
        "severity_accuracy": m.severity_accuracy,
        "precision_std": m.precision_std,
        "recall_std": m.recall_std,
        "num_cases": m.num_cases,
        "num_vuln_cases": m.num_vuln_cases,
        "num_clean_cases": m.num_clean_cases,
    }


def _outcome_dict(o: MatchOutcome) -> dict:
    d = {
        "case_id": o.case_id,
        "is_clean": o.is_clean,
        "expected_total": o.expected_total,
        "reported_total": o.reported_total,
        "true_positives": o.true_positives,
        "false_positives": o.false_positives,
        "false_negatives": o.false_negatives,
    }
    # 工具使用画像随逐用例一并归档(仅工具档有);留存"工具有没有用上"的可复现凭证。
    if o.tool_usage is not None:
        d["tool_usage"] = {
            "tool_calls": o.tool_usage.tool_calls,
            "tools_used": o.tool_usage.tools_used,
            "repomap_called": o.tool_usage.repomap_called,
            "repomap_caller_section_read": o.tool_usage.repomap_caller_section_read,
            "files_read": o.tool_usage.files_read,
        }
    if o.council_trace is not None:
        d["council_trace"] = o.council_trace.model_dump()
    return d


def build_archive_record(
    *,
    profile_name: str,
    profile_mode: str,
    profile_tools: list[str],
    profile_orchestration: str = "adr-032",
    tools_enabled: bool,
    fp_verify: bool = False,
    provider: str,
    model: str,
    runs: int,
    metrics: AggregateMetrics,
    by_capability: dict[str, AggregateMetrics],
    last_run: list[MatchOutcome],
    git_sha: str,
    timestamp: str,
) -> dict:
    """构造一条归档记录(纯函数)。`tools_enabled` 如实记录本次工具是否真正启用。"""
    return {
        "timestamp": timestamp,
        "git_sha": git_sha,
        "profile": {
            "name": profile_name,
            "mode": profile_mode,
            "orchestration": profile_orchestration,
            "tools": profile_tools,
            "tools_enabled": tools_enabled,
            "fp_verify": fp_verify,
        },
        "provider": provider,
        "model": model,
        "runs": runs,
        "metrics": _metrics_dict(metrics),
        "by_capability": {tag: _metrics_dict(m) for tag, m in by_capability.items()},
        "cases": [_outcome_dict(o) for o in last_run],
    }


def write_archive(record: dict, runs_dir: Path | None = None) -> Path:
    """把归档记录写入 runs 目录,文件名带时间/gitsha/profile;追加不覆盖。"""
    target_dir = runs_dir or _RUNS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    # 时间戳里的冒号在 Windows 文件名非法,统一替换为短横线。
    safe_ts = record["timestamp"].replace(":", "-").replace(" ", "_")
    fname = f"{safe_ts}_{record['git_sha']}_{record['profile']['name']}.json"
    path = target_dir / fname
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def archive_now_timestamp() -> str:
    """归档用时间戳(精确到秒)。"""
    return datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


def load_archives(runs_dir: Path | None = None) -> list[dict]:
    """读取 runs 目录下所有归档记录,按时间戳升序返回。目录不存在则返回空表。"""
    target_dir = runs_dir or _RUNS_DIR
    if not target_dir.is_dir():
        return []
    records: list[dict] = []
    for path in target_dir.glob("*.json"):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue  # 跳过损坏/不可读的归档,不影响报告生成
    records.sort(key=lambda r: r.get("timestamp", ""))
    return records
