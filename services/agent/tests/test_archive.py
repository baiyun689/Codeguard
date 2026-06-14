"""归档与能力切片聚合的工程正确性测试。

重点:归档记录字段完整 + 落盘文件名规范、按能力聚合切对子集、git sha 取不到时占位。
"""

from __future__ import annotations

import json

import evals.archive as archive_mod
from evals.archive import (
    GIT_SHA_PLACEHOLDER,
    build_archive_record,
    git_short_sha,
    write_archive,
)
from evals.metrics import aggregate, aggregate_by_capability
from evals.schema import MatchOutcome


def _outcome(case_id, *, clean=False, tp=0, fp=0, fn=0, expected=0, reported=0) -> MatchOutcome:
    return MatchOutcome(
        case_id=case_id,
        is_clean=clean,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        expected_total=expected,
        reported_total=reported,
    )


# --------- 按能力切片 ---------

def test_aggregate_by_capability_slices_subset():
    # 两条用例:file 用例审准(tp=1),diff-only 用例漏报(fn=1)。
    run = [
        _outcome("rb_file", tp=1, expected=1, reported=1),
        _outcome("syn", fn=1, expected=1, reported=0),
    ]
    caps = {"rb_file": ["file"], "syn": ["diff-only"]}
    sliced = aggregate_by_capability([run], caps)

    assert set(sliced) == {"file", "diff-only"}
    assert sliced["file"].recall == 1.0       # file 子集审准
    assert sliced["diff-only"].recall == 0.0  # diff-only 子集漏报


def test_aggregate_by_capability_skips_empty_tags():
    run = [_outcome("a", tp=1, expected=1, reported=1)]
    caps = {"a": ["file"]}
    sliced = aggregate_by_capability([run], caps)
    assert set(sliced) == {"file"}  # 没有 ast/rag 用例就不出现这些键


def test_case_with_multiple_capabilities_counts_in_each():
    run = [_outcome("multi", tp=1, expected=1, reported=1)]
    caps = {"multi": ["file", "ast"]}
    sliced = aggregate_by_capability([run], caps)
    assert sliced["file"].recall == 1.0
    assert sliced["ast"].recall == 1.0


# --------- 归档记录 ---------

def _sample_record(git_sha="abc123"):
    run = [
        _outcome("rb_file", tp=1, expected=1, reported=1),
        _outcome("clean1", clean=True, fp=0),
    ]
    caps = {"rb_file": ["file"], "clean1": ["diff-only"]}
    return build_archive_record(
        profile_name="pipeline-file",
        profile_mode="pipeline",
        profile_tools=["get_file_content"],
        tools_enabled=True,
        provider="openai",
        model="deepseek-chat",
        runs=1,
        metrics=aggregate([run]),
        by_capability=aggregate_by_capability([run], caps),
        last_run=run,
        git_sha=git_sha,
        timestamp="2026-06-14T10-30-00",
    )


def test_archive_record_has_all_fields():
    rec = _sample_record()
    assert rec["git_sha"] == "abc123"
    assert rec["profile"] == {
        "name": "pipeline-file",
        "mode": "pipeline",
        "tools": ["get_file_content"],
        "tools_enabled": True,
    }
    assert rec["provider"] == "openai"
    assert rec["model"] == "deepseek-chat"
    assert "precision" in rec["metrics"] and "recall" in rec["metrics"]
    assert "file" in rec["by_capability"]
    assert {c["case_id"] for c in rec["cases"]} == {"rb_file", "clean1"}


def test_write_archive_filename_and_roundtrip(tmp_path):
    rec = _sample_record()
    path = write_archive(rec, runs_dir=tmp_path)
    assert path.name == "2026-06-14T10-30-00_abc123_pipeline-file.json"
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["profile"]["name"] == "pipeline-file"


def test_write_archive_does_not_overwrite_existing(tmp_path):
    # 不同时间戳 → 不同文件名 → 追加累积。
    r1 = _sample_record()
    r2 = _sample_record()
    r2["timestamp"] = "2026-06-14T11-00-00"
    write_archive(r1, runs_dir=tmp_path)
    write_archive(r2, runs_dir=tmp_path)
    assert len(list(tmp_path.glob("*.json"))) == 2


# --------- git sha 占位 ---------

def test_git_sha_fallback_on_failure(monkeypatch):
    def _boom(*a, **k):
        raise OSError("git not found")
    monkeypatch.setattr(archive_mod.subprocess, "run", _boom)
    assert git_short_sha() == GIT_SHA_PLACEHOLDER
