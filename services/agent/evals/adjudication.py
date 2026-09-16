"""离线语义复核与评分，独立于规则匹配结果。

用法：python -m evals.adjudication prepare ARCHIVE REVIEW.json
      python -m evals.adjudication score ARCHIVE REVIEW.json
准备材料不调用模型，所有报告条目均从待复核状态开始。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def digest(archive: dict) -> str:
    return hashlib.sha256(json.dumps(archive, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def prepare(archive: dict) -> dict:
    if archive.get("runs", 1) != 1:
        raise ValueError("Use a single-run archive; do not score only the last of repeated runs")
    return {
        "archive_sha256": digest(archive),
        "reviewer": "",
        "gold_labels_checked": False,
        "cases": [
            {
                "case_id": case["case_id"],
                "gold_issue_ids": case["gold_issue_ids"],
                "reports": [
                    {"index": index, "report": report, "verdict": "pending",
                     "expected_id": "", "duplicate_of": None,
                     "rationale": "", "evidence": []}
                    for index, report in enumerate(case["reported_issues"])
                ],
            }
            for case in archive["cases"]
        ],
    }


def score(archive: dict, review: dict) -> dict:
    """拒绝未完成或不匹配的复核结果，不自动补填真阳性或误报判定。"""
    if archive.get("runs", 1) != 1:
        raise ValueError("Use a single-run archive")
    if review.get("archive_sha256") != digest(archive):
        raise ValueError("Archive changed; regenerate adjudication")
    if not str(review.get("reviewer", "")).strip() or review.get("gold_labels_checked") is not True:
        raise ValueError("Reviewer identity and gold-label check are required")
    original = {case["case_id"]: case for case in archive["cases"]}
    entries = review.get("cases", [])
    if len(entries) != len(original) or {c["case_id"] for c in entries} != set(original):
        raise ValueError("Cases missing, duplicated or unexpected")
    totals = {"tp": 0, "fn": 0, "false_reports": 0, "duplicate_reports": 0,
              "novel_valid_reports": 0, "out_of_scope_reports": 0,
              "clean_cases": 0, "clean_cases_with_reports": 0,
              "clean_cases_with_false_reports": 0}
    details = []
    for case in entries:
        raw = original[case["case_id"]]
        gold = raw["gold_issue_ids"]
        if case["gold_issue_ids"] != gold or len(set(gold)) != len(gold):
            raise ValueError("Gold denominator changed or duplicate gold IDs")
        reports = case["reports"]
        if len(reports) != len(raw["reported_issues"]) or sorted(r["index"] for r in reports) != list(range(len(reports))):
            raise ValueError("Reports missing or duplicated")
        hits: set[str] = set()
        verdicts = {r["index"]: r["verdict"] for r in reports}
        false_count = 0
        for report in reports:
            if report["report"] != raw["reported_issues"][report["index"]]:
                raise ValueError("Reported finding content changed")
            if not report["rationale"].strip() or not report["evidence"]:
                raise ValueError("Each decision needs rationale and evidence references")
            verdict = report["verdict"]
            if verdict == "correct":
                eid = report["expected_id"]
                if eid not in gold or eid in hits:
                    raise ValueError("Unknown or duplicate matched gold issue")
                hits.add(eid)
            elif verdict == "duplicate":
                other = report.get("duplicate_of")
                if other == report["index"] or verdicts.get(other) not in {"correct", "novel_valid"}:
                    raise ValueError("Duplicate must point to a retained correct/novel report")
                totals["duplicate_reports"] += 1
            elif verdict == "false_positive":
                false_count += 1
            elif verdict == "novel_valid":
                # 正常变更中若发现真实缺陷，须更新标注与数据集版本。
                if not gold:
                    raise ValueError("Clean label contradicted: version the dataset before scoring")
                totals["novel_valid_reports"] += 1
            elif verdict == "out_of_scope":
                totals["out_of_scope_reports"] += 1
            else:
                raise ValueError("Pending/disputed verdicts cannot produce final metrics")
        totals["tp"] += len(hits)
        totals["fn"] += len(gold) - len(hits)
        totals["false_reports"] += false_count
        if not gold:
            totals["clean_cases"] += 1
            totals["clean_cases_with_reports"] += int(bool(reports))
            totals["clean_cases_with_false_reports"] += int(false_count > 0)
        details.append({"case_id": case["case_id"], "detected": sorted(hits), "missed": sorted(set(gold) - hits)})
    valid = totals["tp"] + totals["novel_valid_reports"]
    all_reports = valid + totals["false_reports"] + totals["duplicate_reports"] + totals["out_of_scope_reports"]
    denominator = totals["tp"] + totals["fn"]
    return {"counts": totals, "known_defect_recall": totals["tp"] / denominator if denominator else None,
            "unique_valid_report_precision": valid / all_reports if all_reports else None,
            "cases": details, "scope": "Archive cases only; repeated-run stability must be reported separately"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "score"])
    parser.add_argument("archive", type=Path)
    parser.add_argument("review", type=Path)
    args = parser.parse_args()
    archive = json.loads(args.archive.read_text(encoding="utf-8"))
    if args.action == "prepare":
        with args.review.open("x", encoding="utf-8") as stream:
            json.dump(prepare(archive), stream, ensure_ascii=False, indent=2)
    else:
        print(json.dumps(score(archive, json.loads(args.review.read_text(encoding="utf-8"))), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
